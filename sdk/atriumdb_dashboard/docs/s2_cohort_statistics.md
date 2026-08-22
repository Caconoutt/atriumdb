# Step 2 — Aggregate Statistics Across Cohorts

This document describes the implementation as it exists in the codebase: the schemas, the
statistics pipeline, the SDK method, the FastAPI endpoint, and the tests. It is the S2
counterpart to `s1_cohort_definition_plan.md`.

Given cohorts already resolved by S1, it computes **one mean signal value per (patient,
admission)** over an observation window anchored at each admission, and reports every entry it
dropped and why. No cross-patient aggregation happens here — mean, median, SD, quartiles and KDE
are all left to the dashboard.

---

## 0. Overview

**Inputs**
- `cohorts` — pre-resolved cohorts from the S1 resolver. Each patient carries their qualifying
  admissions, already validated against the admission date range; S2 does not re-check them.
- `measure` — the signal to analyse, identified by tag + frequency (+ optional units).
- `observation_window` — either a fixed span in nanoseconds anchored at each admission, or
  `"all_time"` to use the admission's own discharge as the end.
- `availability_threshold` — minimum fraction of the window that must be covered by usable data.
- `value_range` — optional signal bounds, globally and/or per cohort.

**Output** (camelCase on the wire — every model sets `alias_generator=to_camel`):

```json
{
  "cohorts": [
    {
      "cohortId": 1,
      "nPatients": 180,
      "nVisits": 194,
      "nIncluded": 152,
      "nExcluded": 42,
      "patientResults": [
        {"mrn": "<MRN>", "admissionNs": 1700000000000000000, "mean": 74.1,
         "sex": "M", "ageMonths": 40, "location": "ICU"}
      ],
      "exclusions": [
        {"mrn": "<MRN>", "admissionNs": 1700100000000000000,
         "reason": "below_availability_threshold",
         "windowStartNs": 1700100000000000000, "windowEndNs": 1700186400000000000,
         "availability": 0.61}
      ]
    }
  ]
}
```

Each `patientResults` entry is one **admission**, not one patient: a patient with two qualifying
admissions produces two entries, keyed by `(mrn, admissionNs)`. Every dropped entry appears in
`exclusions` with enough context to explain the drop without re-running the request.

---

## 1. File Layout

```
atriumdb/sdk/
├── atriumdb/                                    ← UNCHANGED — byte-identical to upstream main
├── atriumdb_dashboard/
│   ├── schemas.py                               ← S2 request/response models, appended to the
│   │                                              S1 models and sharing their _Base/Admission
│   ├── statistics_resolver.py                   ← the pipeline + compute_aggregate_statistics()
│   ├── api/
│   │   ├── statistics_endpoints.py              ← POST /statistics + its own SDK dependency
│   │   └── app.py                               ← mount_dashboard() attaches both routers
│   ├── docker/
│   └── docs/
└── tests/
    ├── mock_api/                                ← UNCHANGED — byte-identical to upstream main
    └── atriumdb_dashboard/
        ├── test_dashboard_statistics_api.py     ← endpoint tests against a mocked SDK
        └── test_dashboard_statistics_real_data.py  ← real-dataset run, skipped without a dataset
```

---

## 2. Schemas (`atriumdb_dashboard/schemas.py`)

All models inherit `_Base`, which sets `alias_generator=to_camel` and `populate_by_name=True` —
snake_case in Python, camelCase in JSON.

### 2.1 Request

| Model | Fields |
|---|---|
| `MeasureIdentifier` | `measure_tag: str`, `freq: float`, `units: str \| None`, `freq_units: str \| None` — passed straight to `sdk.get_measure_id()` |
| `Admission` | `admission_ns: int`, `discharge_ns: int \| None`, `location: str \| None` |
| `PatientAdmission` | `mrn: str`, `admissions: list[Admission]` |
| `ValueRange` | `lower: float \| None`, `upper: float \| None` — either end may be open |
| `ValueRangeMap` | `dict[str, ValueRange]`, keyed by measure tag |
| `CohortInput` | `id: int`, `patients: list[PatientAdmission]`, `value_range: ValueRangeMap \| None` |
| `AggregateStatisticsRequest` | `cohorts`, `measure`, `observation_window: PositiveInt \| "all_time"`, `availability_threshold: float = 0.80`, `value_range: ValueRangeMap \| None` |

`observation_window` is `PositiveInt` rather than `int` because a zero-length window gives
availability nothing to measure against.

`Admission` mirrors what the S1 resolver emits — including `location`, which S2 carries straight
through to the response rather than re-running the `encounter → bed → unit` join.

### 2.2 Response

| Model | Fields |
|---|---|
| `PatientResult` | `mrn`, `admission_ns`, `mean`, `sex \| None`, `age_months \| None`, `location \| None` |
| `ExclusionRecord` | `mrn`, `admission_ns \| None`, `reason: ExclusionReason`, `window_start_ns \| None`, `window_end_ns \| None`, `availability \| None` |
| `CohortStatistics` | `cohort_id`, `n_patients`, `n_visits`, `n_included`, `n_excluded`, `patient_results`, `exclusions` |
| `AggregateStatisticsResponse` | `cohorts: list[CohortStatistics]` |

The three demographic fields on `PatientResult` feed the Data Records table and are best-effort:
a dataset that does not record them leaves them `None` and the dashboard renders an em-dash.
Missing demographics never exclude an entry.

### 2.3 Counting semantics

| Counter | Meaning |
|---|---|
| `n_patients` | Distinct MRNs that resolved to a patient ID |
| `n_visits` | `(patient, admission)` entries entering the pipeline — admissions of resolved patients only |
| `n_included` | Entries that produced a `PatientResult` |
| `n_excluded` | `len(exclusions)` — every entry dropped at any stage |

Note that `mrn_not_found` exclusions are counted in `n_excluded` but are not in `n_visits`, since
no admission was reached for them. `n_included + n_excluded` can therefore exceed `n_visits` when
a cohort contains unresolvable MRNs. In practice the dashboard filters those out before calling
S2, so the two normally agree.

---

## 3. Entry Point (`atriumdb_dashboard/statistics_resolver.py`)

```python
def compute_aggregate_statistics(sdk, request, request_id: str) -> AggregateStatisticsResponse:
    if not request_id:
        _LOGGER.error(...)
        raise ValueError("request_id must be a non-empty string.")
    measure_id = _resolve_measure_id(sdk, request, request_id)
    ...
```

It takes the SDK as its first argument rather than being a method on it. Nothing in the pipeline
needs private SDK state — every call it makes (`get_patient_id`, `get_interval_array`,
`get_data`, `get_measure_id`, `get_patient_info`) is public API that already
exists upstream — so `atrium_sdk.py` needs no modification and merges cleanly.

`request_id` is a correlation token: every log line and every exclusion record emitted while
resolving the request is prefixed `[<request_id>]`, so a dashboard-side request can be matched
against AtriumDB's logs. An empty value is rejected before any query runs.

**Direct-DB only.** Unlike `resolve_cohort`, this has no `metadata_connection_type == "api"`
branch — an API-mode SDK cannot call it. API-mode support is not implemented.

---

## 4. Server Side

### 4.1 `atriumdb_dashboard/api/statistics_endpoints.py`

```python
@router.post("/statistics", response_model=AggregateStatisticsResponse)
async def post_cohort_statistics(
    request: AggregateStatisticsRequest,
    x_request_id: str | None = Header(default=None),
    sdk: AtriumSDK = Depends(get_sdk_instance),
):
    if not x_request_id:
        raise HTTPException(status_code=400, detail="X-Request-ID header is required and must be non-empty.")
    try:
        return compute_aggregate_statistics(sdk, request, x_request_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
```

The router is mounted with `prefix="/cohorts"` in `app.py`, so the full path is
**`POST /cohorts/statistics`**.

| Condition | Status |
|---|---|
| Success | 200 |
| Missing or empty `X-Request-ID` | 400 |
| Body fails schema validation | 422 (FastAPI) |
| Requested measure not in the dataset | 422 (`ValueError` from the resolver) |

---

## 5. The Pipeline (`atriumdb_dashboard/statistics_resolver.py`)

Entry point `compute_aggregate_statistics(sdk, request, request_id)` resolves the measure once,
then processes each cohort independently.

### 5.1 Measure resolution

```python
measure_id = sdk.get_measure_id(m.measure_tag, freq=m.freq, units=m.units, freq_units=m.freq_units)
```

`None` means the measure does not exist, and the whole request fails with `ValueError` (→ 422)
before any patient is touched. The resolved `measure_id` is reused for every entry; the tag,
freq and units are **not** passed again to the later `get_interval_array` / `get_data` calls.

### 5.2 Per-entry pipeline

For each cohort: resolve the value range in force (§6), resolve MRNs, then walk every admission
of every resolved patient.

| Step | Action | Exclusion on failure |
|---|---|---|
| 3a | `sdk.get_patient_id(mrn=...)` for each patient | `mrn_not_found` (patient-level, no admission) |
| 3b | Observation window for this admission (§5.3) | `missing_discharge_time` |
| 3c | Availability from `sdk.get_interval_array(measure_id, patient_id, start, end)` | `below_availability_threshold` |
| 3d | Values from `sdk.get_data(...)`, NaN removal, optional bounds, mean | `no_usable_values` or `below_availability_threshold` |

Both `get_interval_array` and `get_data` accept a **patient id or a device id**, and the pipeline
supplies the patient id alone. An earlier version resolved `patient_id` to a `device_id` via
`convert_patient_to_device_id` and passed both; that step has been removed. It was not merely
redundant — it narrowed results, because it returns an id only when a *single* device covers the
whole window, so a stay spanning two devices was dropped as `no_device_found` even though the
data existed. Querying by patient covers the whole stay regardless of how many devices recorded
it.

Availability at step 3c is coverage-based:

```python
covered_ns = int(np.sum(interval_arr[:, 1] - interval_arr[:, 0]))   # 0 when the array is empty
availability = covered_ns / (window_end_ns - window_start_ns)
```

### 5.3 Observation window

```python
if observation_window != "all_time":
    return admission_ns, admission_ns + observation_window
if discharge_ns is None or discharge_ns <= admission_ns:
    return None                       # → missing_discharge_time
return admission_ns, discharge_ns
```

Under `"all_time"` each admission is measured over its own stay. An open stay — or a discharge
that does not follow the admission — has no bounded window to measure availability against, so
the entry is excluded rather than guessed at. Admissions are handled independently: one
admission of a patient can be scored while another is dropped.

### 5.4 Value extraction and the mean

With no bounds in force, the window is fetched normally (3-tuple return), NaN values are dropped,
and the mean is taken over what remains. An empty array after NaN removal yields
`no_usable_values`.

With bounds in force, the call changes shape:

```python
_, values = sdk.get_data(..., return_nan_filled=True)   # 2-tuple when NaN-filled
usable = ~np.isnan(values)
if lower is not None: usable &= values >= lower
if upper is not None: usable &= values <= upper
availability = np.count_nonzero(usable) / len(values)
```

`return_nan_filled=True` makes `get_data` return every sample slot the measure's frequency
implies, with gaps as NaN — and it returns a 2-tuple rather than the usual 3.

**Why availability is recomputed here.** Out-of-range samples are treated as *absent*, not merely
skipped. `get_interval_array` is value-blind: it knows how much data exists, not how much of it
falls inside the bounds. Without this second check, an admission whose signal sat mostly outside
the plausible range would pass step 3d and produce a confident-looking mean from a handful of
samples. Failing it as `below_availability_threshold` — with the recomputed fraction attached to
the exclusion record — is the honest outcome.

### 5.5 Demographics

Fetched only for entries that survived every filter, since they exist purely to populate the
results table:

```python
info = sdk.get_patient_info(patient_id=patient_id, time=admission_ns)
sex = info.get("gender") or None
age_months = _age_months(info["dob"], admission_ns)
```

`_age_months` counts calendar months from dob to admission — `(years * 12) + months`, minus one
if the day of the month has not been reached — so a 3y 4m old is `40`. Counting on the calendar
rather than dividing a nanosecond span avoids month-length drift. A dob after the admission
indicates an inconsistent record and yields `None` rather than a negative age.

`location` is not queried at all: it comes from the `Admission` the caller supplied.

---

## 6. Value Range Resolution

Both `value_range` maps are keyed by measure tag, and **only the tag named by the request's
`measure` is consulted** — bounds keyed by any other tag are ignored.

When both the cohort and the request bound that tag, the two are **intersected** rather than one
replacing the other. The tighter bound wins at each end independently:

```python
lower = max(all supplied lowers)    # highest floor
upper = min(all supplied uppers)    # lowest ceiling
```

So a cohort can narrow the global range but never widen it. An end left open (`None`) constrains
nothing, so the other side's bound carries. When only one of the two maps has an entry it applies
alone; when neither does, the signal is unbounded and step 5.4 takes the simple path.

---

## 7. Exclusions

`ExclusionReason` is a string enum with four values: `mrn_not_found`,
`below_availability_threshold`, `no_usable_values`, `missing_discharge_time`.

Every dropped entry is recorded twice — once as an `ExclusionRecord` in the response, and once as
a log line:

```
[<request_id>]  [EXCLUDED] cohort_id=1  mrn=<MRN>  reason=below_availability_threshold  admission_ns=…  window=[…, …]  availability=0.6100
```

Optional fields are omitted from the line when they do not apply: `admission_ns` and `window` are
absent for `mrn_not_found`, `window` is absent for `missing_discharge_time`, and `availability`
appears only for `below_availability_threshold`.

Records go to a **child logger**, `atriumdb_dashboard.statistics_resolver.exclusions`, at WARNING
level. Attaching a `FileHandler` to that logger routes them to a dedicated file without mixing
them into general debug output; no file path is hardcoded.

---

## 8. Tests

### 8.1 `tests/atriumdb_dashboard/test_dashboard_statistics_api.py` — endpoint tests against a mocked SDK

Every test drives the real HTTP endpoint; only the SDK underneath is mocked, so schema
serialisation, header handling and status codes are all exercised for real.

- A module-scoped autouse fixture starts one uvicorn server on port **8124** and probes the socket
  until it accepts connections (10 s deadline) rather than sleeping a fixed interval.
- A second autouse fixture clears `app.dependency_overrides` after every test, so an SDK injected
  by one test cannot silently serve the next.
- `_mock_sdk(...)` builds a stand-in with configurable `get_measure_id`, `get_patient_id`,
  `get_interval_array`, `get_data` and `get_patient_info`, which
  lets each case target one pipeline stage without a dataset or the native library.

Coverage by group:

| Group | Cases |
|---|---|
| Happy path | single entry included; missing demographics do not exclude; two patients get independent means; multiple admissions scored separately |
| Exclusions | each of the five reasons, asserting the reason, window bounds and availability carried on the record |
| Value range | lower-only, upper-only, both bounds with NaN, out-of-range values reducing availability, global ∩ cohort intersection, a cohort failing to widen the global range, bounds keyed by another tag being ignored |
| `all_time` | open stay excluded; discharge not after admission excluded; mixed admissions where one is scored and one dropped |
| Contract | missing `X-Request-ID` → 400; unknown measure → 422 |

### 8.2 `tests/atriumdb_dashboard/test_dashboard_statistics_real_data.py` — real dataset

Runs the pipeline end-to-end through `compute_aggregate_statistics` against a mounted
AtriumDB dataset, skipped automatically when `ATRIUMDB_DATASET_LOCATION` is unset.

Cohort membership, admission timestamps, the measure identifier, the window and the threshold are
all module-level constants at the top of the file — fill them in for the dataset in hand. The
assertion is deliberately loose (the response is non-empty); the value of the test is the
artefact it writes: the full JSON response goes to `cohort_stats_real_data.log`, and a per-cohort
summary of included and excluded entries is printed to stdout under `-v -s`.

### 8.3 Running the tests

Both live behind Docker, because `AtriumSDK.__init__` refuses to construct on macOS:

```bash
# Build once, from sdk/
docker build -t atriumdb-sdk .

# Endpoint tests (no dataset needed)
docker run --rm -it -v "$(pwd):/sdk" atriumdb-sdk \
  python -m pytest tests/atriumdb_dashboard/test_dashboard_statistics_api.py -v

# Real-dataset run
./docker-run-dataset.sh python -m pytest \
  tests/atriumdb_dashboard/test_dashboard_statistics_real_data.py -v -s
```

See `dockersetup.md` for the full Docker workflow.

---

## 9. Notes

**Choice of summary statistic.** `PatientResult.mean` is `np.mean` over the usable samples in the
window. Mean is the conventional choice for cohort-level physiological signal analysis; if the
signal proves prone to artefact spikes, median would be more robust and could be parameterised on
the request. The dashboard receives per-entry values rather than pre-aggregated statistics
precisely so that this choice does not have to be final for anything computed downstream.

**API mode.** `compute_aggregate_statistics` is direct-DB only. If the dashboard ever needs to
call an AtriumDB server over HTTP rather than embedding the SDK, this method needs the same
`metadata_connection_type == "api"` branch that `dashboard_resolve_cohort` has.
