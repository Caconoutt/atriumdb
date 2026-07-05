"""Real-dataset tests for dashboard cohort resolution.

These tests require a real AtriumDB dataset to be mounted into the container.
They are skipped automatically when ATRIUMDB_DATASET_LOCATION is not set, so
they never block the normal CI test run.

To run them:

    docker run --rm -it \\
      -v "/host/path/to/dataset":/data/atriumdb \\
      --env-file .env \\
      atriumdb-sdk python -m pytest tests/test_dashboard_real_data.py -v -s

Workflow
--------
1. Run ``test_inspect_real_dataset`` first (-v -s) to see what patients,
   MRNs, encounters, and units are actually in the dataset.
2. Copy a few real MRNs from that output into ``test_mrn_cohort_real_data``
   and adjust the admission date range to match your data.
3. Run ``test_demographic_cohort_real_data`` to verify demographic filtering
   works end-to-end against the real schema.
"""

import os
import datetime

import pytest

from atriumdb import AtriumSDK
from atriumdb.dashboard.measure_queries import query_measure_total_hours

# from atriumdb.dashboard.schemas import (
#     AdmissionDateRange,
#     AgeBand,
#     CohortDefinitionRequest,
#     DemographicCohort,
#     MrnCohort,
# )

DATASET_LOCATION = os.environ.get("ATRIUMDB_DATASET_LOCATION")

pytestmark = pytest.mark.skipif(
    not DATASET_LOCATION,
    reason="ATRIUMDB_DATASET_LOCATION not set — skipping real-data tests. "
           "See dockersetup.md section 4 for how to mount a dataset.",
)


@pytest.fixture(scope="module")
def sdk():
    return AtriumSDK(dataset_location=DATASET_LOCATION, auto_upgrade=True)


# ---------------------------------------------------------------------------
# Step 1 — discovery: run this first to learn what is in the dataset
# ---------------------------------------------------------------------------

# def test_inspect_real_dataset(sdk):
#     """Print a summary of patients, MRNs, encounters, and units in the dataset.

#     Run with -v -s to see the full output.  Use this output to fill in the
#     real MRNs and date ranges used by the tests below.
#     """
#     patients = sdk.sql_handler.select_all_patients_in_list()
#     print(f"\n{'='*60}")
#     print(f"PATIENTS ({len(patients)} total — showing first 30)")
#     print(f"{'='*60}")
#     print(f"  {'id':>6}  {'mrn':<20}  {'gender':<8}  {'dob (ns)'}")
#     for row in patients[:30]:
#         pid, mrn, gender, dob = row[0], row[1], row[2], row[3]
#         print(f"  {pid:>6}  {str(mrn):<20}  {str(gender):<8}  {dob}")
#     if len(patients) > 30:
#         print(f"  ... and {len(patients) - 30} more")

#     encounters = sdk.sql_handler.select_patient_encounters()
#     print(f"\n{'='*60}")
#     print(f"ENCOUNTERS ({len(encounters)} total — showing first 20)")
#     print(f"{'='*60}")
#     print(f"  {'enc_id':>7}  {'patient_id':>10}  {'visit_number':<16}  "
#           f"{'unit_name':<12}  start_time_ns")
#     for row in encounters[:20]:
#         enc_id, pid, visit, bed_id, unit_id, unit_name, start_ns, end_ns = row
#         print(f"  {enc_id:>7}  {pid:>10}  {str(visit):<16}  "
#               f"{str(unit_name):<12}  {start_ns}")

#     if encounters:
#         start_times = [r[6] for r in encounters if r[6] is not None]
#         if start_times:
#             print(f"\n  Encounter start_time range:")
#             print(f"    min = {min(start_times)}")
#             print(f"    max = {max(start_times)}")

#     print(f"\n{'='*60}")
#     print("HINT: copy MRNs from the patient list above into")
#     print("test_mrn_cohort_real_data, and set admit_start_ns / admit_end_ns")
#     print("to bracket the encounter start_time range shown above.")
#     print(f"{'='*60}\n")

#     assert len(patients) > 0, "Dataset has no patients — check the mount path."


# ---------------------------------------------------------------------------
# Step 2 — MRN cohort (Priority 1A)  [requires atriumdb.dashboard.schemas]
# ---------------------------------------------------------------------------

# KNOWN_MRNS: list[str] = [
#     # "12345678",
#     # "87654321",
# ]
#
# ADMIT_START_NS: int = 0
# ADMIT_END_NS: int = 10 ** 19
#
# @pytest.mark.skipif(not KNOWN_MRNS, reason="KNOWN_MRNS list is empty")
# def test_mrn_cohort_real_data(sdk):
#     """1A: MRN cohort — supplied MRNs must come back in the resolved list."""
#     date_range = AdmissionDateRange(start=ADMIT_START_NS, end=ADMIT_END_NS)
#     request = CohortDefinitionRequest(
#         type="mrn",
#         admission_date_range=date_range,
#         cohorts=[MrnCohort(id="real_mrn_cohort", mrn_list=KNOWN_MRNS)],
#     )
#     result = sdk.dashboard_resolve_cohort(request, request_id="real-1a")
#     resolved = set(result.cohorts[0].mrn_list)
#     print(f"\nResolved MRNs: {resolved}")
#     assert result.cohorts[0].id == "real_mrn_cohort"
#     assert len(resolved) > 0
#     assert resolved.issubset(set(KNOWN_MRNS))


# ---------------------------------------------------------------------------
# Step 3 — demographic cohort (Priority 1B)  [requires atriumdb.dashboard.schemas]
# ---------------------------------------------------------------------------

# def test_demographic_cohort_no_filters(sdk):
#     """1B: demographic cohort with no filters returns all in-window patients."""
#     encounters = sdk.sql_handler.select_patient_encounters()
#     if not encounters:
#         pytest.skip("Dataset has no encounters — nothing to query.")
#     start_times = [r[6] for r in encounters if r[6] is not None]
#     if not start_times:
#         pytest.skip("No encounter start times found.")
#     admit_start = min(start_times) - 1
#     admit_end = max(start_times) + 1
#     date_range = AdmissionDateRange(start=admit_start, end=admit_end)
#     request = CohortDefinitionRequest(
#         type="demographic",
#         admission_date_range=date_range,
#         cohorts=[DemographicCohort(id="all_patients")],
#     )
#     result = sdk.dashboard_resolve_cohort(request, request_id="real-1b-all")
#     resolved = set(result.cohorts[0].mrn_list)
#     print(f"\nAll in-window MRNs ({len(resolved)}): {sorted(resolved)[:20]}")
#     assert len(resolved) > 0, "Expected at least one patient in the date window."
#
#
# def test_demographic_cohort_sex_filter(sdk):
#     """1B: sex filter — verify M and F cohorts partition the full result."""
#     encounters = sdk.sql_handler.select_patient_encounters()
#     if not encounters:
#         pytest.skip("Dataset has no encounters.")
#     start_times = [r[6] for r in encounters if r[6] is not None]
#     admit_start = min(start_times) - 1
#     admit_end = max(start_times) + 1
#     date_range = AdmissionDateRange(start=admit_start, end=admit_end)
#     request = CohortDefinitionRequest(
#         type="demographic",
#         admission_date_range=date_range,
#         cohorts=[
#             DemographicCohort(id="male",   sex=["M"]),
#             DemographicCohort(id="female", sex=["F"]),
#         ],
#     )
#     result = sdk.dashboard_resolve_cohort(request, request_id="real-1b-sex")
#     by_id = {c.id: set(c.mrn_list) for c in result.cohorts}
#     print(f"\n  male cohort   ({len(by_id['male'])}):   {sorted(by_id['male'])[:10]}")
#     print(f"  female cohort ({len(by_id['female'])}): {sorted(by_id['female'])[:10]}")
#     overlap = by_id["male"] & by_id["female"]
#     assert not overlap, f"Patients appear in both M and F cohorts: {overlap}"


# ---------------------------------------------------------------------------
# Step 4 — measure coverage (measures/hours API)
#
# Queries block_index, converts num_values → hours via freq_nhz, and writes
# a human-readable report to LOG_PATH.
# ---------------------------------------------------------------------------

# Override by setting ATRIUMDB_MEASURE_HOURS_LOG in the environment.
_DEFAULT_LOG_PATH = os.path.join(
    os.path.dirname(__file__), "measure_hours_report.log"
)
LOG_PATH = os.environ.get("ATRIUMDB_MEASURE_HOURS_LOG", _DEFAULT_LOG_PATH)


def _fmt_hours(h: float) -> str:
    total_minutes = int(h * 60)
    hh, mm = divmod(total_minutes, 60)
    return f"{hh:,} h {mm:02d} m"


def test_measure_hours_report(sdk):
    """Write a per-measure coverage report from block_index to LOG_PATH.

    Counts stored samples from ``block_index`` and converts to hours using
    each measure's ``freq_nhz``. Asserts all totals are non-negative. Read
    the log file after the run for a human-friendly table.
    """
    rows = query_measure_total_hours(sdk)

    if not rows:
        pytest.skip("block_index is empty — dataset has no ingested blocks.")

    for row in rows:
        assert row["total_hours"] >= 0, (
            f"Negative total_hours for measure {row['measure_id']}"
        )

    timestamp = datetime.datetime.now().isoformat(timespec="seconds")
    lines = [
        "=" * 66,
        f"  AtriumDB Measure Coverage Report  —  {timestamp}",
        f"  Dataset : {DATASET_LOCATION}",
        f"  Source  : block_index  ({len(rows)} measures)",
        "=" * 66,
        "",
        f"  {'ID':>6}  {'tag':<30}  {'units':<12}  {'total_hours':>14}",
        "-" * 66,
    ]
    for r in rows:
        lines.append(
            f"  {r['measure_id']:>6}  {str(r['measure_tag'] or ''):<30}"
            f"  {str(r['units'] or ''):<12}  {_fmt_hours(r['total_hours']):>14}"
        )
    grand = sum(r["total_hours"] for r in rows)
    lines += [
        "-" * 66,
        f"  {'':>6}  {'TOTAL':<30}  {'':>12}  {_fmt_hours(grand):>14}",
        "",
        "=" * 66,
        "",
    ]
    report = "\n".join(lines)

    with open(LOG_PATH, "w", encoding="utf-8") as fh:
        fh.write(report)

    print(f"\nMeasure coverage report written to: {LOG_PATH}")
    print(report)
