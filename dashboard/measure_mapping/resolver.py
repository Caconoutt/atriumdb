"""The measure-resolution translation layer.

Two responsibilities, per the spec:

* **Init (section 5):** build the lookup that maps each seed measure to its AtriumDB
  ``measure_id``. This is the only code path that calls the AtriumDB SDK.
* **Request time (section 6):** turn a stable dashboard reference (a ``measure.id``, or a
  ``vital_sign`` / ``measure_group`` key) into the AtriumDB ``measure_id``(s) plus the
  metadata a retriever needs. Pure dashboard-DB lookup -- no SDK call.

Out of scope (separate functions consume this layer's output): pulling samples and
aligning/merging series.
"""
from __future__ import annotations

import csv
import logging
from pathlib import Path

from .db import DashboardDB
from .models import InitReport, ResolvedMeasure, SeedRow

log = logging.getLogger("measure_mapping")

DEFAULT_SEED = Path(__file__).resolve().parent / "seed_measures.csv"


# --- seed loading ----------------------------------------------------------
def load_seed_rows(path: str | Path = DEFAULT_SEED) -> list[SeedRow]:
    with Path(path).open(newline="", encoding="utf-8") as fh:
        return [SeedRow.from_csv(row) for row in csv.DictReader(fh)]


# --- resolution helper (verified against atriumdb) -------------------------
def resolve_measure_id(sdk, tag: str, freq_nhz: int, unit_code: str) -> int | None:
    """Resolve the AtriumDB id for an exact triple, or None if absent in this instance.

    Verified: ``get_measure_id`` returns an int for an exact match and None (does not
    raise) otherwise -- including when only the unit differs. Always pass freq in nHz.
    """
    return sdk.get_measure_id(
        measure_tag=tag, freq=freq_nhz, units=unit_code, freq_units="nHz"
    )


# --- init: build the id lookup ---------------------------------------------
def initialize_measure_mapping(sdk, db: DashboardDB, seed_rows: list[SeedRow], *,
                               coverage_policy: str = "needs_review") -> InitReport:
    """Populate measure + measure_mapping, resolving each triple's AtriumDB id.

    Transactional; an unresolved triple must not crash boot -- the row is stored with
    ``atriumdb_measure_id = NULL`` and recorded as unresolved. Admin edits are preserved.

    ``coverage_policy``: "needs_review" auto-inserts instance measures missing from the
    seed (recommended, keeps everything resolvable); "ignore" skips that reconciliation.
    """
    report = InitReport()
    with db.transaction():
        for row in seed_rows:
            # Resolve identity from the triple -- never trust any id from the seed.
            atriumdb_id = resolve_measure_id(sdk, row.tag, row.freq_nhz, row.unit_code)

            if atriumdb_id is None:
                report.unresolved.append(row)  # absent in THIS instance; still stored, id NULL
            else:
                report.resolved += 1
                info = sdk.get_measure_info(atriumdb_id)
                if info is not None and info.get("unit") != row.unit_code:
                    report.unit_mismatch.append((row, info.get("unit")))

            measure_pk = db.upsert_measure(
                tag=row.tag, freq_nhz=row.freq_nhz, unit_code=row.unit_code,
                unit_label=row.unit_label, display_name=row.name,
                atriumdb_measure_id=atriumdb_id,
            )
            db.upsert_mapping_if_seed_owned(
                measure_id=measure_pk, measure_group=row.measure_group,
                vital_sign=row.vital_sign or None, review=row.review,
                conversion_factor=row.conversion_factor, canonical_unit=row.canonical_unit,
                source="seed",
            )
            report.processed += 1

        if coverage_policy == "needs_review":
            _reconcile_unseeded(sdk, db, seed_rows, report)

    log.info(report.summary())
    return report


def _reconcile_unseeded(sdk, db: DashboardDB, seed_rows: list[SeedRow],
                        report: InitReport) -> None:
    """Auto-insert instance measures missing from the seed so they stay resolvable."""
    try:
        live = sdk.get_all_measures()
    except Exception as exc:  # pragma: no cover - diagnostics only
        log.warning("coverage check skipped: get_all_measures failed: %s", exc)
        return
    seed_keys = {(r.tag, int(r.freq_nhz), r.unit_code) for r in seed_rows}
    for m in live.values():
        key = (m["tag"], m["freq_nhz"], m["unit"])
        if key in seed_keys:
            continue
        report.unseeded.append(key)
        db.insert_unseeded(
            tag=m["tag"], freq_nhz=m["freq_nhz"], unit_code=m["unit"],
            unit_label=m.get("unit_label"), display_name=m.get("name") or m["tag"],
            atriumdb_measure_id=m["id"],
        )


# --- re-seed / re-sync ------------------------------------------------------
def resync_atriumdb_ids(sdk, db: DashboardDB, seed_rows: list[SeedRow]) -> InitReport:
    """Re-run only the resolution step and refresh cached ids after an AtriumDB reload.

    Semantics (groups, vital signs, admin edits) are left untouched.
    """
    report = InitReport()
    with db.transaction():
        for row in seed_rows:
            atriumdb_id = resolve_measure_id(sdk, row.tag, row.freq_nhz, row.unit_code)
            if atriumdb_id is None:
                report.unresolved.append(row)
            else:
                report.resolved += 1
            db.refresh_atriumdb_id(
                tag=row.tag, freq_nhz=row.freq_nhz, unit_code=row.unit_code,
                atriumdb_measure_id=atriumdb_id,
            )
            report.processed += 1
    log.info("resync %s", report.summary())
    return report


# --- request-time resolution ------------------------------------------------
def resolve_for_retrieval(db: DashboardDB, *, measure_id=None, vital_sign=None,
                          measure_group=None) -> list[ResolvedMeasure]:
    """Turn a stable dashboard reference into AtriumDB id(s) + retriever metadata.

    Dashboard-DB lookup only (id was cached at init). The client never sends an AtriumDB
    measure_id; it sends a ``measure.id`` and/or a ``vital_sign`` / ``measure_group`` key.
    A ``vital_sign`` selector legitimately returns multiple ids. Measures unresolved in
    this instance (``atriumdb_measure_id`` NULL) are omitted -- the caller sees fewer ids
    rather than a failure.
    """
    rows = db.query_measures(measure_id=measure_id, vital_sign=vital_sign,
                             measure_group=measure_group)
    return [
        ResolvedMeasure(
            atriumdb_measure_id=r.atriumdb_measure_id,
            conversion_factor=r.conversion_factor,
            canonical_unit=r.canonical_unit,
            unit_code=r.unit_code,
            display_name=r.display_name,
            measure_id=r.id,
            vital_sign=r.vital_sign,
            measure_group=r.measure_group,
        )
        for r in rows
        if r.atriumdb_measure_id is not None
    ]
