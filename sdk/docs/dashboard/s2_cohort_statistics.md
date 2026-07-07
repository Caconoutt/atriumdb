# Step 2 — Aggregate Statistics Across Cohorts

This document describes the implementation plan for computing per-cohort aggregate statistics (mean, median, SD) for a given physiological measure over a fixed observation window anchored to each patient's admission time.

---

## Overview

**Inputs**
- `cohort_result` — output of the existing cohort resolver (Step 1): a list of cohorts, each with an `id` and a `mrn_list`. All MRNs are guaranteed to have at least one encounter within the cohort's admission date range — this has already been validated by the resolver and does not need to be re-checked here.
- `measure_tag` — the signal to analyse (e.g. `"MDC_ECG_CARD_BEAT_RATE"`)
- `observation_hours` — length of the observation window in hours, anchored at each patient's earliest admission (e.g. `24`)
- `availability_threshold` — minimum fraction of the window that must be covered by valid data for a patient to be included (e.g. `0.80` = 80 %)

**Output** — top-level response body:
```json
{
  "cohorts": [
    {
      "cohort_id": 1,
      "n_candidates": 180,
      "n_included": 152,
      "n_excluded": 28,
      "patient_results": [
        { "mrn": "123456", "mean": 74.1 },
        { "mrn": "234567", "mean": 78.3 }
      ]
    },
    {
      "cohort_id": 2,
      "n_candidates": 170,
      "n_included": 140,
      "n_excluded": 30,
      "patient_results": [
        { "mrn": "345678", "mean": 71.2 },
        { "mrn": "456789", "mean": 69.8 }
      ]
    }
  ]
}
```

`cohorts` contains one entry per cohort. Each `patient_results` entry is the per-patient mean of the signal over the observation window — raw enough to export and sufficient for the client to compute any statistics or render any plot (box, violin, etc.). All aggregation (mean, median, SD, quartiles, KDE) is left to the client.

---

## Step 1 — Schema Design

### Request

```python
class MeasureIdentifier(BaseModel):
    measure_tag: str        # e.g. "MDC_ECG_CARD_BEAT_RATE"
    freq:        float      # numeric frequency value
    units:       str        # e.g. "BPM"
    freq_units:  str        # e.g. "Hz", "kHz", "nHz"
    # These four are passed directly to sdk.get_measure_id() to resolve the
    # internal measure_id. Returns None if no matching measure exists.
    #
    # sdk.get_measure_id(
    #     measure_tag, freq=freq, units=units, freq_units=freq_units
    # ) -> int | None

class PatientAdmission(BaseModel):
    mrn:          str   # medical record number
    admission_ns: int   # earliest qualifying encounter start_time in Unix epoch ns
                        # already verified to fall within the admission date range

class CohortInput(BaseModel):
    id:       int                      # cohort identifier
    patients: list[PatientAdmission]   # pre-resolved patients for this cohort

class AggregateStatisticsRequest(BaseModel):
    cohorts:                list[CohortInput]   # one entry per cohort
    measure:                MeasureIdentifier
    observation_window:     int                 # window length in epoch ns, e.g. 24 h = 86_400_000_000_000
    availability_threshold: float = 0.80        # fraction in [0, 1]
    viz_type:               str   = "box"       # "box" or "violin"
```

### Response

```python
class PatientResult(BaseModel):
    mrn:  str     # patient identifier, carried through for export
    mean: float   # mean of signal values over the observation window

class CohortStatistics(BaseModel):
    cohort_id:       int
    n_candidates:    int                  # patients with a resolved patient_id
    n_included:      int                  # patients that passed all filters
    n_excluded:      int                  # all exclusions combined; detail in log file
    patient_results: list[PatientResult]  # one entry per included patient

class AggregateStatisticsResponse(BaseModel):
    cohorts: list[CohortStatistics]
```

### FastAPI endpoint

```
POST /cohort/statistics
Body: AggregateStatisticsRequest
Returns: AggregateStatisticsResponse
```

---

## Step 2 — Resolve Measure ID

Before entering the patient loop, resolve the `MeasureIdentifier` from the request to an internal `measure_id` using `sdk.get_measure_id()`. This is done once and reused for every patient.

```python
measure_id = sdk.get_measure_id(
    request.measure.measure_tag,
    freq=request.measure.freq,
    units=request.measure.units,
    freq_units=request.measure.freq_units,
)

if measure_id is None:
    raise HTTPException(
        status_code=404,
        detail=f"Measure not found: tag='{request.measure.measure_tag}', "
               f"freq={request.measure.freq} {request.measure.freq_units}, "
               f"units='{request.measure.units}'",
    )
```

If `measure_id` is `None` the measure does not exist in the dataset and the request fails immediately with a 404 — there is no point proceeding to the patient loop.

The resolved `measure_id` (and the `freq_nhz` looked up alongside it) are passed into Steps 3b and 4.

---

## Step 3 — Per-Cohort Processing

With `measure_id` in hand, iterate through each cohort in `request.cohorts`. For each cohort, run the following substeps against its `patients` list and accumulate a `CohortStatistics` result.

```python
cohort_results = []
all_included_means = []   # for overall statistics across all cohorts

for cohort in request.cohorts:
    cohort_stat = process_cohort(sdk, cohort, measure_id, freq_nhz, request)
    cohort_results.append(cohort_stat)
    all_included_means.extend(cohort_stat._included_means)  # internal, not serialised
```

### Exclusion logging

Every time a patient is filtered out at any substep, write a structured entry to a dedicated log file (path configurable, e.g. `statistics_exclusions.log`). Each entry records enough context to diagnose the exclusion without re-running:

```
[EXCLUDED] cohort_id=1  mrn=123456  patient_id=42  reason=no_device_found
           window=[1700000000000000000, 1700086400000000000]

[EXCLUDED] cohort_id=1  mrn=234567  patient_id=None  reason=mrn_not_found

[EXCLUDED] cohort_id=2  mrn=345678  patient_id=99  reason=below_availability_threshold
           availability=0.61  threshold=0.80
           window=[1700100000000000000, 1700186400000000000]
```

> **TODO — user-facing presentation:** decide whether exclusion details should also be surfaced in the API response (e.g. as an `exclusions` list per cohort) or remain log-only. During development, log-only is sufficient. Before production, consider whether the caller needs per-patient breakdown to audit results.

---

### Step 3a — Resolve Patient IDs

For each MRN in the cohort, call `sdk.get_patient_id()` to retrieve the internal integer patient ID. MRNs that return `None` (patient not found in the dataset) are logged and skipped — they do not count toward `n_candidates`.

```python
def resolve_patient_ids(sdk: AtriumSDK, mrn_list: list[str]) -> dict[str, int]:
    """
    Returns {mrn: patient_id} for every MRN that resolves successfully.
    MRNs with no matching patient are logged and excluded from further processing.
    """
    result = {}
    for mrn in mrn_list:
        patient_id = sdk.get_patient_id(mrn=mrn)
        if patient_id is None:
            _log_exclusion(cohort_id=cohort_id, mrn=mrn, patient_id=None,
                           reason="mrn_not_found")
            continue
        result[mrn] = patient_id
    return result
```

`n_candidates` for the cohort is set to `len(result)` — the number of MRNs that successfully resolved to a patient ID. MRNs that failed resolution are not counted.

---

### Step 3b — Compute Observation Window Per Patient

With `patient_id` and `admission_ns` in hand for each patient, compute the observation window boundary:

```python
window_start_ns = admission_ns
window_end_ns   = admission_ns + request.observation_window
```

Both bounds are in Unix epoch nanoseconds, consistent with all time fields in the database. The `observation_window` from the request is expressed directly in nanoseconds (e.g. 24 hours = `86_400_000_000_000`), so no unit conversion is needed.

Each patient is now represented as:

```python
{
    "patient_id":      int,
    "window_start_ns": int,   # == admission_ns
    "window_end_ns":   int,   # == admission_ns + observation_window
}
```

This tuple is passed forward into Step 3c (device resolution) and Step 3d (availability check).

---

### Step 3c — Resolve Device ID

For each patient, call `sdk.convert_patient_to_device_id()` using the patient's `patient_id` and their observation window bounds:

```python
device_id = sdk.convert_patient_to_device_id(
    start_time=window_start_ns,
    end_time=window_end_ns,
    patient_id=patient_id,
)
```

The function returns a single `int` device ID only when **one device fully encapsulates the entire observation window**. It returns `None` in all other cases — no device found, or multiple devices cover the window with no single one spanning it completely.

Patients where `device_id is None` are logged and excluded from further processing. They are counted in `n_excluded` together with all other exclusion reasons. The specific reason is captured in the log file — for now all exclusions are aggregated into a single counter; per-reason breakdown may be added later.

```python
device_id = sdk.convert_patient_to_device_id(
    start_time=window_start_ns,
    end_time=window_end_ns,
    patient_id=patient_id,
)
if device_id is None:
    _log_exclusion(cohort_id=cohort_id, mrn=mrn, patient_id=patient_id,
                   reason="no_device_found",
                   window=(window_start_ns, window_end_ns))
    n_excluded += 1
    continue
```

Patients that pass this step carry forward: `patient_id`, `device_id`, `window_start_ns`, `window_end_ns`.

---

### Step 3d — Data Availability Check

Call `sdk.get_interval_array()` to retrieve the continuous intervals of available data for this patient's device and measure within the observation window. The return value is a 2D numpy array where each row is `[interval_start_ns, interval_end_ns]`.

```python
interval_arr = sdk.get_interval_array(
    measure_id=measure_id,
    device_id=device_id,
    patient_id=patient_id,
    measure_tag=request.measure.measure_tag,
    freq=request.measure.freq,
    units=request.measure.units,
    freq_units=request.measure.freq_units,
    start=window_start_ns,
    end=window_end_ns,
)
```

Sum the covered nanoseconds across all intervals and compute the availability fraction against the full observation window:

```python
if interval_arr is None or len(interval_arr) == 0:
    covered_ns = 0
else:
    covered_ns = int(np.sum(interval_arr[:, 1] - interval_arr[:, 0]))

observation_window_ns = window_end_ns - window_start_ns
availability = covered_ns / observation_window_ns
```

Compare against the threshold and exclude if below:

```python
if availability < request.availability_threshold:
    _log_exclusion(cohort_id=cohort_id, mrn=mrn, patient_id=patient_id,
                   reason="below_availability_threshold",
                   window=(window_start_ns, window_end_ns),
                   availability=availability,
                   threshold=request.availability_threshold)
    n_excluded += 1
    continue
```

Patients that pass carry forward: `patient_id`, `device_id`, `window_start_ns`, `window_end_ns` into Step 4 (value extraction and per-patient statistics).

---

## Step 4 — Per-Patient Value Extraction and Statistics

For each patient that passed the availability check, call `sdk.get_data()` to retrieve the raw signal values within the observation window. The third element of the return tuple is the 1D numpy value array.

```python
_, _, values = sdk.get_data(
    measure_id=measure_id,
    device_id=device_id,
    patient_id=patient_id,
    start_time_n=window_start_ns,
    end_time_n=window_end_ns,
    measure_tag=request.measure.measure_tag,
    freq=request.measure.freq,
    units=request.measure.units,
    freq_units=request.measure.freq_units,
)
```

Drop any `NaN` values that may be present, then compute per-patient statistics:

```python
values = values[~np.isnan(values)]

if len(values) == 0:
    _log_exclusion(cohort_id=cohort_id, mrn=mrn, patient_id=patient_id,
                   reason="empty_values_after_nan_drop",
                   window=(window_start_ns, window_end_ns))
    n_excluded += 1
    continue

patient_mean   = float(np.mean(values))
patient_median = float(np.median(values))
patient_sd     = float(np.std(values, ddof=1)) if len(values) > 1 else None

included_means.append(patient_mean)
```

`included_means` accumulates one mean per included patient in this cohort.

---

## Step 5 — Cohort Output Assembly

After all patients in the cohort have been processed, build the `CohortStatistics` object directly from the accumulated `patient_results` list — no aggregation needed here:

```python
cohort_stat = CohortStatistics(
    cohort_id=cohort.id,
    n_candidates=n_candidates,
    n_included=len(patient_results),
    n_excluded=n_excluded,
    patient_results=patient_results,
)
```

Append to `cohort_results` and move on to the next cohort.

---

## Step 6 — Assemble Final Response

After all cohorts are processed, return the response directly — all aggregation is left to the client:

```python
return AggregateStatisticsResponse(cohorts=cohort_results)
```

---

## Notes

> **Per-patient summary statistic:** for now, `PatientResult.mean` is computed as `np.mean(values)` over all signal samples within the observation window. Mean is the conventional choice for cohort-level physiological signal analysis. If the signal is prone to artefact spikes, median may be more appropriate — this can be parameterised in a future iteration.