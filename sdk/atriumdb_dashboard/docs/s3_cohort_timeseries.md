# Step 3 — Per-Interval Time-Series Across Cohorts

This document describes the implementation plan for a **time-series** view of a physiological measure across cohorts. It takes the same cohort input as the Step 2 statistics endpoint, but instead of a single per-patient summary over one observation window, it chops each patient's window into fixed-width intervals (e.g. 5 / 10 / 15 min) and reports a per-patient mean **per interval**, so the client can plot each cohort's signal over time.

It reuses the Step 2 request/patient/measure models and pipeline stages wherever possible; only the request additions (the interval width) and the response grouping (by interval bucket) are new.

> **Status.** Sections 0–7 have been re-checked against the merged `atriumdb_dashboard` package as it exists on this branch. Where the original design assumed something the code does not do — notably a device-resolution stage — the plan below follows the code, and each such correction is called out inline.

---

## 0 — Prerequisite: satisfied

The original plan opened with a gate: the models it reuses lived only on `s2_cohort_stats`, and S3 could not start until that merged. **That gate is now closed.** `s2_cohort_stats` merged (`eeb1d88`), and the dashboard was extracted out of the SDK package (`8b4f645`, `526beee`) into a sibling package. Two consequences for every import path in this document:

| Original plan said | Actual location |
|---|---|
| `atriumdb/dashboard/schemas.py` | `sdk/atriumdb_dashboard/schemas.py` |
| `atriumdb/dashboard/statistics_resolver.py` | `sdk/atriumdb_dashboard/statistics_resolver.py` |
| `dashboard/pipeline.py` (proposed) | `sdk/atriumdb_dashboard/pipeline.py` (still to be created — [§4.0](#40--extracting-the-shared-stages)) |

The package rule that governs the whole layer: **`atriumdb_dashboard` imports from `atriumdb`, never the reverse**, so `sdk/atriumdb/` and `sdk/tests/mock_api/` stay byte-identical to upstream `main`. S3 adds only new files under `atriumdb_dashboard/` plus two small edits to existing dashboard files; it touches nothing upstream.

### 0.1 — What the merged code actually does (and where the plan was wrong)

Three corrections, all stemming from `690c4ed remove device id in pipeline`:

1. **There is no device-resolution stage.** `statistics_resolver.py` queries `get_interval_array` and `get_data` by `patient_id` directly. The original plan listed "device resolution" as a reused pipeline stage; it does not exist and must not be reintroduced.
2. **`ExclusionReason` has four members, not five.** The live enum is `MRN_NOT_FOUND`, `BELOW_AVAILABILITY_THRESHOLD`, `NO_USABLE_VALUES`, `MISSING_DISCHARGE_TIME`. There is no `no_device_found`. Every example payload below has been corrected.
3. **Therefore S3's `patient_exclusions` can carry exactly one reason today: `mrn_not_found`.** See [§3.4](#34--why-patient_exclusions-is-single-reason-today) — the two-location exclusion design still holds, but the plan should not pretend the pre-bucketing bucket is richer than it is.

---

## 1 — Endpoint decision: separate endpoint

**Decision: a new endpoint, `POST /cohorts/timeseries`, not a `vizType` branch on `/cohorts/statistics`.** Unchanged from the original plan.

The instinct to keep one endpoint and switch on `vizType` (box / violin → S2, time-series → S3) was considered and rejected, for reasons that go beyond the viz label:

| | `/cohorts/statistics` (S2) | `/cohorts/timeseries` (S3) |
|---|---|---|
| Extra required input | — | `interval_ns` (meaningless for box/violin) |
| Availability evaluated | once, over the whole window | **per interval** |
| Response shape | flat `patient_results` per cohort | **grouped into interval buckets** |
| Per-patient output | one mean | one mean **per interval** |

A single endpoint would need a request model where `interval_ns` is conditionally required, a response that is a union of two unrelated shapes, and internal `if vizType == ...` branching over the parts that genuinely differ (availability, bucketing, assembly). That is the "glue together and it gets messy" outcome. OpenAPI/client codegen also degrades: a union response is far worse to consume than two named response models.

**What the two endpoints share is internal, not the endpoint.** Measure resolution, patient-ID resolution, observation-window computation, value-range resolution and demographics are identical. Those get **extracted into `atriumdb_dashboard/pipeline.py`** and called by both resolvers ([§4.0](#40--extracting-the-shared-stages)). Share the pipeline stages; keep the endpoints, request models, and response models separate.

Router entry, mirroring `statistics_endpoints.py` exactly (own module, `router = APIRouter()`, mounted under the shared `COHORT_PREFIX = "/cohorts"` by `api/app.py`):

```
POST /cohorts/timeseries
Body: TimeSeriesRequest
Returns: TimeSeriesResponse
```

`/cohorts/timeseries` is a literal path and nothing upstream serves it, so `_mount_router`'s front-insertion has nothing to disambiguate here — it applies uniformly regardless.

---

## 2 — Overview

**Inputs** — same cohort/measure/window inputs as S2, plus one:

- `cohorts` — pre-resolved cohorts from the Step 1 resolver (`CohortInput`), each with patients, their qualifying admissions, and an optional per-cohort `value_range`. **Reused unchanged.**
- `measure` — the signal to analyse (`MeasureIdentifier`). **Reused unchanged.**
- `observation_window` — fixed window length in epoch ns, anchored at each admission (e.g. 24 h). See the [all_time decision](#52--observation-window-fixed-only-for-v1).
- **`interval_ns`** *(new)* — bucket width in epoch ns (e.g. 5 min = `300_000_000_000`). The window is chopped into consecutive intervals of this width.
- `availability_threshold` — minimum covered fraction, applied **per interval, and only per interval**. Unlike S2, S3 has **no window-level availability gate**: an entry is never dropped for being sparse across the observation window as a whole. It enters bucketing as long as it resolves a patient and a window, and its availability is then judged independently in each interval. A visit with data in only 3 of 288 intervals therefore appears in `patient_results` for those 3 and in `exclusions` for the other 285 — it is not excluded at the entry level.
- `value_range` — optional signal bounds (`ValueRangeMap`), same semantics as S2: out-of-range samples are treated as absent and reduce availability. **Reused unchanged**, applied per interval.

**Interval convention:** interval `i` covers `[admission_ns + i·interval_ns, admission_ns + (i+1)·interval_ns)`, for `i` in `0 … (observation_window / interval_ns) − 1`. So interval `0` is the first 5 min of the stay, interval `1` the next 5 min, and so on — offsets from admission, identical across all patients in a cohort (which is what makes them plottable on one shared x-axis).

**Output** — grouped by interval within each cohort, with **patient demographics normalised into a per-cohort `visits` table** (see [2.1](#21--why-demographics-are-normalised)):

```json
{
  "cohorts": [
    {
      "cohortId": 1,
      "nPatients": 180,
      "nVisits": 205,
      "visits": [
        { "mrn": "123456", "admissionNs": 1700000000000000000,
          "sex": "M", "ageMonths": 41, "location": "ICU" },
        { "mrn": "234567", "admissionNs": 1700000000000000000,
          "sex": null, "ageMonths": null, "location": null },
        { "mrn": "345678", "admissionNs": 1700000000000000000,
          "sex": "F", "ageMonths": 7, "location": "CCU" },
        { "mrn": "456789", "admissionNs": 1700000000000000000,
          "sex": "F", "ageMonths": 132, "location": "ICU" }
      ],
      "patientExclusions": [
        { "visit": 1, "reason": "mrn_not_found" }
      ],
      "intervals": [
        {
          "intervalIndex": 0,
          "startOffsetNs": 0,
          "endOffsetNs": 300000000000,
          "nIncluded": 2,
          "nExcluded": 1,
          "patientResults": [
            { "visit": 0, "mean": 74.1 },
            { "visit": 2, "mean": 81.6 }
          ],
          "exclusions": [
            { "visit": 3, "reason": "below_availability_threshold", "availability": 0.32 }
          ]
        },
        {
          "intervalIndex": 1,
          "startOffsetNs": 300000000000,
          "endOffsetNs": 600000000000,
          "patientResults": [ ... ],
          "exclusions": [ ... ]
        }
      ]
    }
  ]
}
```

Each interval carries the per-patient means that survived for *that* interval, plus the entries excluded *in that interval*. Both reference a patient by **`visit`** — a 0-based index into the enclosing cohort's `visits` array — rather than repeating the MRN, admission timestamp and demographics. All cross-patient aggregation (per-interval cohort mean, CI bands, etc.) is left to the client, exactly as in S2.

Note the counting relationship in this example: `visits` has 4 entries, one of which (`visit: 1`) failed MRN resolution, so `nVisits` is 3 while `len(visits)` is 4, and every interval's `nIncluded + nExcluded == 3`.

### 2.1 — Why demographics are normalised

Demographics are **demographics-at-admission**: for a given `(mrn, admissionNs)` entry, `sex` / `ageMonths` / `location` are constant for the whole stay. Repeating them on every interval is therefore lossless to remove, and the repetition is substantial. For a 24 h window at 5 min intervals with 205 visits, the response holds 288 × 205 ≈ **59,000** per-patient result objects. Inline, each is ~110 bytes:

```json
{"mrn":"123456","admissionNs":1700000000000000000,"mean":74.1,"sex":"M","ageMonths":41,"location":"ICU"}
```

of which ~90 bytes is byte-identical across all 288 copies — `admissionNs` alone is a 19-digit integer repeated once per interval. Normalised, the same row is `{"visit":0,"mean":74.1}`, ~20 bytes: roughly an **80% reduction** in response size, per cohort.

**Why the index is `(mrn, admissionNs)` and not `mrn`.** A readmitted patient has a different `ageMonths` and often a different `location` at each admission, and §6 requires two admissions for one MRN to produce independent bucket series. Keying the table by MRN alone would collapse those into whichever admission was written last. The `visits` entry *is* the `(patient, admission)` entry, one-to-one with what S2 calls the entry key.

**Why an integer index and not a composite string key.** A `"mrn|admissionNs"` map key costs ~28 bytes per row and a string concat plus hash lookup on every one of the ~59,000 rows. A positional index is ~10 bytes and resolves as `visits[row.visit]`. The client needs no rehydration pass: the table renderer already iterates rows to draw them, so it resolves each row's demographics at draw time — and only for rows actually on screen.

**Index stability.** `visits` contains *every* `(patient, admission)` entry that entered the pipeline, **including entries later excluded** — `patientExclusions` references indices too. Indices must be assigned in one pass over the cohort's input entries **before** any exclusion runs, so an index is simply the entry's position in the request. Appending to `visits` as a side effect of *successful* processing would let a mid-list drop shift every later index, and is the one way to get this wrong. Note the consequence: `len(visits) ≥ nVisits`, since `nVisits` counts entries that reached bucketing while `visits` also carries the pre-bucketing drops.

### 2.2 — The entry enumeration rule

"Position in the request" needs one precise definition, because `visits` is entry-scoped while S2's `mrn_not_found` is patient-scoped. The rule:

> Iterate `cohort.patients` in request order; within each, iterate `patient.admissions` in request order; emit one `VisitInfo` per admission. A patient with **no** admissions emits one placeholder entry with `admission_ns = null`.

So an unresolvable MRN carrying two admissions produces **two** `visits` entries and **two** `patient_exclusions` records, both `mrn_not_found`. This is the one place S3 deliberately differs from S2's exclusion cardinality (S2 logs `mrn_not_found` once per MRN), and it is forced by the index contract: every `visits` row must be referencable, so every row that was dropped needs its own record.

The empty-admissions placeholder is defensive only — S1's resolver never emits a `PatientAdmission` without at least one qualifying admission — but it is the sole reason `VisitInfo.admission_ns` is nullable, so the branch should exist rather than let a malformed input raise.

---

## 3 — Schema design

All new models go in `atriumdb_dashboard/schemas.py`, appended below the existing S2 block and sharing the same `_Base` (which supplies the `to_camel` alias generator and `populate_by_name`). No new base class, no new module.

### 3.0 Reuse map at a glance

| From `atriumdb_dashboard.schemas` | How S3 uses it |
|---|---|
| `_Base` | Base class for every new S3 model — camelCase JSON for free |
| `MeasureIdentifier` | `TimeSeriesRequest.measure`, verbatim |
| `Admission`, `PatientAdmission` | Reached through `CohortInput`, verbatim |
| `CohortInput` | `TimeSeriesRequest.cohorts`, verbatim (incl. its per-cohort `value_range`) |
| `ValueRange`, `ValueRangeMap` | `TimeSeriesRequest.value_range`, verbatim |
| `ExclusionReason` | `VisitExclusion.reason`, verbatim — **no new members needed** |
| `ExclusionRecord` | *Not* imported; re-containered as `VisitExclusion` ([3.1b](#31b-reused-as-field-types-but-re-containered)) |
| `PatientResult` | *Not* imported; re-containered as `VisitMean` + `VisitInfo` |
| `AggregateStatisticsRequest` | *Not* subclassed — see [3.2](#32-new-request-model) |
| `ALL_TIME` | Not used; S3 has no `all_time` ([5.2](#52--observation-window-fixed-only-for-v1)) |

### 3.1 Reused unchanged

Imported directly from `atriumdb_dashboard.schemas`:

- `MeasureIdentifier`
- `Admission`, `PatientAdmission`
- `CohortInput`
- `ValueRange`, `ValueRangeMap`
- `ExclusionReason` — the existing **four** reasons cover S3: `mrn_not_found` and `missing_discharge_time` (entry-level), `below_availability_threshold` and `no_usable_values` (now evaluated per interval). *Correction: the original plan named a fifth, `no_device_found`; that reason does not exist in the enum and the stage that would produce it was removed in `690c4ed`.*

### 3.1b Reused as field types, but re-containered

`ExclusionRecord` and `PatientResult` are **not** reused as-is, because both carry `mrn` / `admission_ns` / demographics inline — exactly the fields [2.1](#21--why-demographics-are-normalised) normalises out. S3 declares slimmer equivalents (`VisitMean`, `VisitExclusion`) that reference a visit by index; the field-level types and semantics are unchanged. This mirrors the choice already made for `TimeSeriesRequest` in [3.2](#32-new-request-model): reuse the field types, own the container.

The per-interval exclusion also drops `window_start_ns` / `window_end_ns` — for an interval-level exclusion those are exactly the enclosing `IntervalResult`'s `start_offset_ns` / `end_offset_ns`, so carrying them per record is pure duplication.

### 3.2 New request model

```python
class TimeSeriesRequest(_Base):
    cohorts: list[CohortInput]
    measure: MeasureIdentifier
    observation_window: PositiveInt          # fixed only — no "all_time" in v1 (see 5.2)
    interval_ns: PositiveInt                 # bucket width, e.g. 5 min = 300_000_000_000
    availability_threshold: float = 0.80     # applied PER interval
    value_range: ValueRangeMap | None = None
```

Deliberately **not** a subclass of `AggregateStatisticsRequest`: its `observation_window` type differs (`PositiveInt` vs `PositiveInt | Literal["all_time"]`), and inheriting a field only to narrow it is more confusing than declaring the four shared fields again. The field-level *types* are reused; the container is its own.

**Validation** — a single `model_validator(mode="after")` covering two checks, both surfacing as `422` through FastAPI's normal Pydantic handling (no endpoint code needed):

1. `observation_window % interval_ns != 0` → reject ([5.1](#51--windowinterval-divisibility)).
2. `observation_window // interval_ns > MAX_INTERVALS` → reject. **New, not in the original plan.** The response is O(intervals × visits), so a 24 h window at 1 s buckets is 86,400 buckets × 205 visits ≈ 17.7 M rows — enough to exhaust memory server-side before anything is serialised. A module constant `MAX_INTERVALS = 2_000` leaves the intended range comfortably clear (24 h at 1 min = 1,440) while making the pathological request a validation error rather than an OOM. Pick the number deliberately at implementation time; the point is that some cap exists.

### 3.3 New response models

```python
class VisitInfo(_Base):
    """One (patient, admission) entry. Demographics are at-admission and
    constant for the stay, so they live here, once, not on every interval."""
    mrn: str
    admission_ns: int | None               # null only for a patient with no admissions (2.2)
    sex: str | None = None                 # best-effort; absent => null
    age_months: int | None = None
    location: str | None = None

class VisitMean(_Base):
    visit: int                             # 0-based index into CohortTimeSeries.visits
    mean: float

class VisitExclusion(_Base):
    visit: int                             # 0-based index into CohortTimeSeries.visits
    reason: ExclusionReason
    availability: float | None = None      # this interval's coverage, when relevant

class IntervalResult(_Base):
    interval_index: int                    # 0-based
    start_offset_ns: int                   # i * interval_ns, offset from admission
    end_offset_ns: int                     # (i+1) * interval_ns
    n_included: int
    n_excluded: int
    patient_results: list[VisitMean]       # per (patient, admission) mean in this interval
    exclusions: list[VisitExclusion]       # entries dropped in THIS interval only

class CohortTimeSeries(_Base):
    cohort_id: int
    n_patients: int                        # distinct MRNs that resolved to a patient_id
    n_visits: int                          # (patient, admission) entries that reached bucketing
    visits: list[VisitInfo]                # ALL entries, incl. excluded; index order == request order
    patient_exclusions: list[VisitExclusion]   # dropped before bucketing (whole entry)
    intervals: list[IntervalResult]

class TimeSeriesResponse(_Base):
    cohorts: list[CohortTimeSeries]
```

`VisitInfo` / `VisitMean` / `VisitExclusion` form a deliberate trio: every per-patient row in the response is keyed by the same `visit` index, so the client has exactly one resolution rule to learn. `visits` is **cohort-scoped, not response-scoped** — cohorts may overlap, but a shared global table would couple them and stop each cohort being independently consumable; duplication across cohorts is bounded by cohort count, not by interval count.

`n_patients` and `n_visits` keep S2's `CohortStatistics` definitions exactly (`n_patients` = MRNs that resolved; `n_visits` = admissions belonging to those patients), so the two endpoints' cohort headers stay directly comparable.

**Why two exclusion locations** — this is the one genuinely new modelling decision:

- An `mrn_not_found` (or, if `all_time` ever arrives, `missing_discharge_time`) drop removes the **entire (patient, admission) entry** from every interval. Recording it once per interval would repeat it 288 times for a 24 h / 5 min request. These go in `CohortTimeSeries.patient_exclusions`.
- A `below_availability_threshold` or `no_usable_values` drop is **specific to one interval** — the same patient may be present in interval 0 and absent in interval 40 because their data ran out. These go in the relevant `IntervalResult.exclusions`.

So a patient present overall but sparse in places appears in `patient_results` for the intervals they cover and in `exclusions` for the intervals they don't. `n_patients` / `n_visits` count what entered bucketing; per-interval `n_included` / `n_excluded` count that interval.

### 3.4 — Why `patient_exclusions` is single-reason today

Trace the four reasons through the S3 pipeline as the merged code actually implements it:

| Reason | Where it can fire in S3 |
|---|---|
| `mrn_not_found` | `patient_exclusions` — the **only** reason reachable there |
| `missing_discharge_time` | Nowhere. `_observation_window` returns `None` only under `ALL_TIME`, which [5.2](#52--observation-window-fixed-only-for-v1) forbids; a `PositiveInt` window always bounds |
| `below_availability_threshold` | `IntervalResult.exclusions` only |
| `no_usable_values` | `IntervalResult.exclusions` only |
| ~~`no_device_found`~~ | Does not exist — no device stage, no enum member |

Keep the two-location structure anyway. It costs one list field, it is the correct shape the moment `all_time` or any other entry-level gate arrives, and collapsing it now would mean reshaping the response later. But do not write client code, tests, or docs that imply `patient_exclusions` is heterogeneous today — assert the single reason explicitly ([§6](#6--testing-outline)) so the claim stays true or fails loudly.

---

## 4 — Processing pipeline

Reuses S2 stages verbatim (measure resolution → patient-ID resolution → observation window), then diverges once a window is in hand. **There is no device-resolution stage** — `get_interval_array` and `get_data` take `patient_id` directly, as they already do in `statistics_resolver.py`.

### 4.0 — Extracting the shared stages

The reuse the original plan asked for is real but needs one refactor first: S2's helpers are private and several take the whole `AggregateStatisticsRequest`, which S3 cannot construct. The fix is to make each shared helper **request-model-agnostic** — take the scalars it actually needs — and move it to a new `atriumdb_dashboard/pipeline.py` that both resolvers import. `statistics_resolver.py` then calls through to it; **S2's observable behaviour must not change**, which is what its existing test suite verifies.

| Helper in `statistics_resolver.py` | Reusable? | Action |
|---|---|---|
| `_observation_window(admission, observation_window)` | **Yes, verbatim** | Move to `pipeline.py` unchanged. Already takes scalars. Handles `ALL_TIME` — S3 simply never passes it |
| `_age_months(dob_ns, admission_ns)` | **Yes, verbatim** | Move to `pipeline.py` unchanged |
| `_fetch_demographics(sdk, patient_id, mrn, admission_ns, request_id)` | **Yes, verbatim** | Move to `pipeline.py` unchanged. Best-effort: returns `(None, None)` and never raises |
| `_resolve_measure_id(sdk, request, request_id)` | After a signature change | Retake as `resolve_measure_id(sdk, measure: MeasureIdentifier, request_id)`. It only reads `request.measure` |
| `_resolve_value_range(cohort, request, request_id)` | After a signature change | Retake as `resolve_value_range(cohort, measure_tag: str, global_range: ValueRangeMap \| None, request_id)`. It only reads `request.measure.measure_tag` and `request.value_range`. The global ∩ cohort intersection logic is unchanged and is exactly what S3 needs |
| `_resolve_patient_ids(sdk, cohort, request_id, exclusions)` | After a shape change | Currently appends `ExclusionRecord`s into a caller-supplied list — S3 needs `VisitExclusion`s keyed by index. Retake as `resolve_patient_ids(sdk, cohort) -> dict[str, int]`, returning `{mrn: patient_id}` for what resolved; each caller derives its own record type from the MRNs *not* in the map. This is the cleanest split: the SDK loop is shared, the record construction is not |
| `_extract_patient_mean(...)` | **No** | Whole-window, single-mean, and does its own availability recount. S3 replaces it ([4.1](#41--fetch-once-bucket-in-memory)). Extract only the masking rule below |
| *(new)* `usable_mask(values, value_range)` | — | The three-line `~isnan & >= lower & <= upper` currently inlined in `_extract_patient_mean`'s bounded branch. Lift it to `pipeline.py`; S2's bounded path and every S3 interval then apply byte-identical range semantics |
| `_make_exclusion(...)` | **No** | Builds `ExclusionRecord` and writes the S2 log line. S3 gets its own `_make_visit_exclusion` in `timeseries_resolver.py`, logging to its own `…timeseries_resolver.exclusions` child logger so the two endpoints' audit streams stay separable |

Keep the extracted names public (no leading underscore) in `pipeline.py`, since they now cross a module boundary by design.

### 4.1 — Fetch once, bucket in memory

```
resolve measure_id once for the request        (pipeline.resolve_measure_id)
validate freq_nhz > 0 for that measure         (see 4.3)
period_ns = sdk.get_measure_info(measure_id)["period_ns"]
n_intervals = observation_window // interval_ns

for each cohort:
  value_range = pipeline.resolve_value_range(cohort, measure_tag, request.value_range, request_id)
  mrn_to_pid  = pipeline.resolve_patient_ids(sdk, cohort)

  # PASS 1 — build the visit table first, so indices cannot shift (see 2.1, 2.2)
  for each (patient, admission) entry, in request order:
      visits.append(VisitInfo(mrn, admission_ns, *demographics))
      # demographics only when the MRN resolved; nulls otherwise

  # PASS 2 — process; every record refers back by index
  for v, entry in enumerate(cohort entries):
     if entry.mrn not in mrn_to_pid:
         patient_exclusions.append(VisitExclusion(visit=v, reason=MRN_NOT_FOUND)) ; continue
     window = pipeline.observation_window(admission, request.observation_window)   # never None here
     
     ── S3-specific from here ──
     _, values = sdk.get_data(measure_id, patient_id, window_start, window_end,
                              return_nan_filled=True)          # ONE call, regular grid
     usable = pipeline.usable_mask(values, value_range)
     bucket boundaries b_i = round(i * interval_ns / period_ns)
     for each interval i in 0 … n_intervals-1:
         slice = usable[b_i : b_{i+1}]
         availability = count_nonzero(slice) / len(slice)
         if availability < threshold:  intervals[i].exclusions.append(VisitExclusion(v, BELOW_…, availability))
         elif slice is all-absent:     intervals[i].exclusions.append(VisitExclusion(v, NO_USABLE_VALUES))
         else:                         intervals[i].patient_results.append(VisitMean(v, mean))
```

**Note the two passes.** Demographics are looked up once per entry while building `visits`, not once per interval — so normalising the response also removes ~288× redundant work from assembly, not just from the wire. Pass 1 must not be folded into pass 2: appending to `visits` only for entries that survive would make an index depend on how many earlier entries were dropped. `resolve_patient_ids` runs *before* pass 1 precisely so pass 1 can fetch demographics for resolved MRNs without reordering anything.

**One SDK data call per entry, not two.** This is a deliberate simplification over both S2 and the original S3 plan, which each called `get_interval_array` *and* `get_data`:

- `get_data(..., return_nan_filled=True)` returns a **2-tuple** `(headers, values)` — not the usual 3-tuple — where `values` is a regular grid of `round((end − start) / period_ns)` samples with gaps filled as `NaN` (`atrium_sdk.py:534-569`). S2's mock already models this two-shape return, so the test scaffolding carries over.
- On that grid, coverage *is* `count_nonzero(usable) / len(slice)`. There is no need to ask `get_interval_array` how much data exists and then re-derive availability from the values array — the second computation subsumes the first. S2 keeps both only because its unbounded path avoids NaN-filling; S3 always NaN-fills, so it has one array and one rule.
- Sample index → interval index is pure integer arithmetic on that grid — no per-sample timestamp lookup, and no timestamps are returned in this mode anyway.

**Risk to check on real data:** the NaN-filled grid measures coverage against the measure's *nominal* period, whereas `get_interval_array` measures wall-clock spans of stored blocks. For a well-behaved periodic signal these agree; for a signal with duplicate or irregular timestamps they can drift. Validate against the real dataset ([§6](#6--testing-outline)); if they disagree materially, fall back to the original plan's two-call form — one `get_interval_array` over the window, intersected per bucket by broadcasting `np.clip` over the (N,1)×(M,2) grid — and use the values array for the mean only.

**Bucketing mechanics.** Two details worth settling before writing the loop:

- Compute boundaries as `b_i = round(i * interval_ns / period_ns)` rather than a fixed `samples_per_interval` stride. When `interval_ns` is not a whole multiple of `period_ns` (a 5 min bucket on a 0.7 Hz signal, say), rounding still yields a contiguous, non-overlapping, exhaustive partition, whereas a fixed stride silently drops a tail. **No validator for `interval_ns % period_ns`** — the divisibility rule in [5.1](#51--windowinterval-divisibility) is about window/interval only, and requiring bucket widths to divide the sample period would couple the API to per-measure metadata the caller does not have.
- Prefer `np.bincount(bucket_index[usable], weights=values[usable], minlength=n_intervals)` over `np.add.reduceat` for the per-bucket sums. `reduceat` returns the element at the index — not zero — when two consecutive boundaries coincide, which is exactly what happens for an empty bucket, and that failure is silent. `bincount` handles empty buckets natively.

**Order of the two interval-level reasons.** Check the threshold first, then all-absent, matching `_extract_patient_mean`'s bounded branch. With any `availability_threshold > 0` an all-absent bucket fails the threshold first, so `no_usable_values` is reachable only when the threshold is `0` — same as in S2. Preserving the order keeps the two endpoints' reason semantics identical.

### 4.2 — Per-interval availability + value_range

Identical rule to S2, scoped to the interval: coverage is measured against `interval_ns` (the bucket width), and out-of-range / NaN samples count as absent. A patient whose signal is mostly artefact in interval 3 fails `availability_threshold` for interval 3 only. Sharing `usable_mask` with S2 is what guarantees the "out-of-range ⇒ absent, not averaged" semantic stays identical across the two endpoints rather than drifting.

**The denominator is `interval_ns`, never `observation_window`.** In S2 the threshold answers "is this visit a usable member of the cohort?" — one whole-window judgement, one drop. In S3 it answers a different question, "is this visit's mean in *this bucket* trustworthy?", and answers it N times independently. The two are not the same test at different scales: 80% coverage of 24 h and 80% coverage of a 5 min bucket are different requirements, and the per-interval one is far more volatile. S3 deliberately asks only the second question.

**Consequence — the included population varies by interval.** Because there is no entry-level gate, each interval's cohort mean is computed over whatever set survived *that* interval, and that set generally shrinks over the series as patients are discharged or come off monitoring. `IntervalResult.n_included` exists precisely so this is visible: the client should carry it alongside each plotted point (as an n-per-bucket trace, band width, or tooltip), because a cohort mean plotted without its denominator will read as a trend when part of it is just a changing population. Note this is the same raggedness [5.2](#52--observation-window-fixed-only-for-v1) rejects `all_time` for — a fixed window bounds the bucket *grid* so every cohort shares an x-axis, but it does not make the per-bucket denominators equal.

### 4.3 — Aperiodic measures

`queries.py` already documents that a measure can carry `freq_nhz = 0` — aperiodic and annotation signals, which have no sampling period. Such a measure has no regular grid, so `return_nan_filled=True` has nothing to fill against and `period_ns` is undefined (a division by zero, or a nonsense `period_ns` from the SDK cache).

**Resolve the measure, then reject `freq_nhz == 0` with a `422`**, in the same place and the same way `resolve_measure_id` already raises `ValueError` for an unknown measure — the endpoint's `except ValueError → HTTPException(422)` wrapper turns it into a clean client error. S2 is unaffected (its unbounded path never NaN-fills), so this check belongs in `timeseries_resolver.py`, not in `pipeline.py`.

---

## 5 — Decisions and cross-component guarantees

All six decisions below are **settled**. They are collected here rather than left inline because each one is only correct if a component *outside* this endpoint upholds its half — the dashboard frontend, the dashboard server, or the S3 resolver itself. Treat this section as the contract to check before and during implementation; the detailed rationale for each lives in the linked section.

| # | Decision | AtriumDB endpoint guarantees | Other component guarantees |
|---|---|---|---|
| [5.1](#51--windowinterval-divisibility) | `observation_window % interval_ns == 0` | Rejects violations with `422` | **Frontend** blocks the request until the selection divides evenly |
| [5.2](#52--observation-window-fixed-only-for-v1) | Fixed window only; no `"all_time"` | `TimeSeriesRequest.observation_window` is `PositiveInt`, so `"all_time"` cannot type-check | **Frontend** removes the `all_time` option when `vizType` is time-series |
| [5.3](#53--interval-units-at-the-api-boundary) | Accepts `interval_ns` in nanoseconds only | — | **Dashboard server** converts the user's 5 / 10 / 15 min selection to ns before calling |
| [5.4](#54--empty-intervals) | Fully-excluded intervals are still emitted | Interval indices are always dense, `0 … N−1` | **Frontend** plots nothing for that index and lists its exclusions in the table |
| [5.5](#55--normalised-visit-table) | Demographics normalised into a per-cohort `visits` table | Every `visit` index is valid and stable against exclusions | **Frontend** resolves `visits[row.visit]` at render time |
| [5.6](#56--availability-threshold-is-per-interval-only) | Threshold is per-interval only; no window-level gate | Never drops an entry for whole-window sparseness | **Frontend** displays `n_included` alongside each plotted point |

### 5.1 — Window/interval divisibility

**Decision: require `observation_window % interval_ns == 0`, else `422`.** Guarantees every interval is exactly `interval_ns` wide, so availability fractions are comparable across buckets and the bucket count is unambiguous. The alternative — allowing a partial trailing bucket with availability measured against its shorter actual width — is defensible but adds an edge case to every downstream consumer for little gain. Revisit only if the dashboard needs arbitrary window lengths.

**Confirmed guarantee — frontend:** validates the window/interval combination in the UI and does not issue the request until the user resolves it, so a `422` here indicates a frontend bug rather than ordinary user error. The server-side check stays regardless: it is the contract for any other client, and a validation that exists only in the UI is not a validation.

Note this constrains `interval_ns` against `observation_window` only, **not** against the measure's sample period — see the bucketing note in [4.1](#41--fetch-once-bucket-in-memory).

### 5.2 — Observation window: fixed only for v1

**Decision: v1 accepts a fixed `observation_window` only; no `"all_time"`.** Under `all_time` each patient's window is `admission → discharge`, so stay length — and therefore interval count — varies per patient. The buckets would be *ragged*: interval 40 would contain only patients whose stay was long enough, silently biasing every late interval toward longer-staying (typically sicker) patients. A time-series meant to be plotted on one shared x-axis needs a common bucket grid, which a fixed window gives and `all_time` does not. If `all_time` is needed later, it needs an explicit decision about how to present ragged tails (truncate to the shortest stay? report per-interval denominators?) — out of scope for v1.

**Confirmed guarantee — frontend:** the `all_time` observation-window option is removed from the picker when `vizType` is time-series, so the combination is unreachable in the UI. On the API side it is unrepresentable rather than validated: `observation_window` is typed `PositiveInt`, so `"all_time"` fails schema parsing before any resolver code runs.

Note this bounds the bucket *grid* only. It does not make per-interval denominators equal — see [5.6](#56--availability-threshold-is-per-interval-only). It is also why `missing_discharge_time` is unreachable in S3 ([3.4](#34--why-patient_exclusions-is-single-reason-today)): the shared `observation_window` helper returns `None` only on the `all_time` branch.

### 5.3 — Interval units at the API boundary

**Decision: schema carries `interval_ns` (nanoseconds); the dashboard server converts the user's "5 / 10 / 15 min" selection to ns before calling**, exactly as it already pre-converts `AgeBand` and `observation_window`. Keeps AtriumDB's boundary uniformly nanosecond-based and unit-conversion-free.

**Confirmed guarantee — dashboard server:** performs the minutes → ns conversion. AtriumDB never sees a minutes-denominated value and does no unit inference; a request carrying `interval_ns: 5` is interpreted literally as 5 nanoseconds, not 5 minutes, and will simply produce a degenerate result rather than an error. This is the one item in this table with no server-side backstop — though the `MAX_INTERVALS` cap from [3.2](#32-new-request-model) now catches the *worst* miscalibration (a minutes value read as ns explodes the bucket count), it does not catch every one, so the conversion still belongs in a single shared helper on the dashboard side rather than at each call site.

### 5.4 — Empty intervals

**Decision: an interval where *every* entry was excluded is still emitted** (empty `patient_results`, populated `exclusions`), so the client sees a real gap in the series rather than a missing index. Interval indices are therefore always dense and complete: `0 … (observation_window / interval_ns) − 1`, with no holes.

**Confirmed guarantee — frontend:** plots no point for such an interval (a visible gap in the line, not an interpolated segment across it) and surfaces that interval's `exclusions` in the table, so the user can see *why* the series is absent there rather than inferring a data outage.

### 5.5 — Normalised visit table

**Decision: per-patient demographics are sent once per visit in `CohortTimeSeries.visits`; each per-interval row references them by integer `visit` index.** Full rationale, sizing, and the index-stability rule in [2.1](#21--why-demographics-are-normalised); the enumeration rule in [2.2](#22--the-entry-enumeration-rule); models in [3.3](#33-new-response-models).

**Guarantee — S3 resolver:** `visits` holds every `(patient, admission)` entry that entered the pipeline including excluded ones, indexed by position in the request, assigned in a pass that completes *before* any exclusion runs. Any `visit` value appearing anywhere in the response is a valid index into that cohort's `visits`.

**Confirmed guarantee — frontend:** resolves `visits[row.visit]` when rendering a row rather than pre-expanding the response, and treats `visits` as cohort-scoped (indices are meaningful only within their own cohort, never across cohorts). Note `len(visits) >= nVisits`, so `visits.length` is not a patient count — use `nPatients` / `nVisits` for display.

### 5.6 — Availability threshold is per-interval only

**Decision: `availability_threshold` is applied per interval and only per interval; S3 has no window-level availability gate.** This is a deliberate divergence from S2, where the same field gates cohort membership over the whole observation window. Rationale in [4.2](#42--per-interval-availability--value_range).

**Guarantee — S3 resolver:** an entry is never dropped for whole-window sparseness. It reaches bucketing whenever it resolves a patient and a window, and is then judged independently in each interval. This must survive the extraction of shared pipeline helpers from S2 ([4.0](#40--extracting-the-shared-stages)) — a window-level availability check riding along inside a "shared" helper would silently reimpose S2 semantics. Note the extraction plan is written so this cannot happen by accident: `_extract_patient_mean`, the function that *contains* S2's availability gate, is explicitly **not** shared; only the value-masking rule inside it is lifted out. [§6](#6--testing-outline) tests for the gate's absence anyway.

**Confirmed guarantee — frontend:** because the included population differs per interval and generally shrinks across the series, each plotted point is displayed with its `n_included` (n-per-bucket trace, band width, or tooltip). A cohort mean plotted without its denominator can read as a trend when part of the movement is a changing population.

---

## 6 — Testing outline

Mirror the S2 approach exactly: two files, one against a mocked SDK and one against a real dataset, both under `tests/atriumdb_dashboard/`.

### 6.1 `test_dashboard_timeseries_api.py` — endpoint tests against a mocked SDK

Copy the scaffolding from `test_dashboard_statistics_api.py`: `mount_dashboard(app)` onto the upstream test app at runtime, a module-scoped autouse fixture running one uvicorn server on **port 8125** (8123 is `test_api.py`, 8124 is statistics) and socket-probing until it accepts, and an autouse fixture clearing `app.dependency_overrides` after every test. `_mock_sdk(...)` needs one addition over S2's: **`get_measure_info` must return a real dict with `period_ns`**, since a bare `MagicMock` there yields a `MagicMock` period and the bucket arithmetic fails somewhere unhelpful. S2's `get_data` mock already branches on `return_nan_filled` and returns the 2-tuple, which is the only shape S3 uses.

| Group | Cases |
|---|---|
| Bucketing | data across the whole window lands one mean in each interval; `intervalIndex` / `startOffsetNs` / `endOffsetNs` correct and dense `0 … N−1`; a bucket width that is not a whole number of samples still partitions exhaustively with no dropped tail |
| Per-interval availability | dense early / sparse late → included early, `below_availability_threshold` late |
| **No window-level gate** | a visit whose coverage over the *whole* window is far below `availability_threshold`, but dense inside a handful of intervals, is **not** in `patient_exclusions` and carries a real mean in exactly those intervals. This is the S2-vs-S3 divergence — assert it explicitly so the [4.0](#40--extracting-the-shared-stages) extraction cannot reintroduce S2's drop |
| Shrinking denominator | `n_included` decreases across the series when data stops partway; `n_included + n_excluded` is constant across all intervals and equals `n_visits` |
| Exclusion placement | `mrn_not_found` appears once per entry in `patient_exclusions`, never per interval; `below_availability_threshold` appears in its specific interval only |
| **Single-reason `patient_exclusions`** | every reason in `patient_exclusions` is `mrn_not_found` — the assertion that keeps [3.4](#34--why-patient_exclusions-is-single-reason-today) honest, and that will fail loudly the day `all_time` is added |
| `value_range` per interval | out-of-range samples reduce a single interval's availability; port S2's global ∩ cohort intersection cases scoped to a bucket, since `resolve_value_range` is now shared code |
| Empty buckets | an interval no visit covers is still emitted, with `patientResults: []` and populated `exclusions` ([5.4](#54--empty-intervals)) |
| Validation | non-divisible `observation_window` / `interval_ns` → `422`; bucket count over `MAX_INTERVALS` → `422`; `freq_nhz == 0` measure → `422`; unknown measure → `422`; missing `X-Request-ID` → `400` |
| Multiple admissions | two admissions for one MRN produce independent bucket series **and two distinct `visits` entries** — same MRN, different `admission_ns`, each with its own `age_months` / `location`. The case an MRN-keyed table would silently collapse |
| Visit index stability | a cohort whose *first* entry is `mrn_not_found` still leaves every surviving entry at its original index; the excluded entry is present in `visits` and referenced from `patient_exclusions` |
| Index integrity | every `visit` referenced anywhere is in range for its cohort's `visits`; `len(visits) >= n_visits` |
| Demographics | age/sex/location on each `VisitInfo`, best-effort (absent → nulls, never excludes), appearing exactly once per entry regardless of interval count |

### 6.2 `test_dashboard_statistics_api.py` — must still pass unchanged

The [4.0](#40--extracting-the-shared-stages) extraction moves five helpers out of `statistics_resolver.py` and changes three signatures. S2's existing suite is the regression test for that refactor: **run it before writing any S3 code and again after the extraction, with no edits to the test file.** If it needs editing, the extraction changed behaviour and went too far.

### 6.3 `test_dashboard_timeseries_real_data.py` — real dataset

Same shape as `test_dashboard_statistics_real_data.py`: drive `compute_cohort_timeseries` directly against a mounted dataset, skipped when `ATRIUMDB_DATASET_LOCATION` is unset, cohort/measure/window/interval as module-level constants, loose assertion plus a full JSON dump to `cohort_timeseries_real_data.log`.

One extra job here beyond S2's: **validate the one-call decision from [4.1](#41--fetch-once-bucket-in-memory)** by computing per-interval availability both ways — from the NaN-filled grid and from a `get_interval_array` intersection — and reporting the divergence. If they agree on real waveform data the single-call path stands; if not, fall back to the two-call form.

### 6.4 Running them

Both live behind Docker, because `AtriumSDK.__init__` refuses to construct on macOS:

```bash
# Build once, from sdk/
docker build -t atriumdb-sdk .

# Endpoint tests (no dataset needed)
docker run --rm -it -v "$(pwd):/sdk" atriumdb-sdk \
  python -m pytest tests/atriumdb_dashboard/test_dashboard_timeseries_api.py -v

# Regression check on S2 after the pipeline extraction
docker run --rm -it -v "$(pwd):/sdk" atriumdb-sdk \
  python -m pytest tests/atriumdb_dashboard/test_dashboard_statistics_api.py -v

# Real-dataset run
./docker-run-dataset.sh python -m pytest \
  tests/atriumdb_dashboard/test_dashboard_timeseries_real_data.py -v -s
```

See `dockersetup.md` for the full Docker workflow.

---

## 7 — File-by-file change list

```
sdk/
├── atriumdb/                                       ← UNCHANGED — byte-identical to upstream main
├── atriumdb_dashboard/
│   ├── schemas.py                                  ← EDIT: append the S3 block below the S2 block
│   │                                                       (TimeSeriesRequest, VisitInfo, VisitMean,
│   │                                                        VisitExclusion, IntervalResult,
│   │                                                        CohortTimeSeries, TimeSeriesResponse,
│   │                                                        MAX_INTERVALS)
│   ├── pipeline.py                                 ← NEW: shared, request-model-agnostic stages
│   │                                                       (resolve_measure_id, resolve_patient_ids,
│   │                                                        observation_window, resolve_value_range,
│   │                                                        usable_mask, fetch_demographics, age_months)
│   ├── statistics_resolver.py                      ← EDIT: call pipeline.py instead of its own
│   │                                                       privates. Behaviour unchanged — §6.2
│   ├── timeseries_resolver.py                      ← NEW: compute_cohort_timeseries() + bucketing
│   ├── __init__.py                                 ← EDIT: export the new models and entry point
│   └── api/
│       ├── timeseries_endpoints.py                 ← NEW: POST /timeseries, mirrors
│       │                                                   statistics_endpoints.py
│       ├── app.py                                  ← EDIT: one _mount_router(app, timeseries_router,
│       │                                                   cohort_prefix) line in mount_dashboard()
│       └── __init__.py                             ← EDIT: export timeseries_router
└── tests/
    ├── mock_api/                                   ← UNCHANGED — byte-identical to upstream main
    └── atriumdb_dashboard/
        ├── test_dashboard_timeseries_api.py        ← NEW: mocked-SDK endpoint tests (port 8125)
        └── test_dashboard_timeseries_real_data.py  ← NEW: real-dataset run, skipped without a dataset
```

**Endpoint conventions to match.** `timeseries_endpoints.py` should copy `statistics_endpoints.py` line for line in structure: SDK from `api.dependencies.get_sdk_instance` (so one `dependency_overrides` entry still covers every router), `x_request_id: str | None = Header(default=None)` with a manual `400` when missing, and `except ValueError → HTTPException(422)` around the resolver call. Note the repo currently has two different `X-Request-ID` styles — `cohort_endpoints.py` enforces it in the `Header(...)` declaration and yields `422`, `statistics_endpoints.py` checks it in the body and yields `400`. **Follow statistics**, since `/cohorts/timeseries` is its sibling and the frontend already handles that contract for `/cohorts/statistics`.

---

## 8 — Summary of what is reused vs new

| Component | Status |
|---|---|
| `_Base`, `MeasureIdentifier`, `PatientAdmission`, `Admission`, `CohortInput`, `ValueRange(Map)`, `ExclusionReason` | **Reused unchanged** — imported from `atriumdb_dashboard.schemas` |
| `ExclusionRecord`, `PatientResult` | **Field types reused, re-containered** as `VisitExclusion` / `VisitMean` / `VisitInfo` ([3.1b](#31b-reused-as-field-types-but-re-containered)) |
| `_observation_window`, `_age_months`, `_fetch_demographics` | **Reused verbatim** — moved to `pipeline.py`, no logic change |
| `_resolve_measure_id`, `_resolve_value_range` | **Reused after a signature change** — take scalars instead of `AggregateStatisticsRequest` |
| `_resolve_patient_ids` | **Reused after a shape change** — returns the map, callers build their own exclusion records |
| `value_range` "out-of-range ⇒ absent" semantics | **Reused** via the extracted `usable_mask`, applied per interval |
| `_extract_patient_mean` | **Not reused** — S3 buckets one fetch instead ([4.1](#41--fetch-once-bucket-in-memory)) |
| Device-resolution stage | **Does not exist** — removed in `690c4ed`; the original plan was wrong to list it |
| `TimeSeriesRequest` (adds `interval_ns`, fixed window, divisibility + `MAX_INTERVALS` validators) | New |
| `VisitInfo`, `VisitMean`, `VisitExclusion` | New (normalised demographics — [2.1](#21--why-demographics-are-normalised)) |
| `IntervalResult`, `CohortTimeSeries`, `TimeSeriesResponse` | New |
| Per-interval bucketing + availability, aperiodic-measure guard | New |
| `pipeline.py`, `timeseries_resolver.py`, `POST /cohorts/timeseries` | New |

---

## 9 — Suggested implementation order

1. **Extract `pipeline.py`** and rewire `statistics_resolver.py` to it. Land this on its own and confirm `test_dashboard_statistics_api.py` passes untouched ([6.2](#62-test_dashboard_statistics_apipy--must-still-pass-unchanged)). Nothing S3-specific yet, so a regression here is unambiguous.
2. **Add the S3 schemas** with their validators. Testable in isolation — divisibility, `MAX_INTERVALS`, and the `all_time` rejection are all pure Pydantic.
3. **Write `timeseries_resolver.py`**, starting with the two-pass visit table and `patient_exclusions`, then the bucketing loop. Index stability is the part worth writing tests for first.
4. **Add the endpoint and mount it**, then the mocked-SDK suite.
5. **Run the real-dataset test**, including the one-call-vs-two-call availability comparison from [6.3](#63-test_dashboard_timeseries_real_datapy--real-dataset). Only after that is the [4.1](#41--fetch-once-bucket-in-memory) decision confirmed rather than assumed.
