# Cohort Definition — Priority 1A, 1B + Server API

This document describes the implementation as it exists in the codebase: the dual-mode SDK
method, the SQL handler additions, the dashboard resolution layer, and the FastAPI endpoint.
It follows the dual-mode pattern the existing SDK uses throughout — one call site works in
both direct-DB mode (local) and API mode (remote over HTTP), with the server being a thin
FastAPI wrapper around the same local logic.

---

## 0. The Dual-Mode Pattern

Every SDK method branches on `metadata_connection_type`:

```python
if self.metadata_connection_type == "api":
    return self._request("POST", "cohorts/", json=request.model_dump(by_alias=True), ...)
else:
    return resolve_cohorts_local(self, request, request_id)
```

The caller's code is **identical** in both cases. The server is a FastAPI process running the
same SDK in direct-DB mode — its endpoint calls the same local function and returns JSON.

```
sdk.dashboard_resolve_cohort(request, request_id)
  │
  ├─ "api" mode
  │     └─ self._request("POST", "cohorts/", ..., headers={"X-Request-ID": request_id})
  │             └─ HTTP ──► FastAPI POST /cohorts
  │                           └─ resolve_cohorts_local(server_sdk, body, x_request_id)
  │                                 └─ sql_handler.select_patient_encounters(...)
  │
  └─ direct-DB mode
        └─ resolve_cohorts_local(self, request, request_id)
              └─ sql_handler.select_patient_encounters(...)
```

---

## 1. File Layout

```
atriumdb/sdk/
├── atriumdb/
│   ├── atrium_sdk.py                        ← MODIFIED: dashboard_resolve_cohort(); _request() header merge
│   ├── sql_handler/
│   │   ├── sql_handler.py                   ← MODIFIED: select_patient_encounters() abstract method
│   │   ├── sqlite/sqlite_handler.py         ← MODIFIED: select_patient_encounters() implementation
│   │   └── maria/maria_handler.py           ← MODIFIED: select_patient_encounters() implementation
│   └── dashboard/                           ← NEW package — pure logic, no FastAPI
│       ├── __init__.py
│       ├── schemas.py                       ← Pydantic request/response models
│       ├── encounter_queries.py             ← LOCATION_LOOKUP + query_patient_encounters + group_encounters_by_admission
│       └── cohort_resolver.py               ← resolve_cohorts_local, _resolve_mrn_cohort, _resolve_demographic_cohort
├── tests/
│   ├── mock_api/
│   │   ├── app.py                           ← MODIFIED: include_router(cohort_router, prefix="/cohorts")
│   │   ├── cohort_endpoints.py              ← NEW: POST /cohorts FastAPI handler
│   │   └── sdk_dependency.py                ← unchanged
│   ├── test_dashboard_api.py                ← NEW: synthetic-fixture cohort tests (SQLite + HTTP)
│   ├── test_dashboard_real_data.py          ← NEW: real-dataset tests, skipped unless a dataset is mounted
│   └── z_test_local_setup.py                ← NEW: seeds MRNs/encounters into a real dataset
├── Dockerfile                               ← NEW: Linux image for running the tests
├── .dockerignore                            ← NEW
├── docker-run-dataset.sh                    ← NEW: runs the image with a dataset mounted
├── dockersetup.md                           ← NEW: Docker usage notes
└── docs/dashboard/
    ├── s1_cohort_definition_plan.md         ← this document
    └── s1_cohort_definition_test_guide.md   ← how to run the real-data tests
```

---

## 2. Pydantic Schemas (`atriumdb/dashboard/schemas.py`)

Nine models. All inherit `_Base`, which sets `alias_generator=to_camel` and
`populate_by_name=True` — snake_case in Python, camelCase on the wire. They serve both the
client (serialise to JSON for HTTP) and the server (deserialise incoming JSON). No SDK
dependency, safe to import anywhere.

| Model | Purpose |
|---|---|
| `AdmissionDateRange` | `start`/`end` epoch ns window, inclusive both sides |
| `Admission` | One qualifying admission: `admissionNs`, `dischargeNs` (nullable), `location` (nullable) |
| `PatientAdmission` | `mrn` + `admissions`: all that patient's qualifying admissions, ascending by `admissionNs` |
| `AgeBand` | `startNs`/`endNs` age range in nanoseconds |
| `MrnCohort` | `id` + `mrnList` for 1A requests |
| `DemographicCohort` | `id` + optional `age`, `sex`, `location`, `valueRange` for 1B |
| `CohortDefinitionRequest` | Top-level request: `type` (`"mrn"`/`"demographic"`), `admissionDateRange`, `cohorts` |
| `ResolvedCohort` | `id` + `patients`: list of `PatientAdmission` |
| `MrnCohortResponse` | `requestId` + list of `ResolvedCohort` |

### 2.1 Response shape

A resolved cohort returns **patients with their admissions**, not a flat MRN list:

```json
{
  "requestId": "a3f9c1d2-4b8e-4f1a-9c3d-2e7b6f0a1d5e",
  "cohorts": [
    {
      "id": "1",
      "patients": [
        {
          "mrn": "MRN001234",
          "admissions": [
            {"admissionNs": 1608553652000000000, "dischargeNs": 1610082209000000000, "location": "ICU"}
          ]
        }
      ]
    }
  ]
}
```

`admissionNs` / `dischargeNs` / `location` all fall out of the `encounter → bed → unit` join
that already runs during filtering, so carrying them into the response costs nothing and
saves downstream consumers (statistics endpoint, Data Records table) from re-running the
same join. `dischargeNs` is `null` when the stay is still open; `location` is `null` when the
encounter has no unit name recorded.

### 2.2 `type` / cohort-class validation

`cohorts` is typed `list[MrnCohort] | list[DemographicCohort]`. Pydantic resolves that union
by **shape**, not by the `type` field, so a `"demographic"` request carrying `mrnList` entries
would parse as `MrnCohort` and only fail later inside the resolver. A
`@model_validator(mode="after")` on `CohortDefinitionRequest` rejects any request whose
cohort classes do not match its `type`, turning that into a validation error at the request
boundary (422 over HTTP).

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

JOIN chain: `encounter → bed → unit`. `encounter` carries no location column of its own — only
`bed_id` — so `unit` is reached via `bed.unit_id`. The INNER JOIN drops rows where `bed_id` is
NULL (pre-admission placeholder rows with no bed assignment are excluded by design).

All four arguments are optional and AND-ed when supplied. Note that an argument is only
applied when it is not `None` **and**, for the list arguments, non-empty: passing
`patient_id_list=[]` applies no patient filter rather than matching nothing.

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

Two functions and one lookup table. Contains no raw SQL — delegates to `sql_handler`.

**`LOCATION_LOOKUP`** — maps API location codes to exact `unit.name` values in the DB.
Filter is on `unit.name`, not `unit.type`.

```python
LOCATION_LOOKUP: dict[str, list[str]] = {
    "ICU": ["ICU"],
    "OR":  ["OR"],
}
```

**`query_patient_encounters(sdk, patient_id_list, admit_start_ns, admit_end_ns, locations)`**
— translates `locations` (API codes) to `unit_name_list` via `LOCATION_LOOKUP`, raising
`ValueError` on an unknown code, then calls
`sdk.sql_handler.select_patient_encounters(...)`. Returns one dict per **encounter row**:

```python
{"encounter_id": int, "patient_id": int, "visit_number": str|None,
 "bed_id": int, "unit_id": int, "unit_name": str|None,
 "start_time_ns": int, "end_time_ns": int|None}
```

**`group_encounters_by_admission(encounter_rows)`** — pure Python. Collapses per-encounter
rows into per-admission records keyed by **`(patient_id, visit_number, unit_name)`**:

- `admit_time_ns = MIN(start_time_ns)` across the group's rows
- `discharge_time_ns = MAX(end_time_ns)`; `None` if any row in the group is still open
- NULL `visit_number` rows group under a `None` visit number with the same rules, and each
  such row is logged as a warning — it indicates incomplete data

#### Why `unit_name` is part of the key

A hospital stay produces one `encounter` row per bed, so one visit spans many rows. The unit
is already in hand for two reasons: it is what the 1B `location` filter matches on
(`u.name IN (...)`), and it is what populates `Admission.location` in the response.

Including it in the grouping key means:

- Bed-to-bed moves **within** a unit collapse into one admission — the extra rows carry no
  new location information.
- A transfer **between** units stays as two admissions, each with its own admit/discharge
  time and one unambiguous location. This is what lets a caller requesting several locations
  (e.g. ICU *and* OR) still see which unit each patient was actually in; a visit-level
  grouping could only report the *set* of units the stay touched, which cannot be expressed
  in a single `location` field.

Edge case: a patient who goes ICU → OR → ICU **under one `visit_number`** produces two
admissions, with the two ICU segments merged into a single ICU admission whose
admit/discharge span the OR gap. In practice ADT assigns a new `visit_number` on re-admission,
which separates the segments naturally; the merge only occurs where the same `visit_number` is
reused across a return to the same unit.

Removing `unit_name` from the key is not an option while `Admission.location` exists — a
visit-level group can touch several units and so has no single location to report.

### 4.2 `cohort_resolver.py`

Entry point: `resolve_cohorts_local(sdk, request, request_id)` — dispatches to 1A or 1B and
assembles a `MrnCohortResponse`. No HTTP awareness; always assumes `sdk` is a direct-DB
instance. Cohorts are processed in request order and results preserve that order; a cohort
with no qualifying patients is still returned, with an empty `patients` list.

**1A — `_resolve_mrn_cohort`** → `list[PatientAdmission]`

| Step | Action |
|---|---|
| 0 | Normalise: `m.strip()` for each MRN |
| 1 | `sdk.get_mrn_to_patient_id_map(mrn_list=...)` — absent MRNs logged (with cohort id) and dropped |
| 2 | `query_patient_encounters(sdk, patient_id_list=..., admit_start_ns=..., admit_end_ns=...)` — no location filter, any unit qualifies |
| 2b | `group_encounters_by_admission(...)` — patients with no in-window encounter logged and dropped |
| 3 | Return one `PatientAdmission` per surviving patient, carrying every qualifying admission sorted by `admission_ns` |

**1B — `_resolve_demographic_cohort`** → `list[PatientAdmission]`

| Step | Action |
|---|---|
| 1 | `query_patient_encounters(sdk, locations=..., admit_start_ns=..., admit_end_ns=...)`; an unknown location code is logged and re-raised |
| 1b | `group_encounters_by_admission(...)` → all admissions kept per patient, grouped by `(visit_number, unit_name)` |
| 2 | `sdk.sql_handler.select_all_patients_in_list(patient_id_list=...)` for demographics; candidates with no MRN on record are logged and dropped |
| 3 | Sex filter — **patient level**: `"U"` matches NULL / empty / `'U'` in `patient.gender` |
| 3b | Age filter — **per admission**: `band.start_ns <= (admit_time_ns - dob_ns) <= band.end_ns`. Patients with no `dob` fail every band when an age filter is present |
| 4 | Keep the patient if at least one admission passed; return only the admissions that passed, sorted by `admission_ns` |

Age is evaluated at each admission's own `admit_time_ns` rather than once per patient, so a
patient with two in-range admissions can qualify on the later one and not the earlier one —
only the qualifying admissions appear in the response. All active filters are AND-ed; values
within a filter are OR-ed.

---

## 5. Dual-Mode Method (`atriumdb/atrium_sdk.py`)

`dashboard_resolve_cohort(request, request_id)` is added after `get_mrn_to_patient_id_map`.
`atriumdb.dashboard` imports nothing from `atrium_sdk` at runtime (the SDK type is behind
`TYPE_CHECKING`), so the schema and resolver imports sit at module top level.

```python
def dashboard_resolve_cohort(self, request, request_id: str):
    if request_id is None or not str(request_id).strip():
        _LOGGER.error(...)
        raise ValueError("request_id must be a non-empty string.")

    if self.metadata_connection_type == "api":
        raw = self._request(
            "POST", "cohorts/",
            json=request.model_dump(by_alias=True),
            headers={"X-Request-ID": request_id},
        )
        return MrnCohortResponse.model_validate(raw)

    return resolve_cohorts_local(self, request, request_id)
```

**`by_alias=True`** is required: the server parses camelCase (`admissionDateRange`,
`mrnList`), which is what the alias generator produces.

### 5.1 `request_id` and log correlation

`request_id` exists so that a dashboard-side request can be matched against AtriumDB's own
logs. Every warning emitted during resolution is prefixed `[<request_id>]` and names the
cohort id, so the MRNs dropped for a given request can be found directly.

It is a **required** parameter with no default. A `None`, empty, or whitespace-only value is
rejected with `ValueError` before any query runs — no resolution work is ever performed that
cannot be traced back to a caller. Over HTTP the same rule is enforced one layer earlier:
the endpoint declares `Header(..., min_length=1)`, so a missing or blank `X-Request-ID`
returns 422 without reaching the SDK.

In API mode the id travels as the `X-Request-ID` header. Carrying it required a change to
`AtriumSDK._request`, which previously discarded caller headers:

```python
headers = {'Authorization': f"Bearer {self.token}", **kwargs.pop('headers', {})}
```

The server echoes the value back as `requestId` on the response, so a client can confirm
which request a payload belongs to.

---

## 6. Server Side

### 6.1 `tests/mock_api/cohort_endpoints.py`

Follows the same pattern as `measures_endpoints.py`, `patient_endpoints.py`, etc.

```python
@router.post("", response_model=MrnCohortResponse)
async def post_cohorts(
    body: CohortDefinitionRequest,
    x_request_id: str = Header(..., min_length=1),
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

Response:

```json
{
  "requestId": "test-1",
  "cohorts": [
    {"id": "A", "patients": [
      {"mrn": "MRN001234", "admissions": [
        {"admissionNs": 1704153600000000000, "dischargeNs": 1704240000000000000, "location": "ICU"}
      ]}
    ]}
  ]
}
```

Omitting `X-Request-ID` returns 422.

---

## 7. Tests

Dashboard tests live in their own files, separate from `test_api.py`, to avoid importing
`test_mit_bih` (which pulls in `wfdb`) just to test cohort endpoints.

### 7.1 `tests/test_dashboard_api.py` — synthetic fixtures

Creates a SQLite dataset under `tests/test_datasets/`, starts the mock FastAPI app on port
8123, and inserts an institution, one ICU unit, one bed, and three patients: two with
in-window ICU encounters (male 25y, female 35y at admission) and one whose encounter falls
outside the window.

| Scenario | What it verifies |
|---|---|
| 1A MRN cohort | Valid MRNs with in-window encounters pass; unknown MRN excluded; MRN with out-of-window encounter excluded |
| 1B location filter | Only patients with in-window encounters in ICU returned |
| 1B sex filter | Only patients matching requested gender returned |
| 1B age filter | Only patients whose age at admission falls in the requested band returned |
| 1B multi-cohort | Multiple cohorts in one request are all resolved correctly |

Each case asserts that `sdk.dashboard_resolve_cohort(...)` (direct-DB) and
`api_sdk.dashboard_resolve_cohort(...)` (HTTP via FastAPI) return the same resolved patients.

### 7.2 `tests/test_dashboard_real_data.py` — real dataset

Runs the same resolver against a mounted AtriumDB dataset, with expected MRN sets and full
expected `Admission` records (admission, discharge and location) as module-level constants.
Skipped automatically when `ATRIUMDB_DATASET_LOCATION` is unset, so it does not affect a
normal test run. See `s1_cohort_definition_test_guide.md`.

### 7.3 Run command (Linux / Docker required)

```bash
cd /path/to/atriumdb/sdk
PYTHONPATH=. python3 -m pytest tests/test_dashboard_api.py::test_api_cohorts -v -s
```

`AtriumSDK.__init__` raises `OSError("AtriumSDK is not currently supported on macOS.")` at
`atrium_sdk.py:171`, because the waveform storage layer requires a native C library with no
macOS build. The dashboard code itself does not use that library, but the constructor check
runs unconditionally before any dashboard code is reached — so the tests run in the Docker
image (`Dockerfile`) or on Linux CI.
