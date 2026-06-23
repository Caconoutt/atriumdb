"""Tests for the measure-resolution layer (spec section 9).

Uses a throwaway local AtriumDB dataset -- no live instance needed. If the compiled
``atriumdb`` package is unavailable the whole module is skipped.
"""
import sys
from pathlib import Path

import pytest

# Make the dashboard package importable when run from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytest.importorskip("atriumdb")
from atriumdb import AtriumSDK  # noqa: E402

from measure_mapping import (  # noqa: E402
    DashboardDB,
    initialize_measure_mapping,
    load_seed_rows,
    resolve_for_retrieval,
    resolve_measure_id,
    resync_atriumdb_ids,
)

FREQ_NHZ = 976_562_500


@pytest.fixture
def seed_rows():
    return load_seed_rows()


@pytest.fixture
def sdk(tmp_path, seed_rows):
    """A dataset that contains every seeded triple EXCEPT one deliberately omitted
    (an mmHg waveform tag) to exercise the unresolved path."""
    sdk = AtriumSDK.create_dataset(dataset_location=str(tmp_path / "ds"),
                                   database_type="sqlite")
    for row in seed_rows:
        if row.tag == "MDC_PRESS_BLD" and row.measure_group == "waveform":
            continue  # leave one seeded triple absent in this instance
        sdk.insert_measure(measure_tag=row.tag, freq=row.freq_nhz, freq_units="nHz",
                           units=row.unit_code)
    return sdk


@pytest.fixture
def db():
    d = DashboardDB(":memory:")
    d.ensure_schema()
    return d


def _dump(db):
    measures = db.conn.execute(
        "SELECT tag, freq_nhz, unit_code, atriumdb_measure_id, display_name "
        "FROM measure ORDER BY tag, freq_nhz, unit_code"
    ).fetchall()
    mappings = db.conn.execute(
        "SELECT measure_id, measure_group, vital_sign, review, conversion_factor, "
        "canonical_unit, source FROM measure_mapping ORDER BY measure_id"
    ).fetchall()
    return [tuple(r) for r in measures], [tuple(r) for r in mappings]


# 1. Resolution -------------------------------------------------------------
def test_resolution_exact_and_wrong_unit(sdk):
    mid = resolve_measure_id(sdk, "MDC_PRESS_BLD_ART_ABP_SYS", FREQ_NHZ, "MDC_DIM_MMHG")
    assert isinstance(mid, int)
    # Wrong unit on the same tag/freq must not resolve.
    assert resolve_measure_id(sdk, "MDC_PRESS_BLD_ART_ABP_SYS", FREQ_NHZ,
                              "MDC_DIM_KILO_PASCAL") is None


# 2. Init idempotency -------------------------------------------------------
def test_init_idempotent(sdk, db, seed_rows):
    r1 = initialize_measure_mapping(sdk, db, seed_rows, coverage_policy="ignore")
    state1 = _dump(db)
    r2 = initialize_measure_mapping(sdk, db, seed_rows, coverage_policy="ignore")
    state2 = _dump(db)
    assert state1 == state2
    assert r1.processed == r2.processed == len(seed_rows)


# 3. Admin guard ------------------------------------------------------------
def test_admin_edits_win(sdk, db, seed_rows):
    initialize_measure_mapping(sdk, db, seed_rows, coverage_policy="ignore")
    # An admin re-buckets a heart-rate measure and marks the row admin-owned.
    pk = db.conn.execute(
        "SELECT id FROM measure WHERE tag='MDC_PULS_RATE'"
    ).fetchone()["id"]
    db.conn.execute(
        "UPDATE measure_mapping SET measure_group='needs_review', source='admin' "
        "WHERE measure_id=?", (pk,))
    db.conn.commit()

    initialize_measure_mapping(sdk, db, seed_rows, coverage_policy="ignore")  # re-seed
    row = db.conn.execute(
        "SELECT measure_group, source FROM measure_mapping WHERE measure_id=?", (pk,)
    ).fetchone()
    assert row["measure_group"] == "needs_review"
    assert row["source"] == "admin"


# 4. Unresolved path --------------------------------------------------------
def test_unresolved_row_stored_null(sdk, db, seed_rows):
    report = initialize_measure_mapping(sdk, db, seed_rows, coverage_policy="ignore")
    assert len(report.unresolved) == 1
    assert report.unresolved[0].tag == "MDC_PRESS_BLD"
    row = db.conn.execute(
        "SELECT atriumdb_measure_id FROM measure WHERE tag='MDC_PRESS_BLD' "
        "AND freq_nhz=125000000000"
    ).fetchone()
    assert row is not None
    assert row["atriumdb_measure_id"] is None
    # Boot succeeded: every seed row processed.
    assert report.processed == len(seed_rows)


# 5. Request resolution -----------------------------------------------------
def test_resolve_for_retrieval_vital_sign(sdk, db, seed_rows):
    initialize_measure_mapping(sdk, db, seed_rows, coverage_policy="ignore")
    resolved = resolve_for_retrieval(db, vital_sign="systolic_bp")
    # All systolic sources resolve here (none omitted, none NULL in this instance).
    expected = sum(1 for r in seed_rows if r.vital_sign == "systolic_bp")
    assert len(resolved) == expected
    assert all(isinstance(r.atriumdb_measure_id, int) for r in resolved)
    # The kPa variant carries the conversion factor through, others are 1.0.
    kpa = [r for r in resolved if r.unit_code == "MDC_DIM_KILO_PASCAL"]
    assert len(kpa) == 1
    assert kpa[0].conversion_factor == pytest.approx(7.50062)
    assert kpa[0].canonical_unit == "mmHg"


def test_resolve_single_measure_id(sdk, db, seed_rows):
    initialize_measure_mapping(sdk, db, seed_rows, coverage_policy="ignore")
    pk = db.conn.execute(
        "SELECT id FROM measure WHERE tag='MDC_PRESS_BLD_ART_ABP_SYS'"
    ).fetchone()["id"]
    resolved = resolve_for_retrieval(db, measure_id=pk)
    assert len(resolved) == 1
    assert resolved[0].measure_id == pk
    assert resolved[0].display_name.startswith("Systolic BP")


def test_unresolved_measure_omitted(sdk, db, seed_rows):
    initialize_measure_mapping(sdk, db, seed_rows, coverage_policy="ignore")
    # The omitted waveform is selectable by group but must not appear (id NULL).
    waveforms = resolve_for_retrieval(db, measure_group="waveform")
    assert all(r.unit_code == "MDC_DIM_MMHG" for r in waveforms)
    assert not any(r.display_name == "MDC_PRESS_BLD" for r in waveforms)


# Resync after a reload -----------------------------------------------------
def test_resync_refreshes_ids(tmp_path, db, seed_rows):
    sdk1 = AtriumSDK.create_dataset(dataset_location=str(tmp_path / "ds1"),
                                    database_type="sqlite")
    sdk1.insert_measure(measure_tag="MDC_PRESS_BLD_ART_ABP_SYS", freq=FREQ_NHZ,
                        freq_units="nHz", units="MDC_DIM_MMHG")
    initialize_measure_mapping(sdk1, db, seed_rows, coverage_policy="ignore")
    before = db.conn.execute(
        "SELECT atriumdb_measure_id FROM measure WHERE tag='MDC_PRESS_BLD_ART_ABP_SYS'"
    ).fetchone()["atriumdb_measure_id"]
    assert before is not None

    # A fresh instance assigns ids in a different order -> the triple still resolves.
    sdk2 = AtriumSDK.create_dataset(dataset_location=str(tmp_path / "ds2"),
                                    database_type="sqlite")
    sdk2.insert_measure(measure_tag="MDC_PRESS_BLD_DECOY", freq=FREQ_NHZ,
                        freq_units="nHz", units="MDC_DIM_MMHG")
    new_id = sdk2.insert_measure(measure_tag="MDC_PRESS_BLD_ART_ABP_SYS", freq=FREQ_NHZ,
                                 freq_units="nHz", units="MDC_DIM_MMHG")
    resync_atriumdb_ids(sdk2, db, seed_rows)
    after = db.conn.execute(
        "SELECT atriumdb_measure_id FROM measure WHERE tag='MDC_PRESS_BLD_ART_ABP_SYS'"
    ).fetchone()["atriumdb_measure_id"]
    assert after == new_id


# Coverage: unseeded instance measure auto-inserted as needs_review ----------
def test_coverage_auto_inserts_unseeded(tmp_path, db, seed_rows):
    sdk = AtriumSDK.create_dataset(dataset_location=str(tmp_path / "ds"),
                                   database_type="sqlite")
    sdk.insert_measure(measure_tag="MDC_PRESS_BLD_ART_ABP_SYS", freq=FREQ_NHZ,
                       freq_units="nHz", units="MDC_DIM_MMHG")
    sdk.insert_measure(measure_tag="BRAND_NEW_CHANNEL", freq=FREQ_NHZ,
                       freq_units="nHz", units="MDC_DIM_MMHG")
    report = initialize_measure_mapping(sdk, db, seed_rows, coverage_policy="needs_review")
    assert ("BRAND_NEW_CHANNEL", FREQ_NHZ, "MDC_DIM_MMHG") in report.unseeded
    row = db.conn.execute(
        "SELECT mm.measure_group, m.atriumdb_measure_id FROM measure m "
        "JOIN measure_mapping mm ON mm.measure_id=m.id WHERE m.tag='BRAND_NEW_CHANNEL'"
    ).fetchone()
    assert row["measure_group"] == "needs_review"
    assert row["atriumdb_measure_id"] is not None
