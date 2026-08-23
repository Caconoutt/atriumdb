# Step 3 — Per-Interval Time-Series Across Cohorts

This document describes the implementation plan for a **time-series** view of a physiological measure across cohorts. It takes the same cohort input as the Step 2 statistics endpoint, but instead of a single per-patient summary over one observation window, it chops each patient's window into fixed-width intervals (e.g. 5 / 10 / 15 min) and reports a per-patient mean **per interval**, so the client can plot each cohort's signal over time.

It reuses the Step 2 request/patient/measure models wherever possible; only the request additions (the interval width) and the response grouping (by interval bucket) are new.

---

## 0 — Prerequisite: schema availability

The models this plan reuses (`MeasureIdentifier`, `PatientAdmission`, `Admission`, `CohortInput`, `ValueRange`, `ExclusionReason`) — and the two it re-containers rather than imports (`ExclusionRecord`, `PatientResult`; see [3.1b](#31b-reused-as-field-types-but-re-containered)) — live in `atriumdb/dashboard/schemas.py`, **which exists only on `s2_cohort_stats`, not on `main`**. This branch was cut fresh from `main` and has no `dashboard/` package yet.

Before any code here can import those models, one of the following must happen:

1. **(Preferred)** `s2_cohort_stats` merges to `main`, then `s3_time_stats` rebases onto the updated `main`. Cleanest history; the shared models arrive as a reviewed unit.
2. Merge `s2_cohort_stats` into `s3_time_stats` now, accepting that S3 then carries S2's code until both land.
3. Cherry-pick just `schemas.py` — not recommended, since S3 also needs the shared **pipeline helpers** (measure resolution, patient-ID resolution, device resolution, availability), which live in `statistics_resolver.py`.

**Recommendation:** option 1, and treat "S2 is merged" as the gate for starting S3 implementation. This plan can be finalised and reviewed in parallel without waiting.

---

## 1 — Endpoint decision: separate endpoint

**Decision: a new endpoint, `POST /cohorts/timeseries`, not a `vizType` branch on `/cohorts/statistics`.**

The instinct to keep one endpoint and switch on `vizType` (box / violin → S2, time-series → S3) was considered and rejected, for reasons that go beyond the viz label:

| | `/cohorts/statistics` (S2) | `/cohorts/timeseries` (S3) |
|---|---|---|
| Extra required input | — | `interval_ns` (meaningless for box/violin) |
| Availability evaluated | once, over the whole window | **per interval** |
| Response shape | flat `patient_results` per cohort | **grouped into interval buckets** |
| Per-patient output | one mean | one mean **per interval** |

A single endpoint would need a request model where `interval_ns` is conditionally required, a response that is a union of two unrelated shapes, and internal `if vizType == ...` branching over the parts that genuinely differ (availability, bucketing, assembly). That is the "glue together and it gets messy" outcome. OpenAPI/client codegen also degrades: a union response is far worse to consume than two named response models.

**What the two endpoints share is internal, not the endpoint.** Measure resolution, patient-ID resolution, observation-window computation, and device resolution are identical. Those should be **extracted into shared helper functions** (e.g. a small `dashboard/pipeline.py`, or reused from `statistics_resolver.py`) and called by both resolvers. Share the pipeline stages; keep the endpoints, request models, and response models separate.

Router entry (mirrors S2's `/cohorts` prefix in `cohort_endpoints.py`):

```
POST /cohorts/timeseries
Body: TimeSeriesRequest
Returns: TimeSeriesResponse
```

---

## 2 — Overview

**Inputs** — same cohort/measure/window inputs as S2, plus one:

- `cohorts` — pre-resolved cohorts from the Step 1 resolver (`CohortInput`), each with patients and their qualifying admissions. **Reused unchanged.**
- `measure` — the signal to analyse (`MeasureIdentifier`). **Reused unchanged.**
- `observation_window` — fixed window length in epoch ns, anchored at each admission (e.g. 24 h). See the [all_time decision](#52--observation-window-fixed-only-for-v1).
- **`interval_ns`** *(new)* — bucket width in epoch ns (e.g. 5 min = `300_000_000_000`). The window is chopped into consecutive intervals of this width.
- `availability_threshold` — minimum covered fraction, applied **per interval, and only per interval**. Unlike S2, S3 has **no window-level availability gate**: an entry is never dropped for being sparse across the observation window as a whole. It enters bucketing as long as it resolves a patient, a window and a device, and its availability is then judged independently in each interval. A visit with data in only 3 of 288 intervals therefore appears in `patient_results` for those 3 and in `exclusions` for the other 285 — it is not excluded at the entry level.
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
        { "mrn": "234567", "admissionNs": null,
          "sex": null, "ageMonths": null, "location": null },
        { "mrn": "345678", "admissionNs": 1700000000000000000,
          "sex": "F", "ageMonths": 7, "location": "CCU" },
        { "mrn": "456789", "admissionNs": 1700000000000000000,
          "sex": "F", "ageMonths": 132, "location": "ICU" }
      ],
      "patientExclusions": [
        { "visit": 1, "reason": "mrn_not_found" },
        { "visit": 2, "reason": "no_device_found" }
      ],
      "intervals": [
        {
          "intervalIndex": 0,
          "startOffsetNs": 0,
          "endOffsetNs": 300000000000,
          "nIncluded": 1,
          "nExcluded": 1,
          "patientResults": [
            { "visit": 0, "mean": 74.1 }
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

### 2.1 — Why demographics are normalised

Demographics are **demographics-at-admission**: for a given `(mrn, admissionNs)` entry, `sex` / `ageMonths` / `location` are constant for the whole stay. Repeating them on every interval is therefore lossless to remove, and the repetition is substantial. For a 24 h window at 5 min intervals with 205 visits, the response holds 288 × 205 ≈ **59,000** per-patient result objects. Inline, each is ~110 bytes:

```json
{"mrn":"123456","admissionNs":1700000000000000000,"mean":74.1,"sex":"M","ageMonths":41,"location":"ICU"}
```

of which ~90 bytes is byte-identical across all 288 copies — `admissionNs` alone is a 19-digit integer repeated once per interval. Normalised, the same row is `{"visit":0,"mean":74.1}`, ~20 bytes: roughly an **80% reduction** in response size, per cohort.

**Why the index is `(mrn, admissionNs)` and not `mrn`.** A readmitted patient has a different `ageMonths` and often a different `location` at each admission, and §6 requires two admissions for one MRN to produce independent bucket series. Keying the table by MRN alone would collapse those into whichever admission was written last. The `visits` entry *is* the `(patient, admission)` entry, one-to-one with what S2 calls the entry key.

**Why an integer index and not a composite string key.** A `"mrn|admissionNs"` map key costs ~28 bytes per row and a string concat plus hash lookup on every one of the ~59,000 rows. A positional index is ~10 bytes and resolves as `visits[row.visit]`. The client needs no rehydration pass: the table renderer already iterates rows to draw them, so it resolves each row's demographics at draw time — and only for rows actually on screen.

**Index stability.** `visits` contains *every* `(patient, admission)` entry that entered the pipeline, **including entries later excluded** — `patientExclusions` references indices too. Indices must be assigned in one pass over the cohort's input entries **before** any exclusion runs, so an index is simply the entry's position in the request. Appending to `visits` as a side effect of *successful* processing would let a mid-list drop shift every later index, and is the one way to get this wrong. Note the consequence: `len(visits) ≥ nVisits`, since `nVisits` counts entries that reached bucketing while `visits` also carries the pre-bucketing drops.

---

## 3 — Schema design

### 3.1 Reused unchanged

Imported directly from `atriumdb/dashboard/schemas.py`:

- `MeasureIdentifier`
- `Admission`, `PatientAdmission`
- `CohortInput`
- `ValueRange`, `ValueRangeMap`
- `ExclusionReason` — the existing five reasons cover S3 too: `mrn_not_found`, `no_device_found`, `missing_discharge_time` (patient/admission-level), and `below_availability_threshold`, `no_usable_values` (now evaluated per interval).

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

Deliberately **not** a subclass of `AggregateStatisticsRequest`: its `observation_window` type differs (no `"all_time"`), and inheriting a field only to narrow it is more confusing than declaring the four shared fields again. The field-level *types* are reused; the container is its own.

**Validation:** reject with `422` when `observation_window % interval_ns != 0` (see [5.1](#51--windowinterval-divisibility)). A cross-field pydantic `model_validator` is the natural home.

### 3.3 New response models

```python
class VisitInfo(_Base):
    """One (patient, admission) entry. Demographics are at-admission and
    constant for the stay, so they live here, once, not on every interval."""
    mrn: str
    admission_ns: int | None               # null when the entry never resolved
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

**Why two exclusion locations** — this is the one genuinely new modelling decision:

- A `mrn_not_found`, `no_device_found`, or `missing_discharge_time` drop removes the **entire (patient, admission) entry** from every interval. Recording it once per interval would repeat it 288 times for a 24 h / 5 min request. These go in `CohortTimeSeries.patient_exclusions`.
- A `below_availability_threshold` or `no_usable_values` drop is **specific to one interval** — the same patient may be present in interval 0 and absent in interval 40 because their data ran out. These go in the relevant `IntervalResult.exclusions`.

So a patient present overall but sparse in places appears in `patient_results` for the intervals they cover and in `exclusions` for the intervals they don't. `n_patients` / `n_visits` count what entered bucketing; per-interval `n_included` / `n_excluded` count that interval.

---

## 4 — Processing pipeline

Reuses S2 stages 1–3c verbatim (measure resolution → patient-ID resolution → observation window → device resolution). S3 diverges only **after** a valid device and full-window bounds are in hand.

```
for each cohort:
  resolve value_range for the cohort (reused _resolve_value_range)

  # PASS 1 — build the visit table first, so indices cannot shift (see 2.1)
  visits = [VisitInfo(mrn, admission_ns, +demographics) for each entry, in request order]

  # PASS 2 — process; every record refers back by index
  for v, entry in enumerate(cohort entries):
     resolve patient_id            ─┐
     compute observation window     │  identical to S2 — extract as
     resolve device_id              │  shared pipeline helpers
     (all_time discharge handling) ─┘
     if dropped here: append VisitExclusion(visit=v, ...) to cohort.patient_exclusions ; continue

     ── S3-specific from here ──
     fetch coverage + values ONCE over the whole window
     for each interval i in 0 … N-1:
         slice this interval's coverage and samples
         evaluate per-interval availability (+ value_range filtering)
         if below threshold or no usable values:
             append VisitExclusion(visit=v, ...) to intervals[i].exclusions
         else:
             append VisitMean(visit=v, mean=interval mean) to intervals[i].patient_results
```

Note the two passes. Demographics are looked up once per entry while building `visits`, not once per interval — so normalising the response also removes ~288× redundant work from assembly, not just from the wire. Pass 1 must not be folded into pass 2: appending to `visits` only for entries that survive would make an index depend on how many earlier entries were dropped.

### 4.1 Fetch once, bucket in memory (efficiency)

Do **not** call `get_data` / `get_interval_array` per interval — that is N calls per patient (288 for 24 h / 5 min). Instead:

1. **One `get_interval_array`** over the full window → the available-data segments. For each interval, compute the intersection of those segments with `[start_i, end_i)` and sum, giving per-interval covered ns and hence per-interval availability. This is a straightforward interval-overlap computation in numpy.
2. **One `get_data`** over the full window. To assign each sample to an interval:
   - Prefer `return_nan_filled=True` (the mode S2 already uses when bounds are active): the returned grid is regular, so sample index → interval index is pure integer arithmetic (`floor((t − window_start) / interval_ns)`), no per-sample time lookup. This also makes `value_range` filtering and per-interval availability fall out of the same array, consistent with S2's bounded path.
   - Bucket with `np.add.reduceat` / boundary `searchsorted` on the index grid; compute each interval's mean over its non-NaN, in-range slice.

Net: **two SDK data calls per patient**, same as S2, regardless of interval count.

### 4.2 Per-interval availability + value_range

Identical rule to S2, scoped to the interval: coverage is measured against `interval_ns` (the bucket width), and out-of-range / NaN samples count as absent. A patient whose signal is mostly artefact in interval 3 fails `availability_threshold` for interval 3 only. This reuses the same "out-of-range ⇒ absent, not averaged" semantic documented on `ValueRange` / `value_range`.

**The denominator is `interval_ns`, never `observation_window`.** In S2 the threshold answers "is this visit a usable member of the cohort?" — one whole-window judgement, one drop. In S3 it answers a different question, "is this visit's mean in *this bucket* trustworthy?", and answers it N times independently. The two are not the same test at different scales: 80% coverage of 24 h and 80% coverage of a 5 min bucket are different requirements, and the per-interval one is far more volatile. S3 deliberately asks only the second question.

**Consequence — the included population varies by interval.** Because there is no entry-level gate, each interval's cohort mean is computed over whatever set survived *that* interval, and that set generally shrinks over the series as patients are discharged or come off monitoring. `IntervalResult.n_included` exists precisely so this is visible: the client should carry it alongside each plotted point (as an n-per-bucket trace, band width, or tooltip), because a cohort mean plotted without its denominator will read as a trend when part of it is just a changing population. Note this is the same raggedness [5.2](#52--observation-window-fixed-only-for-v1) rejects `all_time` for — a fixed window bounds the bucket *grid* so every cohort shares an x-axis, but it does not make the per-bucket denominators equal.

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

### 5.2 — Observation window: fixed only for v1

**Decision: v1 accepts a fixed `observation_window` only; no `"all_time"`.** Under `all_time` each patient's window is `admission → discharge`, so stay length — and therefore interval count — varies per patient. The buckets would be *ragged*: interval 40 would contain only patients whose stay was long enough, silently biasing every late interval toward longer-staying (typically sicker) patients. A time-series meant to be plotted on one shared x-axis needs a common bucket grid, which a fixed window gives and `all_time` does not. If `all_time` is needed later, it needs an explicit decision about how to present ragged tails (truncate to the shortest stay? report per-interval denominators?) — out of scope for v1.

**Confirmed guarantee — frontend:** the `all_time` observation-window option is removed from the picker when `vizType` is time-series, so the combination is unreachable in the UI. On the API side it is unrepresentable rather than validated: `observation_window` is typed `PositiveInt`, so `"all_time"` fails schema parsing before any resolver code runs.

Note this bounds the bucket *grid* only. It does not make per-interval denominators equal — see [5.6](#56--availability-threshold-is-per-interval-only).

### 5.3 — Interval units at the API boundary

**Decision: schema carries `interval_ns` (nanoseconds); the dashboard server converts the user's "5 / 10 / 15 min" selection to ns before calling**, exactly as it already pre-converts `AgeBand` and `observation_window`. Keeps AtriumDB's boundary uniformly nanosecond-based and unit-conversion-free.

**Confirmed guarantee — dashboard server:** performs the minutes → ns conversion. AtriumDB never sees a minutes-denominated value and does no unit inference; a request carrying `interval_ns: 5` is interpreted literally as 5 nanoseconds, not 5 minutes, and will simply produce a degenerate result rather than an error. This is the one item in this table with no server-side backstop, so the conversion belongs in a single shared helper on the dashboard side rather than at each call site.

### 5.4 — Empty intervals

**Decision: an interval where *every* entry was excluded is still emitted** (empty `patient_results`, populated `exclusions`), so the client sees a real gap in the series rather than a missing index. Interval indices are therefore always dense and complete: `0 … (observation_window / interval_ns) − 1`, with no holes.

**Confirmed guarantee — frontend:** plots no point for such an interval (a visible gap in the line, not an interpolated segment across it) and surfaces that interval's `exclusions` in the table, so the user can see *why* the series is absent there rather than inferring a data outage.

### 5.5 — Normalised visit table

**Decision: per-patient demographics are sent once per visit in `CohortTimeSeries.visits`; each per-interval row references them by integer `visit` index.** Full rationale, sizing, and the index-stability rule in [2.1](#21--why-demographics-are-normalised); models in [3.3](#33-new-response-models).

**Guarantee — S3 resolver:** `visits` holds every `(patient, admission)` entry that entered the pipeline including excluded ones, indexed by position in the request, assigned in a pass that completes *before* any exclusion runs. Any `visit` value appearing anywhere in the response is a valid index into that cohort's `visits`.

**Confirmed guarantee — frontend:** resolves `visits[row.visit]` when rendering a row rather than pre-expanding the response, and treats `visits` as cohort-scoped (indices are meaningful only within their own cohort, never across cohorts). Note `len(visits) >= nVisits`, so `visits.length` is not a patient count — use `nPatients` / `nVisits` for display.

### 5.6 — Availability threshold is per-interval only

**Decision: `availability_threshold` is applied per interval and only per interval; S3 has no window-level availability gate.** This is a deliberate divergence from S2, where the same field gates cohort membership over the whole observation window. Rationale in [4.2](#42-per-interval-availability--value_range).

**Guarantee — S3 resolver:** an entry is never dropped for whole-window sparseness. It reaches bucketing whenever it resolves a patient, a window and a device, and is then judged independently in each interval. This must survive the extraction of shared pipeline helpers from S2 ([1](#1--endpoint-decision-separate-endpoint), [7](#7--summary-of-what-is-reused-vs-new)) — a window-level availability check riding along inside a "shared" helper would silently reimpose S2 semantics, which is why [6](#6--testing-outline-for-the-eventual-implementation) tests for its absence explicitly.

**Confirmed guarantee — frontend:** because the included population differs per interval and generally shrinks across the series, each plotted point is displayed with its `n_included` (n-per-bucket trace, band width, or tooltip). A cohort mean plotted without its denominator can read as a trend when part of the movement is a changing population.

---

## 6 — Testing outline (for the eventual implementation)

Mirror the S2 mock-SDK approach (`compute_*` wired to the real resolver, per-stage SDK methods stubbed):

- **Bucketing:** a patient with data across the whole window lands one mean in each interval; interval offsets/indices are correct.
- **Per-interval availability:** a patient dense early and sparse late is included in early intervals, `below_availability_threshold` in late ones.
- **No window-level gate:** a visit whose coverage over the *whole* window is far below `availability_threshold` — but which is dense inside a handful of intervals — is **not** in `patient_exclusions`, and carries a real mean in exactly those intervals. This is the S2-vs-S3 divergence; assert it explicitly so a later refactor toward shared helpers cannot reintroduce S2's window-level drop.
- **Shrinking denominator:** `n_included` decreases across the series when a patient's data stops partway through, and `n_included + n_excluded` stays constant across all intervals (every bucketed entry is accounted for in every interval).
- **Exclusion placement:** `mrn_not_found` / `no_device_found` appear once in `patient_exclusions`, never per interval; `below_availability_threshold` appears in the specific interval only.
- **value_range per interval:** out-of-range samples reduce a single interval's availability (reuse the S2 case, scoped to a bucket).
- **Divisibility guard:** non-divisible `observation_window` / `interval_ns` → `422`.
- **Multiple admissions:** two admissions for one patient produce independent bucket series **and two distinct `visits` entries** — same MRN, different `admission_ns`, each with its own `age_months` / `location`. This is the case an MRN-keyed table would silently collapse.
- **Visit index stability:** a cohort whose *first* entry is excluded pre-bucketing (e.g. `mrn_not_found`) still leaves every surviving entry at its original index — assert a later entry's `visit` equals its position in the request, and that the excluded entry is present in `visits` and referenced from `patient_exclusions`.
- **Index integrity:** every `visit` referenced from any `patient_results` / `exclusions` / `patient_exclusions` is in range for its cohort's `visits`; `len(visits) >= n_visits`.
- **Demographics:** age/sex/location carried on each `VisitInfo`, best-effort (absent → nulls, never excludes), and appearing exactly once per entry regardless of interval count.

---

## 7 — Summary of what is reused vs new

| Component | Status |
|---|---|
| `MeasureIdentifier`, `PatientAdmission`, `Admission`, `CohortInput`, `ValueRange(Map)`, `ExclusionReason` | **Reused unchanged** |
| `ExclusionRecord`, `PatientResult` | **Field types reused, re-containered** as `VisitExclusion` / `VisitMean` (see [3.1b](#31b-reused-as-field-types-but-re-containered)) |
| Measure / patient-ID / window / device resolution stages | **Reused** (extract as shared helpers) |
| `value_range` "out-of-range ⇒ absent" semantics | **Reused**, applied per interval |
| `TimeSeriesRequest` (adds `interval_ns`, fixed window) | New |
| `VisitInfo`, `VisitMean`, `VisitExclusion` | New (normalised demographics — [2.1](#21--why-demographics-are-normalised)) |
| `IntervalResult`, `CohortTimeSeries`, `TimeSeriesResponse` | New |
| Per-interval bucketing + availability | New |
| `POST /cohorts/timeseries` endpoint + resolver | New |
```
