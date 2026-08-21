# Measure Coverage Hours — `GET /measures/hours`

This document describes the implementation as it exists in the codebase: one dashboard-layer
query function, one FastAPI route, and the tests that cover them. It is the S1.5 counterpart to
`s1_cohort_definition_plan.md` — same dashboard package, but a much smaller surface.

---

## 0. What this adds

The dashboard needs to know **how much recorded data exists per measure** — the total hours of
stored signal for each measure tag, aggregated across every device. This is used to populate
measure pickers and coverage summaries without the dashboard having to scan waveform data.

The number reported is *recorded* time, not wall-clock span: it is derived from the sample count
actually stored in `block_index`, so gaps in acquisition are excluded rather than counted.

```
GET /measures/hours                                     [atriumdb_dashboard.api]
  └─ query_measure_total_hours(sdk)                     [unit conversion]
        └─ select_measure_total_values(sdk)             [SUM(block_index.num_values)]
              └─ sdk.sql_handler.connection(begin=False)
                    └─ num_values × period_ns → total_ns → total_hours
```

Both query functions live in `atriumdb_dashboard/queries.py`. Nothing is added to `atriumdb`:
the SQL runs through `SQLHandler.connection()`, the backend-agnostic context manager that
upstream already declares as abstract and both handlers implement.

---

## 1. File Layout

```
atriumdb/sdk/
├── atriumdb/                                ← UNCHANGED — byte-identical to upstream main
├── atriumdb_dashboard/
│   ├── pyproject.toml                       ← own distribution: atriumdb-dashboard
│   ├── queries.py                           ← select_measure_total_values (SQL)
│   │                                          + query_measure_total_hours (sample count → hours)
│   ├── api/
│   │   ├── measures_endpoints.py            ← GET /hours route + its own SDK dependency
│   │   └── app.py                           ← mount_dashboard() attaches the router at /measures
│   ├── docker/
│   │   ├── Dockerfile
│   │   ├── Dockerfile.dockerignore
│   │   └── docker-run-dataset.sh
│   └── docs/
└── tests/
    ├── mock_api/                            ← UNCHANGED — byte-identical to upstream main
    └── atriumdb_dashboard/
        ├── test_dashboard_api.py            ← test_api_measure_total_hours (synthetic fixtures)
        └── test_dashboard_real_data.py      ← test_measure_hours_report (real dataset)
```

Nothing outside `atriumdb_dashboard/` and `tests/atriumdb_dashboard/` differs from upstream
`main` — including `atrium_sdk.py`, which needs no dual-mode method for this endpoint (see §3.1).
See `modularization_pattern.md` for the rules this layout follows.

---

## 2. The Query

Both responsibilities live in `atriumdb_dashboard/queries.py`, split across two functions so the
raw rows can be inspected without the unit conversion:

| Function | Responsibility |
|---|---|
| `select_measure_total_values(sdk)` | Aggregate `block_index.num_values` per measure; return raw tuples |
| `query_measure_total_hours(sdk)` | Convert sample counts to `total_ns` / `total_hours`; shape into dicts |

Neither is a handler method. Both take the SDK as their first argument and obtain a cursor from
`sdk.sql_handler.connection(begin=False)`, so no `atriumdb` file is modified and no out-of-tree
handler subclass is broken by a new abstract method.

### 2.1 Data source (`select_measure_total_values`)

`block_index` is the per-block catalogue written during ingestion. Each row records, for one
`(measure_id, device_id)` block, how many samples it holds (`num_values`) and the time range it
covers. Summing `num_values` per measure gives the total number of stored samples for that
measure across all devices.

```sql
SELECT m.id, m.tag, m.freq_nhz, m.unit, SUM(bi.num_values)
FROM block_index bi
JOIN measure m ON m.id = bi.measure_id
WHERE m.freq_nhz > 0
GROUP BY m.id, m.tag, m.freq_nhz, m.unit
```

Returns one tuple per measure: `(measure_id, measure_tag, freq_nhz, units, total_num_values)`.

**Why it is a concrete method on the ABC rather than two implementations.** The query takes no
parameters — no dynamic `WHERE`, no `IN (...)` placeholder list — so the same SQL string is valid
on both backends and there is nothing dialect-specific to vary. `SQLHandler` already implements
around 28 such queries directly on the base class via `self.connection(...)`; this follows that
existing pattern. `select_patient_encounters` needed per-dialect implementations because it builds
its `WHERE` clause from four optional filters.

Every selected column is named in the `GROUP BY`. Grouping on `bi.measure_id` alone would work on
SQLite but risks rejection under MySQL/MariaDB's `ONLY_FULL_GROUP_BY`, since the functional
dependency runs through `m.id` rather than the grouped column.

`begin=False` because the statement is read-only — no transaction is opened. Measures with
`freq_nhz = 0` (aperiodic and annotation signals, where a sample count cannot be converted to a
duration) are excluded by the `WHERE` clause.

### 2.2 Sample count → hours (`query_measure_total_hours`)

`measure.freq_nhz` stores frequency in **nano-Hertz**, i.e. `freq_nhz = Hz × 10^9`. The sampling
period in nanoseconds is therefore:

```
period_ns = 10^9 / Hz = 10^9 / (freq_nhz / 10^9) = 10^18 / freq_nhz
```

and the totals follow:

```
total_ns    = total_num_values × 10^18 / freq_nhz
total_hours = total_ns / 3_600_000_000_000
```

Worked example: a 1 Hz measure has `freq_nhz = 1_000_000_000`, so `period_ns = 10^9` (one sample
per second). 10,800 stored samples → `1.08 × 10^13 ns` → **3.0 hours**.

The `measure` table also has a stored `period_ns` column, but it is nullable, so the conversion
is computed from `freq_nhz` rather than relying on it.

### 2.3 Return shape

`query_measure_total_hours(sdk)` returns a list of dicts **in whatever order the database
produces** — no sorting is applied at either layer, since the dashboard keys results by
`measure_id`. A caller that needs a particular order sorts for itself:

```python
{
    "measure_id":       int,
    "measure_tag":      str | None,
    "freq_nhz":         int,
    "units":            str | None,   # measure.unit
    "total_num_values": int,
    "total_ns":         float,
    "total_hours":      float,
}
```

Row tuples are zipped into dicts via the `_MEASURE_TOTAL_HOURS_KEYS` constant, which must stay in
the same order as the handler's `SELECT` list.

---

## 3. Server Side (`tests/mock_api/measures_endpoints.py`)

```python
@measures_router.get("/hours")
async def get_measure_total_hours(
        atriumdb_sdk: AtriumSDK = Depends(get_sdk_instance)):
    return query_measure_total_hours(atriumdb_sdk)
```

The router is already mounted at `/measures` in `app.py`, so the full path is
`GET /measures/hours`. No request body, no query parameters.

**Route order matters.** The existing `@measures_router.get("/{measure_id}")` route would match
`/measures/hours` and fail trying to coerce `"hours"` to an `int`, so the `/hours` route is
declared **above** it. FastAPI matches in declaration order.

Response is the list from §2.3, serialised directly by FastAPI:

```json
[
  {"measure_id": 1, "measure_tag": "HR", "freq_nhz": 1000000000, "units": "BPM",
   "total_num_values": 10800, "total_ns": 1.08e13, "total_hours": 3.0},
  {"measure_id": 2, "measure_tag": "SpO2", "freq_nhz": 1000000000, "units": "%",
   "total_num_values": 3600, "total_ns": 3.6e12, "total_hours": 1.0}
]
```

### 3.1 How this differs from the S1 cohort endpoint

| Dimension | `POST /cohorts` (S1) | `GET /measures/hours` (S1.5) |
|---|---|---|
| Entry point | `resolve_cohort(sdk, ...)`, dual-mode | `query_measure_total_hours(sdk)` — the endpoint calls it directly |
| API-mode client support | Yes, via the dashboard's own HTTP client | No; an API-mode SDK cannot reach this |
| Request/response models | Pydantic schemas | plain `list[dict]` |
| `X-Request-ID` | required | not used — deliberate, see §5 |
| SQL location | `queries.select_patient_encounters(sdk, ...)` | `queries.select_measure_total_values(sdk)` |
| Router prefix | `/cohorts` | `/measures` — mounted ahead of the host's `/{measure_id}` |

The layering is the same as S1: all SQL lives in `atriumdb_dashboard/queries.py` and reaches the
database through `sdk.sql_handler.connection()`, never through a method added to a handler. The
remaining differences follow from the endpoint's small scope: it takes no input and returns a flat
table, so there is nothing to validate, correlate, or model.

One wrinkle is specific to this endpoint. The host already serves `GET /measures/{measure_id}`,
and Starlette matches routes in registration order with no preference for a more specific path, so
an appended `/measures/hours` would resolve to `get_measure_info(measure_id="hours")` instead.
`mount_dashboard` therefore moves the dashboard's routes to the front of the routing table — the
same effect the original in-place edit got by declaring `/hours` above `/{measure_id}` in one
module.

---

## 4. Tests

### 4.1 `tests/atriumdb_dashboard/test_dashboard_api.py::test_api_measure_total_hours` — synthetic fixtures

Creates a SQLite dataset at `tests/test_datasets/sqlite_dashboard_api_hours_test` and starts the
mock FastAPI app on port **8124**.

Fixture data — two measures at 1 Hz across two devices, with `block_index` rows **inserted
directly**:

| Measure | Device | `num_values` | Equivalent |
|---|---|---|---|
| HR | monitor_1 | 7200 | 2 h |
| HR | monitor_2 | 3600 | 1 h |
| SpO2 | monitor_1 | 3600 | 1 h |

Writing `block_index` by hand is deliberate: the normal ingest path needs the native `libTSC`
library, which has no macOS build and is not needed to test the aggregation itself.

Assertions (all keyed by `measure_tag`, so they do not depend on row order):
- exactly two measures come back (one row per measure, devices summed)
- the dict carries all seven expected keys
- `HR` → `total_num_values == 10800` and `total_hours ≈ 3.0`
- `SpO2` → `total_num_values == 3600` and `total_hours ≈ 1.0`
- the same four values are asserted again over HTTP against `GET /measures/hours` (status 200),
  so the local helper and the endpoint are verified to agree

Float comparisons use a `1e-6` tolerance. The HTTP call is a plain `requests.get`, not an
API-mode SDK call, because no SDK method wraps this endpoint (§3.1).

### 4.2 `tests/atriumdb_dashboard/test_dashboard_real_data.py::test_measure_hours_report` — real dataset

Runs the same helper against a mounted AtriumDB dataset. Skipped automatically when
`ATRIUMDB_DATASET_LOCATION` is unset, so it never blocks a normal test run.

It asserts only that every `total_hours` is non-negative, and skips when `block_index` is empty —
the real value is the **report it writes**, a per-measure coverage table:

```
==================================================================
  AtriumDB Measure Coverage Report  —  2026-08-05T17:22:00
  Dataset : /data/atriumdb
  Source  : block_index  (12 measures)
==================================================================

      ID  tag                             units            total_hours
------------------------------------------------------------------
       1  MDC_ECG_HEART_RATE              bpm                1,204 h 18 m
       ...
------------------------------------------------------------------
          TOTAL                                              9,881 h 02 m
==================================================================

--- RAW JSON ---
[ ... ]
```

Rows appear in the order the query returns them (§2.3) — the report does not sort. The report is
written to `tests/measure_hours_report.log`, overridable with the
`ATRIUMDB_MEASURE_HOURS_LOG` environment variable. Both the formatted table and the raw JSON go
into the same file, so the numbers can be eyeballed or diffed between runs.

### 4.3 Running the tests

Both live behind Docker, because `AtriumSDK.__init__` refuses to construct on macOS:

```bash
# Build once, from sdk/
docker build -t atriumdb-sdk -f atriumdb_dashboard/docker/Dockerfile .

# Synthetic test (no dataset needed)
docker run --rm -it -v "$(pwd):/sdk" atriumdb-sdk \
  python -m pytest tests/atriumdb_dashboard/test_dashboard_api.py::test_api_measure_total_hours -v -s

# Real-data report (dataset mounted read-only)
./atriumdb_dashboard/docker/docker-run-dataset.sh python -m pytest \
  tests/atriumdb_dashboard/test_dashboard_real_data.py::test_measure_hours_report -v -s
```

`docker-run-dataset.sh` mounts the dataset at `/data/atriumdb` **read-only** — this endpoint only
reads — sets `ATRIUMDB_DATASET_LOCATION`, and bind-mounts the working copy over `/sdk` so the code
under test is the current checkout. Set `HOST_DATASET_PATH` at the top of the script before use;
it refuses to run while the placeholder value is in place. See `atriumdb_dashboard/docs/dockersetup.md` for the full
Docker workflow.

---

## 5. Intended Consumption Model

### 5.1 Why there is no `X-Request-ID`

`POST /cohorts` requires an `X-Request-ID` because it is called **interactively**: a user opens the
dashboard, defines a cohort, and a request goes out on their behalf. When one of those requests
returns a surprising result, the correlation id is what ties the user's report back to the
AtriumDB log lines for that single call.

`GET /measures/hours` is not that kind of endpoint. It is not called when a user reaches a page.
It is intended to be pulled on a **schedule by the server** — a daily job that refreshes stored
measure-coverage totals, which the dashboard then reads from its own database. There is no user
session to correlate against, and one caller on a fixed cadence makes runs identifiable by
timestamp alone. Requiring a per-request id would add ceremony without adding traceability, so the
endpoint deliberately omits it, takes no parameters, and returns a bare array.

### 5.2 Not yet built

The endpoint works and is tested, but the workflow that is meant to call it does not exist yet.
Two pieces are outstanding:

- **The scheduled job.** Nothing currently calls `GET /measures/hours` on a cadence. The daily
  pull — its schedule, the process that runs it, and where the retrieved totals are persisted on
  the dashboard side — still has to be set up. Until then the endpoint is only exercised by the
  tests in §4 and by manual calls.
- **Failure notification.** A scheduled pull fails silently by definition: no user is waiting on
  the response, so a failed or skipped run is invisible until someone notices the coverage figures
  have gone stale. The job therefore needs to send an **email notification on failure** — a
  non-200 response, a connection error, or a run that does not happen at all. This has not been
  implemented or configured.
