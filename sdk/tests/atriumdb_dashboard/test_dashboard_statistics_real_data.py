"""Real-dataset integration test for the S2 aggregate statistics pipeline.

Requires a real AtriumDB dataset mounted at ATRIUMDB_DATASET_LOCATION.
Skipped automatically when the env var is absent.

Admission timestamps below come from the Step-3 demographic cohort test
(test_dashboard_real_data.py on the s1_stats branch) and are treated as
fixed inputs here — this test exercises only the S2 statistics resolver.

Run with:
    pytest tests/test_statistics_real_data.py -v -s
"""

import json
import os
from pathlib import Path

import pytest

from atriumdb import AtriumSDK
from atriumdb_dashboard.schemas import (
    Admission,
    AggregateStatisticsRequest,
    CohortInput,
    MeasureIdentifier,
    PatientAdmission,
)
from atriumdb_dashboard.statistics_resolver import compute_aggregate_statistics

# ---------------------------------------------------------------------------
# Dataset guard — skip when no real data is mounted
# ---------------------------------------------------------------------------

DATASET_LOCATION = os.environ.get("ATRIUMDB_DATASET_LOCATION")

pytestmark = pytest.mark.skipif(
    not DATASET_LOCATION,
    reason="ATRIUMDB_DATASET_LOCATION not set — skipping real-data tests.",
)


@pytest.fixture(scope="module")
def sdk():
    return AtriumSDK(dataset_location=DATASET_LOCATION, auto_upgrade=True)


# ---------------------------------------------------------------------------
# Measure to analyse — fill these in before running
# ---------------------------------------------------------------------------

STATS_MEASURE_TAG: str       = "MDC_ECG_CARD_BEAT_RATE"
STATS_FREQ: float            = 976562500
STATS_UNITS: str | None      = "MDC_DIM_BEAT_PER_MIN"
STATS_FREQ_UNITS: str | None = None

STATS_WINDOW_NS: int                = 24 * 3600 * 1_000_000_000  # 24 hours
STATS_AVAILABILITY_THRESHOLD: float = 0.20

STATS_LOG_PATH: Path = Path(__file__).parent / "cohort_stats_real_data.log"

# ---------------------------------------------------------------------------
# Admission timestamps validated by the Step-3 demographic cohort test.
# Keys are cohort names; values map MRN → list of admission timestamps (ns).
# ---------------------------------------------------------------------------

EXPECTED_DEMOGRAPHIC_ADMISSIONS: dict[str, dict[str, list[int]]] = {
    "male_age_10_0_15_0": {
        "MRN824A6D3E": [1608553652000000000],
        "MRND62AADF3": [1586531479000000000],
        "MRN79BFCA31": [1597582892000000000],
    },
    "female_age_10_3_15_10": {
        "MRN63163B57": [1591514059000000000],
        "MRN44A9FE95": [1586164554000000000],
    },
    "female_age_2_5_7_11": {
        "MRN3E7DB077": [1584420294000000000],
        "MRNAA5C025D": [1587313853000000000],
        "MRN376EFBF4": [1587319600000000000],
    },
}

# Cohort name → integer ID passed to the statistics endpoint
_COHORT_ID_MAP: dict[str, int] = {
    "male_age_10_0_15_0":    1,
    "female_age_10_3_15_10": 2,
    "female_age_2_5_7_11":   3,
}
_COHORT_NAME_MAP: dict[int, str] = {v: k for k, v in _COHORT_ID_MAP.items()}


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not STATS_MEASURE_TAG,
    reason="STATS_MEASURE_TAG not set — fill in the measure constants above.",
)
def test_cohort_statistics_real_data(sdk):
    """S2: per-patient mean signal over a 24-hour window anchored at admission.

    Cohort membership and admission timestamps come from
    EXPECTED_DEMOGRAPHIC_ADMISSIONS (pre-validated in Step 3 on s1_stats).
    Full JSON response is written to cohort_stats_real_data.log for inspection.
    """
    cohort_inputs = [
        CohortInput(
            id=_COHORT_ID_MAP[cohort_id],
            patients=[
                PatientAdmission(
                    mrn=mrn,
                    admissions=[Admission(admission_ns=ns) for ns in admissions],
                )
                for mrn, admissions in mrn_map.items()
            ],
        )
        for cohort_id, mrn_map in EXPECTED_DEMOGRAPHIC_ADMISSIONS.items()
        if cohort_id in _COHORT_ID_MAP
    ]

    request = AggregateStatisticsRequest(
        cohorts=cohort_inputs,
        measure=MeasureIdentifier(
            measure_tag=STATS_MEASURE_TAG,
            freq=STATS_FREQ,
            units=STATS_UNITS,
            freq_units=STATS_FREQ_UNITS,
        ),
        observation_window=STATS_WINDOW_NS,
        availability_threshold=STATS_AVAILABILITY_THRESHOLD,
    )

    result = compute_aggregate_statistics(sdk, request, request_id="real-s2")

    STATS_LOG_PATH.write_text(json.dumps(result.model_dump(by_alias=True), indent=2))
    print(f"\nFull result written to: {STATS_LOG_PATH}")

    print(f"\n{'='*60}")
    for cs in result.cohorts:
        name = _COHORT_NAME_MAP.get(cs.cohort_id, str(cs.cohort_id))
        print(f"\n  [{name}]  patients={cs.n_patients}  visits={cs.n_visits}  "
              f"included={cs.n_included}  excluded={cs.n_excluded}")
        for r in cs.patient_results:
            print(f"    [included]  mrn={r.mrn}  admission_ns={r.admission_ns}  mean={r.mean:.4f}")
        for e in cs.exclusions:
            print(f"    [excluded]  mrn={e.mrn}  admission_ns={e.admission_ns}  "
                  f"reason={e.reason.value}"
                  + (f"  availability={e.availability:.3f}" if e.availability is not None else ""))
    print(f"\n{'='*60}")

    assert result is not None
