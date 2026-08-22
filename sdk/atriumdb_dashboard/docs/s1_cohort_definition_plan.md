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
resolve_cohort(sdk, request, request_id)
  │
  ├─ "api" mode
  │     └─ _post_cohorts_remote(...)  # sends X-Request-ID header
  │             └─ HTTP ──► FastAPI POST /cohorts
  │                           └─ resolve_cohorts_local(server_sdk, body, x_request_id)
  │                                 └─ queries.select_patient_encounters(server_sdk, ...)
  │
  └─ direct-DB mode
        └─ resolve_cohorts_local(sdk, request, request_id)
              └─ queries.select_patient_encounters(sdk, ...)
```

---

## 1. File Layout

```
atriumdb/sdk/
├── atriumdb/                                ← UNCHANGED — byte-identical to upstream main
├── atriumdb_dashboard/                      ← NEW top-level package; atriumdb never imports it
│   ├── pyproject.toml                       ← its own distribution: atriumdb-dashboard
│   ├── __init__.py                          ← public API re-exports
│   ├── schemas.py                           ← Pydantic request/response models
│   ├── locations.py                         ← DB-backed location validation (unit table)
│   ├── queries.py                           ← select_patient_encounters (raw SQL, both backends)
│   │                                          + query_patient_encounters
│   │                                          + group_encounters_by_admission
│   ├── cohort_resolver.py                   ← resolve_cohort (entry point), resolve_cohorts_local,
│   │                                          _resolve_mrn_cohort, _resolve_demographic_cohort
│   ├── api/                                 ← FastAPI surface (optional import; needs fastapi)
│   │   ├── __init__.py
│   │   ├── cohort_endpoints.py              ← POST /cohorts handler + get_sdk_instance dependency
│   │   └── app.py                           ← mount_dashboard() / create_dashboard_app()
│   ├── docker/
│   │   ├── Dockerfile                       ← Linux image for running the tests
│   │   ├── Dockerfile.dockerignore          ← BuildKit-scoped ignore file
│   │   └── docker-run-dataset.sh            ← runs the image with a dataset mounted
│   └── docs/
│       ├── s1_cohort_definition_plan.md     ← this document
│       ├── s1_cohort_definition_test_guide.md
│       └── dockersetup.md                   ← Docker usage notes
├── tests/
│   ├── mock_api/                            ← UNCHANGED — byte-identical to upstream main
│   └── atriumdb_dashboard/
│       ├── __init__.py
│       ├── test_dashboard_api.py            ← synthetic-fixture cohort tests (SQLite + HTTP);
│       │                                      calls mount_dashboard(app) at import time
│       ├── test_dashboard_real_data.py      ← real-dataset tests, skipped unless a dataset is mounted
│       └── z_test_local_setup.py            ← seeds MRNs/encounters into a real dataset
└── pyproject.toml                           ← UNCHANGED — byte-identical to upstream main

Nothing outside `atriumdb_dashboard/` and `tests/atriumdb_dashboard/` differs from upstream main.
`atriumdb-dashboard` is a distribution in its own right, versioned separately and installed
alongside the SDK:

    pip install -e ".[testing]"                  # atriumdb
    pip install -e "./atriumdb_dashboard[api]"   # atriumdb-dashboard
```

---

## 2. Pydantic Schemas (`atriumdb_dashboard/schemas.py`)

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
`bed_id` — so `unit` is reached via `bed.unit_id`.

Two exclusions apply unconditionally, ahead of any caller-supplied filter:

- **NULL `bed_id`** — dropped by the INNER JOIN. A pre-admission placeholder with no bed
  assignment is not an admission.
- **NULL `visit_number`** — dropped by `WHERE e.visit_number IS NOT NULL`. Admissions are
  keyed by `(patient_id, visit_number, unit_name)`, so a row without a visit number cannot be
  attributed to a stay; left in, every such row for a patient would collapse into one
  synthetic `None` admission spanning unrelated visits.

A patient whose only encounters lack a visit number therefore resolves to no admissions and
drops out of the cohort. In the MRN path this surfaces in the existing "no encounter in date
range" warning, so it is visible in the logs rather than silent.

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
| NULL visit_number rows | Returned | Excluded (`IS NOT NULL`) |
| ORDER BY | `encounter.id` | `e.start_time` |

---

## 4. Dashboard Layer (`atriumdb_dashboard/`)

### 4.1 `locations.py` and `queries.py`

`queries.py` owns the whole query path: the raw `encounter → bed → unit` SQL plus the two
shaping functions above it. It runs that SQL through `sdk.sql_handler.connection()`, the
backend-agnostic context manager that upstream's `SQLHandler` already declares and both
handlers implement. That is what removes the need to add a `select_patient_encounters`
method to `SQLHandler` and its two subclasses. Both backends use the `?` paramstyle, so a
single statement serves each.

**Locations are validated against the database, not a constant.** There is no hardcoded
vocabulary: whatever the caller sends is checked against `unit.name`, so a deployment that
adds a unit gets it immediately, and deployments with different unit names share this code
unchanged.

`locations.py` holds two functions and one error type:

- **`location_exists(sdk, name)`** — wraps `sdk.sql_handler.select_unit(name=...)`, which
  upstream implements *concretely* on the base `SQLHandler` (not as an abstract method), so
  both backends are covered without touching `atriumdb`. Returns `True` when a unit row
  with that name exists.
- **`validate_location_codes(sdk, codes)`** — checks each distinct code, raising
  `UnknownLocationError` listing every unmatched name. Repeated codes cost one query, not
  several. `None` or an empty list means "no location filter" and issues no query at all.
- **`UnknownLocationError`** — subclasses `ValueError`, so existing `except ValueError`
  handlers still catch it while the endpoint can catch this type specifically.

Unknown locations are **rejected, not dropped**. A location filter narrows a cohort, so
ignoring a bad one silently returns a *wider* result than was asked for, and the caller
cannot tell "nobody was in that unit" from "that unit name was a typo". This differs
deliberately from the MRN path, where an unrecognised MRN is logged and skipped: dropping an
MRN narrows the cohort, which fails safe in a way that widening does not.

**Where validation happens changed.** It used to be a Pydantic `field_validator` on
`DemographicCohort`. A validator runs at model construction, with no SDK and no connection,
so it cannot consult the database. Validation therefore moved to resolve time, in
`_resolve_demographic_cohort`, and `cohort_endpoints.py` catches `UnknownLocationError` and
raises `HTTPException(422)`. The wire contract is unchanged — a bad location is still a 422
naming the offending value — but a *direct* SDK caller now sees the error when they resolve
rather than when they build the request object.

Case sensitivity is delegated to the database's collation, which is **not** uniform: SQLite
compares `TEXT` case-sensitively, while MariaDB's default `utf8mb4_general_ci` does not. A
deployment needing identical behaviour on both should normalise unit names on ingest.

**`query_patient_encounters(sdk, patient_id_list, admit_start_ns, admit_end_ns, locations)`**
— passes `locations` straight through as `unit_name_list` (the location string *is* the
`unit.name`; validation already happened in the resolver), then calls
`queries.select_patient_encounters(sdk, ...)`. Returns one dict per **encounter row**:

```python
{"encounter_id": int, "patient_id": int, "visit_number": str|None,
 "bed_id": int, "unit_id": int, "unit_name": str|None,
 "start_time_ns": int, "end_time_ns": int|None}
```

**`group_encounters_by_admission(encounter_rows)`** — pure Python. Collapses per-encounter
rows into per-admission records keyed by **`(patient_id, visit_number, unit_name)`**:

- `admit_time_ns = MIN(start_time_ns)` across the group's rows
- `discharge_time_ns = MAX(end_time_ns)`; `None` if any row in the group is still open
- NULL `visit_number` rows never arrive here — `select_patient_encounters` filters them in
  SQL. The `None` branch is retained only because this function is public and may be handed
  rows from elsewhere; it groups them and logs a warning that stays may have been merged

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
| 1 | `validate_location_codes(sdk, cohort.location)` against the `unit` table, then `query_patient_encounters(sdk, locations=..., admit_start_ns=..., admit_end_ns=...)`; an unknown location raises `UnknownLocationError`, which the endpoint maps to a 422 |
| 1b | `group_encounters_by_admission(...)` → all admissions kept per patient, grouped by `(visit_number, unit_name)` |
| 2 | `sdk.sql_handler.select_all_patients_in_list(patient_id_list=...)` for demographics; candidates with no MRN on record are logged and dropped |
| 3 | Sex filter — **patient level**: only `"M"` and `"F"` are requestable, so NULL / empty / `'U'` in `patient.gender` matches neither and is dropped |
| 3b | Age filter — **per admission**: `band.start_ns <= (admit_time_ns - dob_ns) <= band.end_ns`. Patients with no `dob` fail every band when an age filter is present |
| 4 | Keep the patient if at least one admission passed; return only the admissions that passed, sorted by `admission_ns` |

Age is evaluated at each admission's own `admit_time_ns` rather than once per patient, so a
patient with two in-range admissions can qualify on the later one and not the earlier one —
only the qualifying admissions appear in the response. All active filters are AND-ed; values
within a filter are OR-ed.

---

## 5. Dual-Mode Entry Point (`atriumdb_dashboard/cohort_resolver.py`)

`resolve_cohort(sdk, request, request_id)` takes the SDK as its first argument rather than
being a method on it, which is what keeps `atriumdb` untouched. The dependency runs one way
only: `atriumdb_dashboard` imports from the SDK (and only behind `TYPE_CHECKING` for the type
itself), while `atriumdb` never imports the dashboard.

```python
def resolve_cohort(sdk, request, request_id: str):
    if request_id is None or not str(request_id).strip():
        _LOGGER.error(...)
        raise ValueError("request_id must be a non-empty string.")

    if sdk.metadata_connection_type == "api":
        return _post_cohorts_remote(sdk, request, request_id)

    return resolve_cohorts_local(sdk, request, request_id)
```

The remote path issues its own `requests.post` instead of calling `AtriumSDK._request`.
`_request` builds its header dict from scratch, so it cannot carry `X-Request-ID`; making it
merge caller headers would mean a one-line edit to `atrium_sdk.py`. `_post_cohorts_remote`
restates the URL construction, the 30-second token-refresh check, and the non-200 error
handling so the SDK stays byte-identical to upstream. That duplication is the deliberate
cost of the zero-diff constraint — if `_request` ever changes its auth or refresh behaviour,
this function must be updated to match.

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

### 6.1 `atriumdb_dashboard/api/cohort_endpoints.py`

Follows the same pattern as `measures_endpoints.py`, `patient_endpoints.py`, etc.

```python
@router.post("", response_model=MrnCohortResponse)
async def post_cohorts(
    body: CohortDefinitionRequest,
    x_request_id: str = Header(..., min_length=1),
    sdk: AtriumSDK = Depends(get_sdk_instance),
) -> MrnCohortResponse:
    return resolve_cohort(sdk, body, request_id=x_request_id)
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

### 7.1 `tests/atriumdb_dashboard/test_dashboard_statistics_api.py` — synthetic fixtures

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

Each case asserts that `resolve_cohort(sdk, ...)` (direct-DB) and
`resolve_cohort(api_sdk, ...)` (HTTP via FastAPI) return the same resolved patients.

### 7.2 `tests/atriumdb_dashboard/test_dashboard_real_data.py` — real dataset

Runs the same resolver against a mounted AtriumDB dataset, with expected MRN sets and full
expected `Admission` records (admission, discharge and location) as module-level constants.
Skipped automatically when `ATRIUMDB_DATASET_LOCATION` is unset, so it does not affect a
normal test run. See `s1_cohort_definition_test_guide.md`.

### 7.3 Run command (Linux / Docker required)

```bash
cd /path/to/atriumdb/sdk
PYTHONPATH=. python3 -m pytest tests/atriumdb_dashboard/test_dashboard_statistics_api.py::test_api_cohorts -v -s
```

`AtriumSDK.__init__` raises `OSError("AtriumSDK is not currently supported on macOS.")` at
`atrium_sdk.py:171`, because the waveform storage layer requires a native C library with no
macOS build. The dashboard code itself does not use that library, but the constructor check
runs unconditionally before any dashboard code is reached — so the tests run in the Docker
image (`Dockerfile`) or on Linux CI.
