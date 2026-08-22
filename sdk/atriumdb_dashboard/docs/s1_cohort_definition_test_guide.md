# S1 Cohort Definition — Test Guide

This guide covers how to run the dashboard cohort resolver tests against a real AtriumDB
dataset. The synthetic tests in `test_dashboard_api.py` build their own SQLite dataset under
`tests/test_datasets/` and run in a normal test run. The real-data tests require a mounted
dataset and are always skipped when the dataset is not present.

---

## Purpose

The real-data tests verify that the cohort resolver works correctly against an actual AtriumDB
dataset — real patient rows, real device-patient mappings, and real waveform timestamps — rather
than the minimal fixtures used in unit tests.

Because this dataset contains no ADT/admission records (encounter, bed, unit tables are empty),
`z_test_local_setup.py` injects scrambled data to simulate a realistic query environment:

- **Scrambled MRNs** — deterministic fake MRNs (`MRN` + first 8 uppercase hex chars of `SHA-256(patient_id)`,
  e.g. `MRN824A6D3E`) assigned to every patient that has no MRN, so the MRN cohort resolver has
  something to look up without exposing raw patient IDs.
- **Synthetic encounter rows** — one encounter per patient derived from their `device_patient`
  time range (`MIN(start_time)` → `MAX(end_time)`), giving the resolver a valid admission window
  to filter against.
- **Synthetic unit and bed rows** — one ICU unit and one bed per patient, so the location filter
  join chain (`encounter → bed → unit`) resolves correctly.

These injected rows are not clinically meaningful — they exist solely to exercise the resolver
logic end-to-end on real patient and waveform data. Note that because every patient gets exactly
one encounter in one unit, this dataset cannot exercise multi-admission or unit-transfer
grouping; those paths are covered by `test_dashboard_api.py`.

---

## Files Involved

| File | Role |
|---|---|
| `tests/atriumdb_dashboard/test_dashboard_real_data.py` | The test file. Contains three tests: discovery, MRN cohort (1A), and demographic cohort (1B). |
| `tests/atriumdb_dashboard/z_test_local_setup.py` | Pre-processing script. Seeds scrambled MRNs and synthetic encounter/bed/unit rows. Run once before the tests; safe to re-run. |
| `docker-run-dataset.sh` | Helper script to launch the Docker container with the dataset mounted and `ATRIUMDB_DATASET_LOCATION` set. Edit `HOST_DATASET_PATH` at the top to point to your local dataset folder. |

---

## Prerequisites

1. Build the Docker image from the `sdk/` directory if you have not already:
   ```bash
   docker build -t atriumdb-sdk .
   ```

2. Open `docker-run-dataset.sh` and set `HOST_DATASET_PATH` to the absolute path of your dataset
   on your Mac. The folder must contain `meta/index.db` and a `tsc/` subdirectory. The script
   refuses to run while the placeholder value is still in place.

3. The dataset must be mounted **writable** — `z_test_local_setup.py` writes to `meta/index.db`.
   The mount line in `docker-run-dataset.sh` reads:
   ```bash
   -v "$HOST_DATASET_PATH:$CONTAINER_DATASET" \
   ```
   If you add a `:ro` suffix, the setup script will fail with
   `attempt to write a readonly database`.

The script also bind-mounts your working copy over the image's source:

```bash
-v "$(pwd):/sdk" \
```

so the code under test is always the checkout you are editing, not the snapshot copied in at
build time. You do not need to rebuild the image after editing SDK or test code.

---

## Step 1 — Inspect the Dataset

Run the discovery test first to see what is actually in the database:

```bash
./docker-run-dataset.sh python -m pytest tests/atriumdb_dashboard/test_dashboard_real_data.py::test_inspect_real_dataset -v -s
```

This prints a patient table (id, mrn, gender, dob) and an encounter table (enc_id, patient_id,
visit_number, unit_name, start_time_ns) directly to stdout, followed by the min/max encounter
`start_time`.

**If the encounter table is empty**, proceed to Step 2. If encounters are already present, skip to Step 3.

---

## Step 2 — Seed Synthetic Data

Run the setup script — either directly:

```bash
./docker-run-dataset.sh python tests/atriumdb_dashboard/z_test_local_setup.py
```

or from an interactive shell in the container:

```bash
./docker-run-dataset.sh bash
python tests/atriumdb_dashboard/z_test_local_setup.py
```

What it does:
1. For every patient with no MRN → assigns `MRN` + first 8 uppercase hex chars of `SHA-256(patient_id)`
   (e.g. patient 10046 → `MRN824A6D3E`). Patients that already have an MRN keep it.
2. Inserts one `institution` row (id=1, name `"Test Institution"`) and one `unit` row
   (`name="ICU"`, `type="icu"`) if not already present.
3. For every patient → inserts one `bed` row (`name="Bed-<patient_id>"`, `unit_id` → ICU) if not present.
4. For every patient in `device_patient` → deletes any existing encounter row and re-inserts one:
   - `start_time = MIN(start_time)` across all their device-patient records
   - `end_time = MAX(end_time)` across all their device-patient records
   - `bed_id` → their dedicated bed row, `visit_number = "1"`

The script is safe to re-run at any time — encounters are always re-derived from device_patient,
so every run produces identical results regardless of prior state.

The script prints the full `pid → mrn → encounter window` mapping, plus `ADMIT_START_NS` /
`ADMIT_END_NS` covering all seeded encounters, so you can verify what was seeded and pick a
test window.

---

## Step 3 — Run the Tests

Run all real-data tests:

```bash
./docker-run-dataset.sh python -m pytest tests/atriumdb_dashboard/test_dashboard_real_data.py -v -s
```

Or run them individually:

```bash
# Discovery — prints patient + encounter summary
./docker-run-dataset.sh python -m pytest tests/atriumdb_dashboard/test_dashboard_real_data.py::test_inspect_real_dataset -v -s

# 1A — MRN cohort
./docker-run-dataset.sh python -m pytest tests/atriumdb_dashboard/test_dashboard_real_data.py::test_mrn_cohort_real_data -v -s

# 1B — demographic cohort (all filter combinations in one test)
./docker-run-dataset.sh python -m pytest tests/atriumdb_dashboard/test_dashboard_real_data.py::test_demographic_cohort_real_data -v -s
```

---

## Configuring the Tests

Both tests are driven entirely by module-level constants — no fixtures to edit.

### 1A — MRN cohort

| Constant | Role |
|---|---|
| `MRN_ADMIT_START_NS` / `MRN_ADMIT_END_NS` | The admission window (currently 2020-01-01 → 2021-01-01 UTC). |
| `MRN_COHORTS` | List of `(cohort_id, mrn_list)` pairs — one cohort per entry. |
| `EXPECTED_MRN_COHORTS` | `cohort_id → set of MRNs` expected to survive. MRNs in the input but absent here are expected to be excluded (unknown, or no in-window encounter). |
| `EXPECTED_MRN_ADMISSIONS` | `cohort_id → {mrn: [admission_ns, ...]}`. Checks admission timestamps only. |

Only cohorts with a non-empty entry are asserted, so you can leave a cohort out of the expected
dicts while still exploring. The test always asserts that resolved MRNs are a subset of the
input.

### 1B — demographic cohort

| Constant | Role |
|---|---|
| `DEMO_ADMIT_START_NS` / `DEMO_ADMIT_END_NS` | The admission window. |
| `DEMOGRAPHIC_COHORTS` | List of `DemographicCohort` objects to run. Available filters: `sex`, `age` (list of `AgeBand` in nanoseconds), `location` (matched against `unit.name` in the dataset). |
| `EXPECTED_DEMOGRAPHIC_COHORTS` | `cohort_id → set of MRNs`. |
| `EXPECTED_DEMOGRAPHIC_ADMISSIONS` | `cohort_id → {mrn: [Admission, ...]}`. Compares full `Admission` models, so `admission_ns`, `discharge_ns` **and** `location` must all match. |

Two assertions always run regardless of the expected dicts: the `no_filters` cohort must be
non-empty, and the `male` / `female` cohorts must not overlap. Both cohorts must therefore stay
in `DEMOGRAPHIC_COHORTS`.

Current definitions:

```python
ONE_YEAR_NS  = 365 * 24 * 3600 * 1_000_000_000
ONE_MONTH_NS =  30 * 24 * 3600 * 1_000_000_000

DEMOGRAPHIC_COHORTS: list[DemographicCohort] = [
    DemographicCohort(id="no_filters"),
    DemographicCohort(id="male",   sex=["M"]),
    DemographicCohort(id="female", sex=["F"]),
    DemographicCohort(
        id="male_age_10_0_15_0",
        sex=["M"],
        age=[AgeBand(start_ns=10 * ONE_YEAR_NS + 0 * ONE_MONTH_NS,
                     end_ns=15 * ONE_YEAR_NS + 0 * ONE_MONTH_NS)],
        location=["ICU"],
    ),
    ...
]

EXPECTED_DEMOGRAPHIC_COHORTS: dict[str, set[str]] = {
    "male_age_10_0_15_0": {"MRN824A6D3E", "MRND62AADF3", "MRN79BFCA31"},
    ...
}

EXPECTED_DEMOGRAPHIC_ADMISSIONS: dict[str, dict[str, list[Admission]]] = {
    "male_age_10_0_15_0": {
        "MRN824A6D3E": [Admission(admission_ns=1608553652000000000,
                                  discharge_ns=1610082209000000000,
                                  location="ICU")],
        ...
    },
}
```

The cohort ids encode their band as `<years>_<months>`, e.g. `male_age_10_0_15_0` is 10y0m to
15y0m and `female_age_10_3_15_10` is 10y3m to 15y10m.

### Age band semantics

The resolver checks `band.start_ns <= (admit_time_ns - dob_ns) <= band.end_ns` — **both bounds
inclusive**. So `end_ns = 15 * ONE_YEAR_NS` admits patients up to exactly 15.0 years and excludes
anyone older; to include everyone who has not yet reached their 16th birthday, use
`end_ns = 16 * ONE_YEAR_NS`. Pick whichever matches the cohort you mean and name the id
accordingly.

Age is evaluated **per admission**, at that admission's own `admission_ns`, not at the current
date and not only at the patient's earliest in-range admission. A patient with several in-range
admissions qualifies if any one of them falls in a band, and only the qualifying admissions
appear in their `admissions` list. In this dataset each patient has exactly one seeded
encounter, so the distinction does not show up here — but it does against real ADT data.

To verify a filter manually before adding it to the test, run this inside the container:

```bash
python -c "
import sqlite3
conn = sqlite3.connect('/data/atriumdb/meta/index.db')
ONE_YEAR_NS = 365 * 24 * 3600 * 1_000_000_000
rows = conn.execute('''
    SELECT p.id, p.mrn, p.gender, p.dob, e.start_time,
           (e.start_time - p.dob) AS age_ns
    FROM encounter e
    JOIN patient p ON e.patient_id = p.id
    WHERE e.start_time >= 1577836800000000000
      AND e.start_time <= 1609459200000000000
      AND p.gender = ?
      AND p.dob IS NOT NULL
      AND (e.start_time - p.dob) >= ?
      AND (e.start_time - p.dob) <= ?
''', ('M', 10*ONE_YEAR_NS, 15*ONE_YEAR_NS)).fetchall()
for r in rows: print(r)
"
```

More ready-made peek queries are kept, commented out, at the bottom of `docker-run-dataset.sh`.

---

## What to Expect

### Passing run output (stdout with `-v -s`)

```
PASSED tests/atriumdb_dashboard/test_dashboard_real_data.py::test_inspect_real_dataset
  PATIENTS (178 total — showing first 30)
  ...
  ENCOUNTERS (172 total — showing first 20)
  ...

PASSED tests/atriumdb_dashboard/test_dashboard_real_data.py::test_mrn_cohort_real_data
  cohort_a (3 resolved):
    mrn=MRN3279EDCF  admissions=[Admission(admission_ns=1602609105000000000, discharge_ns=..., location='ICU')]
    mrn=MRN3E7DB077  admissions=[...]
    mrn=MRN824A6D3E  admissions=[...]

PASSED tests/atriumdb_dashboard/test_dashboard_real_data.py::test_demographic_cohort_real_data
  ============================================================
  no_filters (172 patients):
    mrn=MRN...  admissions=[...]
  male (89 patients):
    ...
  female (83 patients):
    ...
  male_age_10_0_15_0 (3 patients):
    mrn=MRN79BFCA31  admissions=[Admission(admission_ns=1597582892000000000, discharge_ns=1597729818000000000, location='ICU')]
    mrn=MRN824A6D3E  admissions=[Admission(admission_ns=1608553652000000000, discharge_ns=1610082209000000000, location='ICU')]
    mrn=MRND62AADF3  admissions=[Admission(admission_ns=1586531479000000000, discharge_ns=1586610119000000000, location='ICU')]
  ============================================================
```

Each patient line prints the full `admissions` list, so a patient with several qualifying
admissions shows several `Admission(...)` entries, ordered by `admission_ns`. The demographic
test prints at most 10 patients per cohort.

### Updating expected values

After the cohort list prints, the demographic test emits a paste-ready block:

```
EXPECTED_DEMOGRAPHIC_ADMISSIONS = {
    "male_age_10_0_15_0": {
        "MRN824A6D3E": [Admission(admission_ns=..., discharge_ns=..., location='ICU')],
        ...
    },
}
```

Copy it over the constant in the test file rather than transcribing values by hand — then eyeball
the numbers against the dataset before trusting them. Only cohort ids already present as keys in
`EXPECTED_DEMOGRAPHIC_ADMISSIONS` appear in this block, so add an empty entry for a cohort first
if you want it printed.

### Common skip / failure reasons

| Symptom | Cause | Fix |
|---|---|---|
| `collected 0 items` or `not found` error | Using an old test function name | Use `test_demographic_cohort_real_data` (the two demographic tests were merged into one) |
| All tests skipped | `ATRIUMDB_DATASET_LOCATION` not set | Use `docker-run-dataset.sh`; do not run pytest directly |
| `OperationalError: attempt to write a readonly database` | Dataset mounted `:ro` | Remove `:ro` from the mount line in `docker-run-dataset.sh` |
| `KeyError` on a cohort id | Cohort id in an `EXPECTED_*` dict but not in `MRN_COHORTS` / `DEMOGRAPHIC_COHORTS` | Keep the two in sync — the expected dicts are indexed by the resolved results |
| Expected MRN set doesn't match | Test window doesn't cover encounter start_times | Check setup script output for `ADMIT_START_NS`/`ADMIT_END_NS` and update the window constants |
| `UnknownLocationError` / HTTP 422 `Unknown location code(s)` | No row in the `unit` table has that name | Check the spelling against `SELECT DISTINCT name FROM unit;`. Note SQLite matches case-sensitively |
