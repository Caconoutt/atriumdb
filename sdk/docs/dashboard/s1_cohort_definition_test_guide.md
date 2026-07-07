# S1 Cohort Definition — Test Guide

This guide covers how to run the dashboard cohort resolver tests against a real AtriumDB dataset.
The synthetic tests in `test_dashboard_api.py` use an in-memory SQLite database and run in normal CI.
These real-data tests require a mounted dataset and are always skipped when the dataset is not present.

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
logic end-to-end on real patient and waveform data.

---

## Files Involved

| File | Role |
|---|---|
| `tests/test_dashboard_real_data.py` | The test file. Contains three tests: discovery, MRN cohort (1A), and demographic cohort (1B). |
| `tests/z_test_local_setup.py` | Pre-processing script. Seeds scrambled MRNs and synthetic encounter/bed/unit rows. Run once before the tests; safe to re-run. |
| `docker-run-dataset.sh` | Helper script to launch the Docker container with the dataset mounted and `ATRIUMDB_DATASET_LOCATION` set. Edit `HOST_DATASET_PATH` at the top to point to your local dataset folder. |

---

## Prerequisites

1. Build the Docker image from the `sdk/` directory if you have not already:
   ```bash
   docker build -t atriumdb-sdk .
   ```

2. Open `docker-run-dataset.sh` and set `HOST_DATASET_PATH` to the absolute path of your dataset
   on your Mac. The folder must contain `meta/index.db`.

3. Confirm the dataset mount is **not** read-only (required for `z_test_local_setup.py`).
   The line in `docker-run-dataset.sh` should read:
   ```bash
   -v "$HOST_DATASET_PATH:$CONTAINER_DATASET" \
   ```
   If it says `:ro` at the end, remove that suffix.

---

## Step 1 — Inspect the Dataset

Run the discovery test first to see what is actually in the database:

```bash
./docker-run-dataset.sh python -m pytest tests/test_dashboard_real_data.py::test_inspect_real_dataset -v -s
```

This prints a patient table (id, mrn, gender, dob) and an encounter table (enc_id, patient_id,
visit_number, unit_name, start_time_ns) directly to stdout.

**If the encounter table is empty**, proceed to Step 2. If encounters are already present, skip to Step 3.

---

## Step 2 — Seed Synthetic Data

Enter the container and run the setup script:

```bash
./docker-run-dataset.sh bash
python tests/z_test_local_setup.py
```

What it does:
1. For every patient with no MRN → assigns `MRN` + first 8 uppercase hex chars of `SHA-256(patient_id)`
   (e.g. patient 10046 → `MRN824A6D3E`).
2. Inserts one `institution` row (id=1) and one `unit` row (`name="ICU", type="icu"`) if not present.
3. For every patient → inserts one `bed` row (`name="Bed-<patient_id>"`, `unit_id` → ICU) if not present.
4. For every patient in `device_patient` → deletes any existing encounter row and re-inserts one:
   - `start_time = MIN(start_time)` across all their device-patient records
   - `end_time = MAX(end_time)` across all their device-patient records
   - `bed_id` → their dedicated bed row, `visit_number = "1"`

The script is safe to re-run at any time — encounters are always re-derived from device_patient,
so every run produces identical results regardless of prior state.

The script prints the full `pid → mrn → encounter window` mapping so you can verify what was seeded.

---

## Step 3 — Run the Tests

Run all real-data tests:

```bash
./docker-run-dataset.sh python -m pytest tests/test_dashboard_real_data.py -v -s
```

Or run them individually:

```bash
# Discovery — prints patient + encounter summary
./docker-run-dataset.sh python -m pytest tests/test_dashboard_real_data.py::test_inspect_real_dataset -v -s

# 1A — MRN cohort
./docker-run-dataset.sh python -m pytest tests/test_dashboard_real_data.py::test_mrn_cohort_real_data -v -s

# 1B — demographic cohort (all filter combinations in one test)
./docker-run-dataset.sh python -m pytest tests/test_dashboard_real_data.py::test_demographic_cohort_real_data -v -s
```

---

## Configuring Demographic Cohorts

The demographic test is driven by two constants at the top of the Step 3 section in
`test_dashboard_real_data.py`:

**`DEMOGRAPHIC_COHORTS`** — list of `DemographicCohort` objects to run. Add or uncomment entries
as you explore the real data. Available filters: `sex`, `age` (list of `AgeBand` in nanoseconds),
`location` (API codes resolved via `LOCATION_LOOKUP` in `encounter_queries.py`).

**`EXPECTED_DEMOGRAPHIC_COHORTS`** — dict mapping cohort id → expected MRN set. Only cohorts with
a non-empty set are validated; leave entries empty while still exploring. Example:

```python
EXPECTED_DEMOGRAPHIC_COHORTS: dict[str, set[str]] = {
    "male_age_10_15": {"MRN824A6D3E", "MRND62AADF3", "MRN79BFCA31"},
}

DEMOGRAPHIC_COHORTS: list[DemographicCohort] = [
    DemographicCohort(id="no_filters"),
    DemographicCohort(id="male", sex=["M"]),
    DemographicCohort(id="female", sex=["F"]),
    DemographicCohort(
        id="male_age_10_15",
        sex=["M"],
        age=[AgeBand(start_ns=10 * ONE_YEAR_NS, end_ns=16 * ONE_YEAR_NS)],
    ),
]
```

Age bounds use `ONE_YEAR_NS = 365 * 24 * 3600 * 1_000_000_000`. "Age 10 to 15 inclusive" means
the patient has passed their 10th birthday but not yet their 16th, so use `end_ns=16 * ONE_YEAR_NS`.
Age is evaluated at each patient's `admission_ns` (earliest in-range encounter start), not the current date.

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
''', ('M', 10*ONE_YEAR_NS, 16*ONE_YEAR_NS)).fetchall()
for r in rows: print(r)
"
```

---

## What to Expect

### Passing run output (stdout with `-v -s`)

```
PASSED tests/test_dashboard_real_data.py::test_inspect_real_dataset
  PATIENTS (178 total — showing first 30)
  ...
  ENCOUNTERS (172 total — showing first 20)
  ...

PASSED tests/test_dashboard_real_data.py::test_mrn_cohort_real_data
  cohort_a (3 resolved):
    mrn=MRN3279EDCF  admission_ns=1602609105000000000
    mrn=MRN3E7DB077  admission_ns=1584420294000000000
    mrn=MRN824A6D3E  admission_ns=1608553652000000000

PASSED tests/test_dashboard_real_data.py::test_demographic_cohort_real_data
  ============================================================
  no_filters (172 patients):
    mrn=MRN...  admission_ns=...
  male (89 patients):
    ...
  female (83 patients):
    ...
  male_age_10_15 (3 patients):
    mrn=MRN79BFCA31  admission_ns=1597582892000000000
    mrn=MRN824A6D3E  admission_ns=1608553652000000000
    mrn=MRND62AADF3  admission_ns=1586531479000000000
  ============================================================
```

### Common skip / failure reasons

| Symptom | Cause | Fix |
|---|---|---|
| `collected 0 items` or `not found` error | Using an old test function name | Use `test_demographic_cohort_real_data` (the two demographic tests were merged into one) |
| All tests skipped | `ATRIUMDB_DATASET_LOCATION` not set | Use `docker-run-dataset.sh`; do not run pytest directly |
| `OperationalError: attempt to write a readonly database` | Dataset still mounted `:ro` | Remove `:ro` from `docker-run-dataset.sh` |
| Expected MRN set doesn't match | Test window doesn't cover encounter start_times | Check setup script output for `ADMIT_START_NS`/`ADMIT_END_NS` and update test constants |
