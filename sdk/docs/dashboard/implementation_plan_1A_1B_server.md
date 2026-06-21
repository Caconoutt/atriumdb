# Implementation Plan: Priority 1A, 1B + Server API

This plan translates the design in `atriumDB_sdk_design_step_1_v4_refined.md` into concrete file
changes. It follows the same dual-mode pattern the existing SDK uses throughout: one call site
works in both direct-DB mode (local) and API mode (remote over HTTP), with the server being a
thin FastAPI wrapper around the same local logic.

---

## 0. The Dual-Mode Pattern (how the existing SDK does it)

The whole SDK is built around one branching rule. Every method that touches data does this:

```python
# atrium_sdk.py:3040 — get_mrn_to_patient_id_map as the canonical example
if self.metadata_connection_type == "api":
    return self._request("GET", "patients/mrn|...")   # HTTP to server
else:
    # hit sql_handler directly
    ...
```

The caller never knows or cares which path ran. The server is just a FastAPI process running
the same SDK in direct-DB mode, so its endpoints simply call the same local functions and
return JSON.

The cohort resolver follows **exactly** the same structure, with the dual-mode method living
on `AtriumSDK` itself — same as `get_mrn_to_patient_id_map`, `get_all_measures`, etc.:

```
Caller:  sdk.dashboard_resolve_cohort(request)
           │
           ├─ self.metadata_connection_type == "api"
           │       └─ self._request("POST", "cohorts/", json=request.model_dump())
           │               └─ HTTP ──► FastAPI server
           │                               └─ resolve_cohorts_local(server_sdk, request)
           │                                       └─ sql_handler / db_queries
           │
           └─ else (direct-DB)
                   └─ resolve_cohorts_local(self, request)
                           └─ sql_handler / db_queries
```

`resolve_cohorts_local` is the function that does the real work. Both paths converge on it —
on the server side via HTTP, on the direct-DB side by calling it directly.

---

## 1. File Layout

```
atriumdb/sdk/
├── atriumdb/
│   ├── atrium_sdk.py                 ← MODIFIED: add dashboard_resolve_cohort() method
│   └── dashboard/                    ← NEW package — pure logic, no FastAPI
│       ├── __init__.py
│       ├── schemas.py                ← Pydantic request/response models
│       ├── encounter_queries.py             ← query_patient_encounters + group_encounters_by_visit
│       └── cohort_resolver.py        ← resolve_cohorts_local (direct-DB, no HTTP awareness)
└── tests/mock_api/                   ← existing API layer — all FastAPI wiring lives here
    ├── app.py                        ← MODIFIED: add cohort_router at /cohorts
    ├── cohort_endpoints.py           ← NEW: POST /cohorts handler
    ├── sdk_dependency.py             ← unchanged: provides get_sdk_instance()
    └── ...                           ← existing endpoint files unchanged
```

No new dependencies: `fastapi`, `pydantic`, and `uvicorn` are already in `pyproject.toml`.

---

## 2. Shared Pydantic Schemas (`atriumdb/dashboard/schemas.py`)

These models serve double duty: the client entry point serialises them to JSON for the HTTP
call; the server endpoint deserialises the incoming JSON back into the same models.

```python
from pydantic import BaseModel
from typing import Literal

class AdmissionDateRange(BaseModel):
    start: int   # epoch ns
    end:   int   # epoch ns

class AgeBand(BaseModel):
    startNs: int
    endNs:   int

class MrnCohort(BaseModel):
    id:      str
    mrnList: list[str]

class DemographicCohort(BaseModel):
    id:         str
    age:        list[AgeBand] | None = None
    sex:        list[str]     | None = None
    location:   list[str]     | None = None
    valueRange: dict          | None = None   # reserved for future use

class CohortDefinitionRequest(BaseModel):
    type:               Literal["mrn", "demographic"]
    admissionDateRange: AdmissionDateRange
    cohorts:            list[MrnCohort] | list[DemographicCohort]

class ResolvedCohort(BaseModel):
    id:      str
    mrnList: list[str]

class MrnCohortResponse(BaseModel):
    requestId: str
    cohorts:   list[ResolvedCohort]
```

---

## 3. Custom DB Functions (`atriumdb/dashboard/encounter_queries.py`)

These only run on the direct-DB side. They never run on a client in API mode.

### 3.1 Connection pattern — design-doc correction

The design doc shows:
```python
with sdk.sql_handler.connection() as conn:
    cursor = conn.cursor()   # ← WRONG
```

Both `SQLiteHandler.connection()` (`sqlite_handler.py:68`) and
`MariaDBHandler.connection()` (`maria_handler.py:152`) are context managers that yield a
**`(conn, cursor)` tuple**. The correct pattern is:

```python
with sdk.sql_handler.connection() as (conn, cursor):
    cursor.execute(sql, tuple(params))
    rows = cursor.fetchall()
```

### 3.1b `select_patient_encounters` on `SQLHandler` (the actual SQL)

The raw encounter JOIN lives in the SQL handler layer, not in `encounter_queries.py`. This follows
the same pattern as `select_all_patients_in_list`, `select_device_patients`, etc.:

- **Abstract interface** — `sql_handler.py`: `@abstractmethod select_patient_encounters(patient_id_list, admit_start_ns, admit_end_ns, unit_name_list)`
- **SQLite implementation** — `sqlite_handler.py`: uses `self.sqlite_db_connection()` and `?` placeholders
- **MariaDB implementation** — `maria_handler.py`: uses `self.maria_db_connection()` and `?` placeholders

Both implementations:
- Build `arg_tuple = ()` and `where_clauses = []`
- Use `','.join(['?'] * len(...))` for IN placeholders
- Append `WHERE ... AND ...` only if `where_clauses` is non-empty
- Add `ORDER BY e.start_time ASC`
- Return raw tuples: `(encounter_id, patient_id, visit_number, bed_id, unit_id, unit_name, start_time_ns, end_time_ns)`

`query_patient_encounters` in `encounter_queries.py` only translates location codes via
`LOCATION_LOOKUP`, then delegates to `sdk.sql_handler.select_patient_encounters(...)`.

### 3.2 `LOCATION_LOOKUP`

Location is matched against `u.name` (not `u.type`). The lookup maps API location codes to
the exact strings stored in `unit.name`:

```python
LOCATION_LOOKUP: dict[str, list[str]] = {
    "ICU": ["ICU"],
    "OR":  ["OR"],
}
```

The `WHERE` clause therefore uses `u.name IN (...)`, not `u.type IN (...)`.

### 3.3 `query_patient_encounters`

Thin wrapper in `encounter_queries.py`. Translates API location codes to `unit.name` values via
`LOCATION_LOOKUP`, then calls `sdk.sql_handler.select_patient_encounters(...)`. The actual
SQL (JOIN chain, placeholders, WHERE construction) lives entirely in the SQL handler
implementations (see §3.1b).

```python
def query_patient_encounters(sdk, patient_id_list=None, admit_start_ns=None,
                              admit_end_ns=None, locations=None):
    unit_name_list = None
    if locations:
        unit_name_list = []
        for code in locations:
            if code not in LOCATION_LOOKUP:
                raise ValueError(f"Unknown location code {code!r}")
            unit_name_list.extend(LOCATION_LOOKUP[code])

    rows = sdk.sql_handler.select_patient_encounters(
        patient_id_list=patient_id_list,
        admit_start_ns=admit_start_ns,
        admit_end_ns=admit_end_ns,
        unit_name_list=unit_name_list,
    )
    return [{"encounter_id": row[0], "patient_id": row[1], ...} for row in rows]
```

**Schema refs** (`maria_tables.py`):
- `encounter (id, patient_id, bed_id, start_time, end_time, visit_number)` — lines 143–155
- `bed (id, unit_id, name)` — lines 102–107
- `unit (id, institution_id, name, type)` — lines 92–98

`encounter.bed_id` is NULLable (line 146). `INNER JOIN bed` drops rows where `bed_id` is
NULL — pre-admission placeholder rows without a bed are excluded by design.

### 3.4 `group_encounters_by_visit`

Pure Python, no SDK calls. Collapses per-encounter rows into per-visit records:
`admit_time_ns = MIN(start_time)`, `discharge_time_ns = MAX(end_time)` per
`(patient_id, visit_number)`. Log a warning when `visit_number` is NULL (design doc §7).

---

## 4. Direct-DB Resolver (`atriumdb/dashboard/cohort_resolver.py`)

Contains `resolve_cohorts_local` — the function that does the real work for both 1A and 1B.
It is called directly in direct-DB mode and called by the server endpoint in API mode.
It has **no knowledge of HTTP** — it always assumes `sdk` is a local, direct-DB instance.

```python
def resolve_cohorts_local(
    sdk: AtriumSDK,
    request: CohortDefinitionRequest,
    request_id: str = "",
) -> MrnCohortResponse:
    resolved = []
    if request.type == "mrn":
        for cohort in request.cohorts:
            mrns = _resolve_mrn_cohort(sdk, cohort, request.admissionDateRange, request_id)
            resolved.append(ResolvedCohort(id=cohort.id, mrnList=mrns))
    elif request.type == "demographic":
        for cohort in request.cohorts:
            mrns = _resolve_demographic_cohort(sdk, cohort, request.admissionDateRange)
            resolved.append(ResolvedCohort(id=cohort.id, mrnList=mrns))
    return MrnCohortResponse(requestId=request_id, cohorts=resolved)
```

### 4.1 1A — `_resolve_mrn_cohort`

| Step | Code | SDK hook |
|------|------|----------|
| 0 — normalise | `mrn_input = [m.strip() for m in cohort.mrnList]` | — |
| 1 — existence check | `sdk.get_mrn_to_patient_id_map(mrn_list=mrn_input)` | `atrium_sdk.py:3027` — hits `patient` table; absent MRNs omitted from result |
| 1b — log missing | `logger.warning(...)` for unrecognised MRNs | — |
| 2 — admission range check | `query_patient_encounters(sdk, patient_id_list=..., admit_start_ns=..., admit_end_ns=...)` | `encounter_queries.py` — no `locations` filter |
| 2b — collapse visits | `group_encounters_by_visit(encounter_rows)` | `encounter_queries.py` |
| 2c — filter + log | Reverse-map surviving patient IDs back to MRNs; log excluded ones | — |

### 4.2 1B — `_resolve_demographic_cohort`

| Step | Code | SDK hook |
|------|------|----------|
| 1 — candidate encounters | `query_patient_encounters(sdk, locations=cohort.location, admit_start_ns=..., admit_end_ns=...)` | `encounter_queries.py` |
| 1b — collapse + reference admission | `group_encounters_by_visit(...)` → pick earliest `admit_time_ns` per patient | `encounter_queries.py` + pure Python |
| 2 — fetch demographics | `sdk.sql_handler.select_all_patients_in_list(patient_id_list=...)` | `maria_handler.py:700` / `sqlite_handler.py:632` — returns `(id, mrn, gender, dob, ...)` tuples |
| 2b — index | `{row[0]: {"mrn": row[1], "gender": row[2], "dob_ns": row[3]}}` | Column order fixed at `maria_handler.py:703` |
| 3 — age filter | `age_at_admission_ns = admit_time_ns - dob_ns`; check bands | Pure Python; patients with `dob_ns=None` excluded when age filter present |
| 3b — sex filter | `"U"` matches NULL / empty / `'U'` in `patient.gender` (VARCHAR(1), schema line 113) | Pure Python |

`select_all_patients_in_list` note: it takes `patient_id_list` **or** `mrn_list`, not both. Here
we always pass `patient_id_list` (the IDs from Step 1). This avoids a second MRN lookup.

---

## 5. Dual-Mode Method on `AtriumSDK` (`atriumdb/atrium_sdk.py`)

Add `dashboard_resolve_cohort` as a method on `AtriumSDK`, placed alongside the other
patient/metadata methods (after `get_mrn_to_patient_id_map` at line 3027 is a natural home).
The naming prefix `dashboard_` makes its scope explicit and avoids collision with any future
generic SDK cohort concept.

```python
# atrium_sdk.py — add after get_mrn_to_patient_id_map (~line 3080)
def dashboard_resolve_cohort(
    self,
    request: "CohortDefinitionRequest",
    request_id: str = "",
) -> "MrnCohortResponse":
    from atriumdb.dashboard.schemas import MrnCohortResponse
    from atriumdb.dashboard.cohort_resolver import resolve_cohorts_local

    if self.metadata_connection_type == "api":
        raw = self._request(
            "POST",
            "cohorts/",
            json=request.model_dump(),
            headers={"X-Request-ID": request_id},
        )
        return MrnCohortResponse(**raw)
    else:
        return resolve_cohorts_local(self, request, request_id)
```

Imports are deferred (inside the method body) to avoid a circular import — `atrium_sdk.py`
is imported by nearly everything, so top-level imports of `dashboard.*` there would create
a cycle.

**Call flow summary:**

```
# Direct-DB (local)
sdk = AtriumSDK(metadata_connection_type="mariadb", connection_params={...})
result = sdk.dashboard_resolve_cohort(request)
  └─ resolve_cohorts_local(self, request)
       └─ query_patient_encounters → sql_handler.connection() → MariaDB

# API mode (remote)
sdk = AtriumSDK(metadata_connection_type="api", api_url="http://...", token=...)
result = sdk.dashboard_resolve_cohort(request)
  └─ self._request("POST", "cohorts/", ...)     # atrium_sdk.py:5312
       └─ HTTP POST → FastAPI /cohorts
            └─ resolve_cohorts_local(server_sdk, request)
                 └─ query_patient_encounters → sql_handler.connection() → MariaDB
```

The caller's code is **identical** in both cases.

---

## 6. Server Side

### 6.1 Cohort Endpoint (`tests/mock_api/cohort_endpoints.py`)

Follows the same pattern as every other file in `tests/mock_api/`. Uses the existing
`get_sdk_instance` from `sdk_dependency.py` — no new dependency machinery needed.

```python
from fastapi import APIRouter, Depends, Header
from atriumdb import AtriumSDK
from atriumdb.dashboard.schemas import CohortDefinitionRequest, MrnCohortResponse
from tests.mock_api.sdk_dependency import get_sdk_instance

router = APIRouter()

@router.post("", response_model=MrnCohortResponse)
async def post_cohorts(
    body: CohortDefinitionRequest,
    x_request_id: str = Header(default=""),
    sdk: AtriumSDK = Depends(get_sdk_instance),
):
    return sdk.dashboard_resolve_cohort(body, request_id=x_request_id)
```

### 6.2 App registration (`tests/mock_api/app.py`)

Add one import and one `include_router` call alongside the existing routers:

```python
from tests.mock_api.cohort_endpoints import router as cohort_router
app.include_router(cohort_router, prefix="/cohorts")
```

### 6.3 Running the server

```bash
uvicorn tests.mock_api.app:app --host 0.0.0.0 --port 8000
```

Example call:
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

## 7. Implementation Order

1. `atriumdb/dashboard/__init__.py` — empty, registers the package
2. `dashboard/schemas.py` — Pydantic models (no SDK dependency, write and test first)
3. `dashboard/encounter_queries.py` — `LOCATION_LOOKUP`, `query_patient_encounters`,
   `group_encounters_by_visit`
4. `dashboard/cohort_resolver.py` — `resolve_cohorts_local`, `_resolve_mrn_cohort`,
   `_resolve_demographic_cohort`
5. `atrium_sdk.py` — add `dashboard_resolve_cohort` method with deferred imports
6. `tests/mock_api/cohort_endpoints.py` — `POST /cohorts` router
7. `tests/mock_api/app.py` — add `include_router(cohort_router, prefix="/cohorts")`
8. Smoke-test with a SQLite test DB (unit-test `cohort_resolver` directly), then wire the
   server and test with `metadata_connection_type="api"` pointing at it (same pattern as
   `test_api.py:53` using `app.dependency_overrides`)

---

## 8. Open Questions

| # | Issue | Status |
|---|-------|--------|
| 1 | **`_request` is private** (`atrium_sdk.py:5312`). Called from within `AtriumSDK.dashboard_resolve_cohort`, which is fine — same class. | Resolved — internal call is appropriate. |
| 2 | **Connection context manager pattern.** Follow existing code format: `with sdk.sql_handler.connection() as (conn, cursor):` — both handlers yield a tuple. | Resolved — follow existing code. |
| 3 | **Location filtering column.** Use `u.name` (not `u.type`) to match `"ICU"` / `"OR"`. | Resolved — `u.name IN (...)` confirmed. |
| 4 | **NULL `bed_id` rows.** `INNER JOIN bed` drops encounters where `bed_id` is NULL. | Resolved — drop them; pre-admission placeholder rows are excluded by design. |
| 5 | **SDK patient cache staleness** (`_mrn_to_patient_id`). | Ignored for now. |
| 6 | **Thread safety** of `lru_cache` SDK singleton. MariaDB uses a connection pool (safe); SQLite is not. | Open — confirm pool size is adequate for expected concurrency before go-live. |
