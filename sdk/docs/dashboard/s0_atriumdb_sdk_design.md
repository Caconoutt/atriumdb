# AtriumDB SDK Implementation Design

This document translates each priority/step 1 of `atriumDB_requirement.md` into concrete AtriumDB SDK calls, showing the exact functions, their chaining order, and what each call produces. It is the implementation-side counterpart to the API contract.

---

## Assumptions and Conventions

1. **Deployment mode:** The AtriumDB team operates an **internal server** with direct database access. Direct-DB mode (`metadata_connection_type="mariadb"` / `"mysql"` / `"sqlite"`) is therefore guaranteed for this integration, and the custom SQL functions in this document can be hosted on that server without additional setup. The SDK's `metadata_connection_type="api"` mode — in which no in-process database connection exists — is not applicable here and is out of scope.
2. **Timestamps:** All times exchanged with these functions are Unix epoch **nanoseconds, UTC**, matching `encounter.start_time`, `patient.dob`, etc. (all `BIGINT` ns in the schema).
3. **Age bands:** The API contract carries age bounds in **nanoseconds**. The dashboard server is responsible for the full conversion before serialisation: `totalNs = (year × 365 + month × 30) × 86_400_000_000_000` (the `year × 365 + month × 30` day approximation is a deliberate convention, stated in the contract). AtriumDB receives and uses nanosecond values directly — **no unit conversion is performed on the AtriumDB side**. No dob range is pre-computed or carried in the request — AtriumDB assesses age by computing each candidate patient's `dob` against their actual admission time once it's known (see Priority 1B).
4. **Reference admission rule:** When a patient has multiple admissions inside `admissionDateRange`, the **earliest in-range admission** is the patient's reference admission. The same reference admission anchors both age-at-admission (Priority 1) and the `observationWindow` (Priority 2/3). Per §7, "admission" here means a distinct `visit_number` group (its own admit/discharge time), so a patient can have multiple *visits* inside the range, not just multiple `encounter` rows within one visit. **For now, the dashboard only considers the patient's 1st (earliest) visit in range** — later visits within the same `admissionDateRange` are not separately evaluated or returned. This is a known simplification, not a long-term design constraint; revisit if multi-visit cohorts become a requirement.
5. **MRN normalisation:** `patient.mrn` is the single canonical MRN representation used throughout this document. MRNs are compared **as exact strings**; the dashboard server trims surrounding whitespace before serialisation and performs no other normalisation.
6. **Location matching:** Location filters resolve through an explicit server-side lookup table (`LOCATION_LOOKUP`, see `query_patient_encounters`) mapped against `unit.name`, not free-text partial matching, and not `log_hl7_adt.location`.
7. **Admission source of truth:** Admission and discharge times are read from the relational schema (`patient` → `encounter`), not from `log_hl7_adt`. A single hospital stay can produce multiple `encounter` rows sharing the same `visit_number` — e.g. one row per bed/unit the patient occupied across transfers. The stay's `admit_time` is therefore the **earliest `start_time`** among the `encounter` rows grouped by `visit_number`, and its `discharge_time` is the **latest `end_time`** in that same group (`NULL` if any row in the group is still open, i.e. the stay is ongoing).

---

## Custom DB Functions

These functions are not native AtriumDB SDK methods but are defined to extend the SDK's capabilities for tables/joins the SDK does not expose publicly. They are invoked as `query_xxx(sdk, ...)` — the SDK object is passed as the first argument and database access is obtained from its `sql_handler` internally, so no separate connection management is needed at the call site.

The relational chain used throughout this document is:

```
patient (id, mrn, gender, dob)
   └─< encounter (patient_id, visit_number, bed_id, start_time, end_time)
            └─ bed (id, unit_id, name)
                    └─ unit (id, institution_id, name, type)   ← "ICU" / "OR" lives in unit.name
```

This replaces the previous design's reliance on `log_hl7_adt` (the raw ADT message log). `log_hl7_adt` records every transfer/demographic-update event and requires heuristic deduplication by `visit_num`; `encounter` is the curated, one-row-per-stay admission record with a clean foreign key to `bed` → `unit`, so it is preferred wherever an authoritative admission/location record is needed. `encounter.start_time` is this document's notion of "admit time."

---

### `query_patient_encounters`

The core admission/location lookup. Joins `encounter` → `bed` → `unit` so location resolves to `unit.name` rather than free-text. Used by **both** 1A (admission-range check, no location filter) and 1B (location + admission-range check).

Location matching uses the same explicit `LOCATION_LOOKUP` pattern as before, but now resolved against `unit.name` instead of `log_hl7_adt.location`:

```python
# Server-side config. Populated from production data:
#   SELECT DISTINCT type FROM unit;
# Maps API location codes to the EXACT values stored in unit.name.
LOCATION_LOOKUP: dict[str, list[str]] = {
    "ICU": ["ICU"],          # must NOT accidentally include NICU/PICU/CICU
    "OR":  ["OR"],
}


def query_patient_encounters(
    sdk: AtriumSDK,
    patient_id_list: list[int] | None = None,   # None = no patient pre-filter
    admit_start_ns: int | None = None,          # encounter.start_time >= this
    admit_end_ns: int | None = None,             # encounter.start_time <= this
    locations: list[str] | None = None,         # API codes, e.g. ["ICU"]; resolved via LOCATION_LOOKUP
) -> list[dict]:
    """
    Query encounter, joined to bed and unit, for admission + location records.

    encounter.start_time is this document's "admit time" (see Assumptions §7).

    Returns a list of dicts, one per encounter row (not per visit — callers use
    group_encounters_by_visit() to collapse rows into per-visit admission records):
    {
        "encounter_id":      int,
        "patient_id":        int,
        "visit_number":      str | None,
        "bed_id":            int,
        "unit_id":           int,
        "unit_type":         str | None,
        "start_time_ns":     int,    # encounter.start_time
        "end_time_ns":       int | None,  # encounter.end_time; None = stay ongoing
    }
    """
    conditions: list[str] = []
    params: list = []

    if patient_id_list:
        placeholders = ", ".join("?" for _ in patient_id_list)
        conditions.append(f"e.patient_id IN ({placeholders})")
        params.extend(patient_id_list)

    if admit_start_ns is not None:
        conditions.append("e.start_time >= ?")
        params.append(admit_start_ns)
    if admit_end_ns is not None:
        conditions.append("e.start_time <= ?")
        params.append(admit_end_ns)

    if locations:
        resolved: list[str] = []
        for code in locations:
            if code not in LOCATION_LOOKUP:
                raise ValueError(f"Unknown location code: {code!r}")
            resolved.extend(LOCATION_LOOKUP[code])
        placeholders = ", ".join("?" for _ in resolved)
        conditions.append(f"u.type IN ({placeholders})")
        params.extend(resolved)

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    sql = f"""
        SELECT e.id, e.patient_id, e.visit_number, e.bed_id, u.id, u.type,
               e.start_time, e.end_time
        FROM   encounter e
        JOIN   bed  b ON e.bed_id  = b.id
        JOIN   unit u ON b.unit_id = u.id
        {where_clause}
        ORDER  BY e.start_time ASC
    """

    with sdk.sql_handler.connection() as conn:
        cursor = conn.cursor()
        cursor.execute(sql, tuple(params))
        rows = cursor.fetchall()

    return [
        {
            "encounter_id":  row[0],
            "patient_id":    row[1],
            "visit_number":  row[2],
            "bed_id":        row[3],
            "unit_id":       row[4],
            "unit_type":     row[5],
            "start_time_ns": row[6],
            "end_time_ns":   row[7],
        }
        for row in rows
    ]


def group_encounters_by_visit(encounter_rows: list[dict]) -> dict[tuple[int, str | None], dict]:
    """
    Collapse per-encounter rows into per-visit admission records, implementing
    Assumptions §7: admit_time = MIN(start_time), discharge_time = MAX(end_time),
    grouped by (patient_id, visit_number).

    Visits with visit_number=NULL cannot be grouped with other rows and are kept
    as individual records under their own (patient_id, None) key — if a patient
    has multiple NULL-visit_number rows, only the earliest start_time is retained
    (same earliest-wins rule). A warning should be logged when NULL visit_numbers
    are encountered, as they indicate incomplete data.

    Returns a dict keyed by (patient_id, visit_number):
    {
        (patient_id, visit_number): {
            "patient_id":        int,
            "visit_number":      str | None,
            "admit_time_ns":     int,    # MIN(start_time) across the visit's encounter rows
            "discharge_time_ns": int | None,  # MAX(end_time); None if any row is still open
            "unit_types":        set[str],    # all unit types seen across the visit
        }
    }
    """
    visits: dict[tuple[int, str | None], dict] = {}

    for row in encounter_rows:
        pid = row["patient_id"]
        vn  = row["visit_number"]
        key = (pid, vn)

        if key not in visits:
            visits[key] = {
                "patient_id":        pid,
                "visit_number":      vn,
                "admit_time_ns":     row["start_time_ns"],
                "discharge_time_ns": row["end_time_ns"],
                "unit_types":        {row["unit_type"]} if row["unit_type"] else set(),
            }
        else:
            v = visits[key]
            # admit_time = MIN(start_time)
            if row["start_time_ns"] < v["admit_time_ns"]:
                v["admit_time_ns"] = row["start_time_ns"]
            # discharge_time = MAX(end_time); None if any row is still open
            if v["discharge_time_ns"] is None or row["end_time_ns"] is None:
                v["discharge_time_ns"] = None
            elif row["end_time_ns"] > v["discharge_time_ns"]:
                v["discharge_time_ns"] = row["end_time_ns"]
            if row["unit_type"]:
                v["unit_types"].add(row["unit_type"])

    return visits
```

**Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `sdk` | `AtriumSDK` | Initialised SDK instance in direct-DB mode. Database access via `sdk.sql_handler.connection()`. |
| `patient_id_list` | `list[int] \| None` | Restrict to specific patients (e.g. an MRN-resolved list in 1A). `None` = no patient pre-filter. |
| `admit_start_ns` / `admit_end_ns` | `int \| None` | Bounds on `encounter.start_time` (inclusive), epoch nanoseconds — the `admissionDateRange`. |
| `locations` | `list[str] \| None` | API location codes (`"ICU"`, `"OR"`), resolved to exact `unit.name` values via `LOCATION_LOOKUP` and OR-ed. `None` = no location filter (used by 1A, which only cares that *an* admission exists, not where). |

**Return value:** one row per matching encounter row (not per visit). Call `group_encounters_by_visit()` on the result to collapse rows into per-visit admission records per Assumptions §7 — `admit_time_ns = MIN(start_time)`, `discharge_time_ns = MAX(end_time)` per `(patient_id, visit_number)` group. A patient with multiple admissions in range produces multiple visit groups; the caller picks the reference visit per Assumptions §4.

---

## Priority 1 — Cohort Resolution

### Requirement

Given a `CohortDefinitionRequest` containing one or more `Demographiccohort` or `Mrncohort` entries, return a `MrncohortResponse` with a resolved flat MRN list per cohort.

---

### API: `POST /cohorts`

The dashboard server calls this endpoint to resolve all cohort definitions into validated MRN lists. All cohorts in a single request are always the same type — either all MRN lists or all demographic filters. The top-level `type` field tells AtriumDB which route to take for the entire request.

POST is used here rather than GET because the request body is a deeply nested object (age bands, MRN lists, location and sex filters) that cannot be expressed as query parameters. Using POST for complex read queries is standard practice.

`admissionDateRange` is a top-level required field in `CohortDefinitionRequest`. It applies to both routes: in 1A it defines the window within which an MRN must have an admission to be considered valid; in 1B it both scopes the ADT candidate pool and provides the per-patient `admit_time_ns` anchor needed to compute age-at-admission correctly. The `AdmissionDateRange` type definition already exists in the API spec (used in `StatisticsRequest`) and is reused as-is here.

**Request** (`CohortDefinitionRequest`):
```
POST /cohorts
X-Request-ID: a3f9c1d2-4b8e-4f1a-9c3d-2e7b6f0a1d5e
Content-Type: application/json
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `type` | `"mrn"` \| `"demographic"` | Yes | Discriminator — routes the entire request to 1A or 1B. |
| `cohorts` | `Mrncohort[]` \| `Demographiccohort[]` | Yes | One or more cohort definitions. |
| `admissionDateRange` | `AdmissionDateRange` | Yes | Inclusive date window in epoch nanoseconds. In 1A: an MRN must have at least one encounter in this range to be valid. In 1B: candidates are drawn from `encounter` rows with `start_time` in this range, and each patient's earliest in-range `admit_time_ns` (per `group_encounters_by_visit`) anchors their age-at-admission calculation. |

Mrncohort example:
```json
{
  "type":    "mrn",
  "admissionDateRange": { "start": 1704067200000000000, "end": 1767225599999999999 },
  "cohorts": [
    { "id": "1", "mrnList": ["MRN001234", "MRN005678"] },
    { "id": "2", "mrnList": ["MRN009012", "MRN003456"] }
  ]
}
```

Demographiccohort example:
```json
{
  "type":    "demographic",
  "admissionDateRange": { "start": 1704067200000000000, "end": 1767225599999999999 },
  "cohorts": [
    { "id": "1", "age": [ ... ], "sex": ["M"], "location": ["ICU"], "valueRange": { ... } },
    { "id": "2", "age": [ ... ], "sex": ["F"], "location": ["ICU"], "valueRange": null }
  ]
}
```

**Response** (`MrncohortResponse`):
```json
{
  "requestId": "a3f9c1d2-4b8e-4f1a-9c3d-2e7b6f0a1d5e",
  "cohorts": [
    { "id": "1", "mrnList": ["MRN001", "MRN002"] },
    { "id": "2", "mrnList": ["MRN010", "MRN011"] }
  ]
}
```

Top-level `type` routing:
- `"mrn"` → **1A** (MRN existence + admission range check)
- `"demographic"` → **1B** (location + age + sex filter)

---

### 1A — `Mrncohort` Resolution (MRN validation)

**Input:** `mrnList: string[]` + `admissionDateRange` (from `CohortDefinitionRequest`)
**Output:** valid MRNs (exist in AtriumDB **and** have an admission in the date range)

Two checks are applied in sequence, each with its own log message on failure:

1. **Existence check** — does the MRN exist in AtriumDB at all?
2. **Admission range check** — for MRNs that exist, does the patient have an encounter record within the `admissionDateRange`?

Both failure types are logged and silently excluded from the resolved cohort. They are not returned to the client yet at this stage.

```python
# Step 0 — normalise inbound MRNs per Assumptions §5 (trim whitespace, compare as strings)
mrn_input = [m.strip() for m in cohort.mrn_list]

# Step 1 — existence check via SDK
mrn_to_pid = sdk.get_mrn_to_patient_id_map(mrn_list=mrn_input)

recognised   = [m for m in mrn_input if m in mrn_to_pid]
unrecognised = [m for m in mrn_input if m not in mrn_to_pid]

if unrecognised:
    logger.warning(
        "MRNs not found in AtriumDB — excluded from cohort",
        extra={"request_id": request_id, "cohort_id": cohort.id, "mrns": unrecognised},
    )

if not recognised:
    return []   # nothing left to check

# Step 2 — admission range check via the encounter table (no location filter — any unit counts)
patient_id_to_mrn = {pid: mrn for mrn, pid in mrn_to_pid.items() if mrn in recognised}

encounter_rows = query_patient_encounters(
    sdk,
    patient_id_list=list(patient_id_to_mrn.keys()),
    admit_start_ns=admission_date_range.start,
    admit_end_ns=admission_date_range.end,
)

# Collapse to per-visit records; any patient_id present in the result has
# at least one visit in range — that is sufficient for 1A's purposes.
visits = group_encounters_by_visit(encounter_rows)
patient_ids_with_admission = {pid for pid, vn in visits.keys()}
mrns_with_admission = {patient_id_to_mrn[pid] for pid in patient_ids_with_admission}
no_admission_in_range = [m for m in recognised if m not in mrns_with_admission]

if no_admission_in_range:
    logger.warning(
        "MRNs exist in AtriumDB but have no encounter record in the requested date range — excluded from cohort",
        extra={
            "request_id":     request_id,
            "cohort_id":      cohort.id,
            "mrns":           no_admission_in_range,
            "admit_start_ns": admission_date_range.start,
            "admit_end_ns":   admission_date_range.end,
        },
    )

resolved_mrns = list(mrns_with_admission)
```

| Step | Call | Input | Output |
|------|------|-------|--------|
| 1 | `get_mrn_to_patient_id_map(mrn_list)` — SDK | Normalised MRN list | `{mrn: patient_id}` for existing MRNs only; absent = not found |
| 2 | `query_patient_encounters(sdk, patient_id_list, admit_start_ns, admit_end_ns)` — Custom DB | Recognised patient IDs + date range, no `locations` | One row per encounter |
| 2b | `group_encounters_by_visit(encounter_rows)` | Per-encounter rows | Per-visit admission records; any patient_id present = has admission in range |

The two log messages distinguish the two failure modes, making it easy to diagnose whether an MRN was wrong (typo, wrong registry) or simply outside the date window of interest.

---

### 1B — `Demographiccohort` Resolution

> **API:** Same `POST /cohorts` endpoint as 1A. The AtriumDB server routes here when the top-level `request.type == "demographic"`. No separate call is needed from the dashboard — the top-level `type` field determines the internal path for all cohorts in the request.

**Input:** `age`, `sex`, `location` filters + `admissionDateRange` (from `CohortDefinitionRequest`)
**Output:** list of MRNs matching all criteria (AND across dimensions, OR within each)

**Example request:**
```json
{
  "type": "demographic",
  "admissionDateRange": { "start": 1704067200000000000, "end": 1767225599999999999 },
  "cohorts": [
    {
      "id": "1",
      "age": [
        { "startNs": 63072000000000000, "endNs": 126144000000000000 }
      ],
      "sex": ["M"],
      "location": ["ICU"],
      "valueRange": null
    },
    {
      "id": "2",
      "age": [
        { "startNs": 189216000000000000, "endNs": 378432000000000000 }
      ],
      "sex": ["F", "U"],
      "location": ["ICU"],
      "valueRange": null
    }
  ]
}
```

| Field | Cohort 1 value | Cohort 2 value | Notes |
|-------|----------------|----------------|-------|
| `admissionDateRange.start` | `1704067200000000000` | same | 2024-01-01 00:00:00 UTC in epoch ns |
| `admissionDateRange.end` | `1767225599999999999` | same | 2025-12-31 23:59:59.999… UTC in epoch ns |
| `age[0].startNs` | `63072000000000000` | `189216000000000000` | 2 years ; 6 years (month=0 in all cases, so formula reduces to year×365×86 400 000 000 000) |
| `age[0].endNs` | `126144000000000000` | `378432000000000000` | 4 years ; 12 years |
| `sex` | `["M"]` | `["F", "U"]` | male only ; female or unknown sex |
| `location` | `["ICU"]` | `["ICU"]` | resolved via `LOCATION_LOOKUP` server-side |
| `valueRange` | `null` | `null` | no vital-sign range filter applied |

Cohort 1 finds **male ICU patients aged 2–4 years at their earliest in-range admission**. Cohort 2 finds **female or unknown-sex ICU patients aged 6–12 years at their earliest in-range admission**. Both cohorts draw candidates only from admissions that fall within the shared `admissionDateRange` (2024–2025). Age bounds are pre-converted to nanoseconds by the dashboard server using the `(year × 365 + month × 30) × 86 400 000 000 000` convention. No dob range is pre-computed — age is assessed by computing each candidate patient's `dob` against their actual admission time once it's known (Step 3 below).

#### Step 1 — Location + admission date range → candidate encounters, via `encounter` ⋈ `bed` ⋈ `unit`

The `admissionDateRange` arrives at the AtriumDB server as a top-level field in `CohortDefinitionRequest`, together with the cohort definitions in the same `POST /cohorts` call. Passing it into `query_patient_encounters` here is both correct and necessary for two independent reasons.

First, it scopes the candidate pool correctly by location: a `Demographiccohort` with `location=["ICU"]` should mean *patients who were in the ICU during the period of interest*, not patients who were ever in the ICU at any point in history. Without this filter, the candidate pool would span the entire encounter history and produce incorrectly large cohorts.

Second — and more importantly — it is required for **age-at-admission filtering to be semantically correct**. The age criteria in a cohort definition (e.g. "2–3 years old") must be evaluated against the patient's age *at the time they were admitted within the date range*, not their age today. Consider: a researcher specifies `admissionDateRange = 2024–2025` and an age band of `2–3 years`. A patient born in 2021 was 3 years old at their 2024 ICU admission but is 4 years old today — they should be *included*. A patient born in 2020 was 4–5 years old at their 2024 admission — they should be *excluded*, even though they were 2–3 years old at some point. Without linking each patient's `dob` to their specific admission timestamp within the date range, this distinction is impossible to make. The `admit_time_ns` returned per row (i.e. `encounter.start_time`) is the anchor that makes the age check in Step 3 possible.

```python
encounter_rows = query_patient_encounters(
    sdk,
    locations=cohort.location,                        # e.g. ["ICU"], ["OR"], or None
    admit_start_ns=admission_date_range.start,        # from CohortDefinitionRequest
    admit_end_ns=admission_date_range.end,
)

# Collapse per-encounter rows into per-visit records per Assumptions §7.
visits = group_encounters_by_visit(encounter_rows)

# Build patient_id → reference visit (earliest admit_time_ns in range) per Assumptions §4.
reference_admission: dict[int, dict] = {}
for (pid, vn), visit in visits.items():
    if pid not in reference_admission or visit["admit_time_ns"] < reference_admission[pid]["admit_time_ns"]:
        reference_admission[pid] = visit
```

If `location` is `null`, omit the `locations` parameter — this returns all patients admitted in the date range regardless of unit.

| Call | Parameters | Returns |
|------|------------|---------|
| `query_patient_encounters(sdk, locations, admit_start_ns, admit_end_ns)` — Custom DB | Location codes + admission date range | One row per encounter incl. `unit_type`, `start_time_ns` |
| `group_encounters_by_visit(encounter_rows)` | Per-encounter rows | Per-visit records with `admit_time_ns = MIN(start_time)`, `discharge_time_ns = MAX(end_time)` |

#### Step 2 — Fetch demographics for the candidate patient set, via existing SDK/sql_handler call

`gender`/`dob` aren't known yet for the patients that survived Step 1. Rather than adding another custom function, this reuses the SDK's own `sql_handler.select_all_patients_in_list`, which already supports an `id` list:

```python
patient_rows = sdk.sql_handler.select_all_patients_in_list(
    patient_id_list=list(reference_admission.keys())   # patient_ids from Step 1
)
# columns: id, mrn, gender, dob, first_name, middle_name, last_name,
#          first_seen, last_updated, source_id, weight, height
demographics_by_patient_id = {row[0]: {"mrn": row[1], "gender": row[2], "dob_ns": row[3]} for row in patient_rows}
```

#### Step 3 — Apply age and sex filters (pure Python, no SDK calls)

Age is assessed **at the time of the patient's reference ICU admission** (the earliest in-range admission per Assumptions §4), not at the current date. Each candidate's `dob` is checked directly against their real `admit_time_ns` from Step 1 — `age_at_admission_ns = admit_time_ns - dob_ns`.

Age bounds arrive as **nanoseconds** pre-converted by the dashboard server using `(year × 365 + month × 30) × 86_400_000_000_000`. The sex filter value `"U"` matches rows where gender is NULL, empty, or `'U'`; `"M"` and `"F"` match their stored values directly.

```python
surviving_mrns = []

for pid, encounter_row in reference_admission.items():
    demo = demographics_by_patient_id.get(pid, {})
    dob_ns = demo.get("dob_ns")
    mrn = demo.get("mrn")

    # --- Age filter: dob checked against this patient's real admit_time_ns from Step 1 ---
    age_ok = True
    if cohort.age and dob_ns is not None:
        age_at_admission_ns = encounter_row["admit_time_ns"] - dob_ns
        age_ok = any(
            band.startNs <= age_at_admission_ns <= band.endNs
            for band in cohort.age
        )
    elif cohort.age and dob_ns is None:
        age_ok = False   # dob unknown — cannot verify age, exclude patient

    # --- Sex filter: "U" matches NULL / empty / 'U' ---
    sex_ok = True
    if cohort.sex:
        gender_raw = demo.get("gender")
        requested = {s.upper() for s in cohort.sex}
        if gender_raw is None or gender_raw.strip() == "":
            sex_ok = "U" in requested
        else:
            g = gender_raw.strip().upper()
            sex_ok = g in requested or (g == "U" and "U" in requested)

    if age_ok and sex_ok:
        surviving_mrns.append(mrn)
```

#### Full chain summary

```
location codes + admissionDateRange
    → query_patient_encounters(sdk, locations, admit_start_ns, admit_end_ns)   [custom DB — encounter ⋈ bed ⋈ unit]
    → group_encounters_by_visit(encounter_rows)                                 [Python — MIN/MAX per (patient_id, visit_number)]
    → reference_admission: {patient_id → earliest in-range visit
                                          (admit_time_ns, discharge_time_ns, unit_types)}

candidate patient_ids
    → sdk.sql_handler.select_all_patients_in_list(patient_id_list)         [existing SDK call — patient table]
    → demographics_by_patient_id: {patient_id → (mrn, gender, dob_ns)}

    → age_at_admission_ns = admit_time_ns - dob_ns                         [Python — per patient, against their real admission]
    → age filter: band.startNs ≤ age_at_admission_ns ≤ band.endNs          [Python — ns values from dashboard]
    → sex filter: gender match incl. "U" = unknown                         [Python]

    → surviving MRN list
```

---

### Cohort Resolution Output

Regardless of which route was taken — explicit MRN list via **1A** or demographic filter via **1B** — the result is the same structure: a validated flat MRN list per cohort, where every MRN is confirmed to exist in AtriumDB and to have at least one admission record within the requested date range. This list is returned to the client as `MrncohortResponse` and subsequently passed back as the `cohorts` field of the `StatisticsRequest`, where it drives the signal data retrieval and statistics computation in Priority 2 and 3.

Priority 2/3 re-anchor each patient using the same reference admission rule (Assumptions §4): earliest in-range admission, observation window clipped at `discharge_time_ns` when the stay ends before `endHour`.

---

### 1C — Available Measures by Location

### API: `GET /measures`

The measures will be stored in the dashboard database on its launch. The dashboard server only calls this endpoint to validate measures stored in the dashboard database for the case of new measures being added in the AtriumDB. The endpoint allows optionally scoped to a clinical location.

**Request:** no body. `location` passed as an optional query parameter. `X-Request-ID` passed as a header (see Request ID Convention):

```
GET /measures?location=ICU
X-Request-ID: a3f9c1d2-4b8e-4f1a-9c3d-2e7b6f0a1d5e
```

| Query Parameter | Required | Values | Description |
|-----------------|----------|--------|-------------|
| `location` | No | `"ICU"`, `"OR"` | Filter measures to only those recorded on devices at that location. Omit to return all measures across all locations. |

**Response:**
```json
{
  "requestId": "a3f9c1d2-4b8e-4f1a-9c3d-2e7b6f0a1d5e",
  "measures": [
    { "tag": "MDC_ECG_HEART_RATE",        "name": "Heart Rate",            "freq_nhz": 500000000000, "units": "bpm"  },
    { "tag": "MDC_PRESS_BLD_ART_ABP_SYS", "name": "Systolic BP (Arterial)","freq_nhz": 125000000000, "units": "mmHg" }
  ]
}
```

**Input:** optional `location` query parameter
**Output:** list of measures available in AtriumDB for that location

> **Note:** This endpoint is low priority. If the implementation is too complex or costly on the AtriumDB side, it can be skipped — the dashboard will fall back to a statically configured measure list.

---
 
## Future Phases (informational — not for implementation)
 
Listed for forward visibility only; details intentionally unspecified and subject to change:
 
- **Priority 4 — Wave / time-series retrieval** — per the API contract, pending resolution of time-interval granularity and gap-handling questions.
- **OR location support** — `LOCATION_LOOKUP["OR"]` and the `"OR"` annotation are defined but unused by the V1 dashboard (ICU only).
---
 
## Supplementary Assumptions

The following assumptions have been made in this design where the production AtriumDB state was not fully known. Each is stated as a working assumption with a fallback if it does not hold.

1. **`unit.name` provides sufficient location information** for filtering patients by clinical unit, and its stored values cleanly distinguish `"ICU"` from related-but-distinct units (NICU, PICU, CICU). If `unit.name` is inconsistent or too coarse in production, `unit.name` should be checked as a fallback or combined with `type` in `LOCATION_LOOKUP`'s resolution.

2. **Every `encounter` row has a non-NULL `bed_id`** that resolves through `bed.unit_id` to a `unit` row. The schema permits `bed_id` to be NULL; if production data has encounters without a bed (e.g. pre-admission placeholder rows), `query_patient_encounters`'s inner `JOIN bed`/`JOIN unit` will silently exclude them — confirm whether that's the desired behaviour or whether a `LEFT JOIN` + explicit NULL-handling is needed.

3. **`patient.mrn` is the canonical, correctly formatted MRN** for all comparisons in this document — there is no separate ADT-log MRN representation to reconcile against, since `log_hl7_adt` is no longer part of this design's data path.

4. **Direct-DB mode is confirmed** — the AtriumDB team operates an internal server with direct database access (see Assumptions §1). This is not an open question.
