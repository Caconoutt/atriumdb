# Implementation Plan: Priority 1A, 1B + Server API

> **Status: Implementation complete. Tests written but not yet executed.**
> The AtriumSDK raises `OSError` on macOS (`atrium_sdk.py:169`) because it requires a native
> C library only available on Linux. Tests must be run in a Linux environment (Docker or CI).
> See §8 for the run command.

This document reflects the **actual implementation** as of the current state of the codebase.
It follows the dual-mode pattern the existing SDK uses throughout: one call site works in both
direct-DB mode (local) and API mode (remote over HTTP), with the server being a thin FastAPI
wrapper around the same local logic.

---

## 0. The Dual-Mode Pattern

Every SDK method branches on `metadata_connection_type`:

```python
if self.metadata_connection_type == "api":
    return self._request("POST", "cohorts/", json=request.model_dump(), ...)
else:
    return resolve_cohorts_local(self, request, request_id)
```

The caller's code is **identical** in both cases. The server is a FastAPI process running the
same SDK in direct-DB mode — its endpoint calls the same local function and returns JSON.

```
sdk.dashboard_resolve_cohort(request)
  │
  ├─ "api" mode
  │     └─ self._request("POST", "cohorts/", ...)
  │             └─ HTTP ──► FastAPI /cohorts
  │                           └─ resolve_cohorts_local(server_sdk, request)
  │                                 └─ sql_handler.select_patient_encounters(...)
  │
  └─ direct-DB mode
        └─ resolve_cohorts_local(self, request)
              └─ sql_handler.select_patient_encounters(...)
```

---

## 1. File Layout (actual)

```
atriumdb/sdk/
├── atriumdb/
│   ├── atrium_sdk.py                        ← MODIFIED: dashboard_resolve_cohort() method
│   ├── sql_handler/
│   │   ├── sql_handler.py                   ← MODIFIED: select_patient_encounters() abstract method
│   │   ├── sqlite/sqlite_handler.py         ← MODIFIED: select_patient_encounters() implementation
│   │   └── maria/maria_handler.py           ← MODIFIED: select_patient_encounters() implementation
│   └── dashboard/                           ← NEW package — pure logic, no FastAPI
│       ├── __init__.py
│       ├── schemas.py                       ← Pydantic request/response models
│       ├── encounter_queries.py             ← LOCATION_LOOKUP + query_patient_encounters + group_encounters_by_visit
│       └── cohort_resolver.py               ← resolve_cohorts_local, _resolve_mrn_cohort, _resolve_demographic_cohort
└── tests/
    ├── mock_api/
    │   ├── app.py                           ← MODIFIED: include_router(cohort_router, prefix="/cohorts")
    │   ├── cohort_endpoints.py              ← NEW: POST /cohorts FastAPI handler
    │   └── sdk_dependency.py               ← unchanged
    └── test_dashboard_api.py               ← NEW: cohort API tests (SQLite only, see §8)
```

---

## 2. Pydantic Schemas (`atriumdb/dashboard/schemas.py`)

Seven models — serve both the client (serialise to JSON for HTTP) and the server (deserialise
incoming JSON). No SDK dependency, safe to import anywhere.

| Model | Purpose |
|---|---|
| `AdmissionDateRange` | `start`/`end` epoch ns window |
| `AgeBand` | `startNs`/`endNs` age range in nanoseconds |
| `MrnCohort` | `id` + `mrnList` for 1A requests |
| `DemographicCohort` | `id` + optional `age`, `sex`, `location`, `valueRange` for 1B |
| `CohortDefinitionRequest` | Top-level request: `type` (`"mrn"`/`"demographic"`), `admissionDateRange`, `cohorts` |
| `ResolvedCohort` | `id` + resolved `mrnList` |
| `MrnCohortResponse` | `requestId` + list of `ResolvedCohort` |

---

## 3. SQL Handler Layer

### 3.1 `select_patient_encounters` (new abstract method)

Added to `SQLHandler` immediately after `select_encounters` so both encounter-related methods
are grouped together. Implemented by both `SQLiteHandler` and `MariaDBHandler`.

```
sql_handler.py       → @abstractmethod select_patient_encounters(patient_id_list, admit_start_ns, admit_end_ns, unit_name_list)
sqlite_handler.py    → implementation using self.sqlite_db_connection()
maria_handler.py     → implementation using self.maria_db_connection()
```

Both implementations follow the existing SDK style exactly:
- `arg_tuple = ()`, `where_clauses = []`
- `','.join(['?'] * len(...))` for IN placeholders
- `WHERE ... AND ...` appended only when `where_clauses` is non-empty
- `ORDER BY e.start_time ASC`
- Returns raw tuples: `(encounter_id, patient_id, visit_number, bed_id, unit_id, unit_name, start_time_ns, end_time_ns)`

JOIN chain: `encounter → bed → unit`. The INNER JOIN drops rows where `bed_id` is NULL
(pre-admission placeholder rows with no bed assignment are excluded by design).

### 3.2 Comparison: `select_encounters` vs `select_patient_encounters`

| Dimension | `select_encounters` | `select_patient_encounters` |
|---|---|---|
| Requires patient input | Yes — patient_id_list or mrn_list required | No — all args optional |
| Location filter | None | `unit_name_list` via JOIN to bed+unit |
| Time semantics | Overlap (`end_time > start AND start_time < end`) | Admission range (`start_time >= admit_start AND start_time <= admit_end`) |
| NULL bed_id rows | Returned (no bed JOIN) | Excluded (INNER JOIN) |
| ORDER BY | `encounter.id` | `e.start_time` |

---

## 4. Dashboard Layer (`atriumdb/dashboard/`)

### 4.1 `encounter_queries.py`

Two functions and one lookup table. Does not contain raw SQL — delegates to `sql_handler`.

**`LOCATION_LOOKUP`** — maps API location codes to exact `unit.name` values in the DB.
Filter is on `unit.name`, not `unit.type`.

```python
LOCATION_LOOKUP: dict[str, list[str]] = {
    "ICU": ["ICU"],
    "OR":  ["OR"],
}
```

**`query_patient_encounters(sdk, patient_id_list, admit_start_ns, admit_end_ns, locations)`**
— translates `locations` list of API codes to `unit_name_list` via `LOCATION_LOOKUP`, then
calls `sdk.sql_handler.select_patient_encounters(...)`. Returns list of dicts:
```python
{"encounter_id": int, "patient_id": int, "visit_number": str|None,
 "bed_id": int, "unit_id": int, "unit_name": str|None,
 "start_time_ns": int, "end_time_ns": int|None}
```

**`group_encounters_by_visit(encounter_rows)`** — pure Python. Groups per-encounter rows into
per-visit records keyed by `(patient_id, visit_number)`:
- `admit_time_ns = MIN(start_time_ns)` across the visit's rows
- `discharge_time_ns = MAX(end_time_ns)`; `None` if any row is still open
- NULL `visit_number` rows are grouped under `(patient_id, None)` with the same MIN/MAX rules

### 4.2 `cohort_resolver.py`

Entry point: `resolve_cohorts_local(sdk, request, request_id)` — dispatches to 1A or 1B.
No HTTP awareness; always assumes `sdk` is a direct-DB instance.

**1A — `_resolve_mrn_cohort`:**

| Step | Action |
|---|---|
| 0 | Normalise: `m.strip()` for each MRN |
| 1 | `sdk.get_mrn_to_patient_id_map(mrn_list=...)` — absent MRNs logged and dropped |
| 2 | `query_patient_encounters(sdk, patient_id_list=..., admit_start_ns=..., admit_end_ns=...)` — no location filter |
| 2b | `group_encounters_by_visit(...)` — patients with no in-window encounter logged and dropped |
| 3 | Return surviving MRNs |

**1B — `_resolve_demographic_cohort`:**

| Step | Action |
|---|---|
| 1 | `query_patient_encounters(sdk, locations=..., admit_start_ns=..., admit_end_ns=...)` |
| 1b | `group_encounters_by_visit(...)` → reference admission = earliest `admit_time_ns` per patient |
| 2 | `sdk.sql_handler.select_all_patients_in_list(patient_id_list=...)` for demographics |
| 3 | Age filter: `band.startNs <= (admit_time_ns - dob_ns) <= band.endNs`; patients with no `dob` excluded when age filter present |
| 3b | Sex filter: `"U"` matches NULL / empty / `'U'` in `patient.gender` |
| 4 | Return surviving MRNs |

---

## 5. Dual-Mode Method (`atriumdb/atrium_sdk.py`)

`dashboard_resolve_cohort` added after `get_mrn_to_patient_id_map`. Imports are deferred
inside the method body to avoid circular imports (`atrium_sdk.py` is at the top of the
import tree).

```python
def dashboard_resolve_cohort(self, request, request_id: str = ""):
    from atriumdb.dashboard.schemas import MrnCohortResponse
    from atriumdb.dashboard.cohort_resolver import resolve_cohorts_local

    if self.metadata_connection_type == "api":
        raw = self._request(
            "POST", "cohorts/",
            json=request.model_dump(),
            headers={"X-Request-ID": request_id},
        )
        return MrnCohortResponse(**raw)
    return resolve_cohorts_local(self, request, request_id)
```

---

## 6. Server Side

### 6.1 `tests/mock_api/cohort_endpoints.py`

Follows the same pattern as `measures_endpoints.py`, `patient_endpoints.py`, etc.

```python
@router.post("", response_model=MrnCohortResponse)
async def post_cohorts(
    body: CohortDefinitionRequest,
    x_request_id: str = Header(default=""),
    sdk: AtriumSDK = Depends(get_sdk_instance),
) -> MrnCohortResponse:
    return sdk.dashboard_resolve_cohort(body, request_id=x_request_id)
```

### 6.2 `tests/mock_api/app.py`

```python
from tests.mock_api.cohort_endpoints import router as cohort_router
app.include_router(cohort_router, prefix="/cohorts")
```

### 6.3 Manual curl test

```bash
curl -X POST http://localhost:8000/cohorts \
  -H "Content-Type: application/json" \
  -H "X-Request-ID: test-1" \
  -d '{
    "type": "mrn",
    "admissionDateRange": {"start": 1704067200000000000, "end": 1767225599999999999},
    "cohorts": [{"id": "A", "mrnList": ["MRN001234", "MRN005678"]}]
  }'
```

---

## 7. Tests (`tests/test_dashboard_api.py`)

All dashboard API tests live in `test_dashboard_api.py` — separate from `test_api.py` to
avoid importing `test_mit_bih` (which pulls in `wfdb`) just to test cohort endpoints.

**Coverage:**

| Test | What it verifies |
|---|---|
| 1A MRN cohort | Valid MRNs with in-window encounters pass; unknown MRN excluded; MRN with out-of-window encounter excluded |
| 1B location filter | Only patients with in-window encounters in ICU returned |
| 1B sex filter | Only patients matching requested gender returned |
| 1B age filter | Only patients whose age at admission falls in the requested band returned |
| 1B multi-cohort | Multiple cohorts in one request are all resolved correctly |

Each case asserts `sdk.dashboard_resolve_cohort(...)` (direct-DB) and
`api_sdk.dashboard_resolve_cohort(...)` (HTTP via FastAPI) return identical results.

**Run command (Linux / Docker required):**

```bash
# From inside a Linux environment with the venv activated:
cd /path/to/atriumdb/sdk
PYTHONPATH=. python3 -m pytest tests/test_dashboard_api.py::test_api_cohorts -v -s
```

**Why not macOS:** `AtriumSDK.__init__` raises `OSError("AtriumSDK is not currently supported
on macOS.")` at `atrium_sdk.py:169` because the waveform storage layer requires a native C
library with no macOS build. The dashboard code itself does not use the C library, but the
SDK constructor check runs unconditionally before our code is reached.

---

## 8. Open Questions

| # | Issue | Status |
|---|-------|--------|
| 1 | `_request` is private. Called from within `AtriumSDK.dashboard_resolve_cohort` — same class, so this is fine. | Resolved |
| 2 | Connection context manager pattern: `with sdk.sql_handler.connection() as (conn, cursor):` — both handlers yield a tuple. | Resolved — SQL moved to sql_handler layer, pattern used correctly |
| 3 | Location filtering column: `u.name` not `u.type`. | Resolved — `u.name IN (...)` |
| 4 | NULL `bed_id` rows dropped by INNER JOIN. | Resolved — intentional exclusion |
| 5 | SDK patient cache staleness (`_mrn_to_patient_id`). | Ignored for now |
| 6 | Thread safety of `lru_cache` SDK singleton. MariaDB uses a connection pool (safe); SQLite does not. | Open — confirm before go-live |
| 7 | Tests not yet executed due to macOS constraint. | Pending — run in Docker or CI on Linux |
