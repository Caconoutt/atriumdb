"""z_test_local_setup.py — pre-processing script to seed scrambled MRNs,
synthetic unit/bed records, and synthetic encounter rows into a real dataset
that has waveform data but no ADT/encounter records.

Run inside the Docker container (dataset must NOT be mounted :ro):

    python tests/z_test_local_setup.py

The script is safe to re-run at any time — encounters are always deleted and
re-derived from device_patient, so every run produces identical results.

What it does
------------
1. Assigns a deterministic scrambled MRN (MRN + first 8 hex chars of
   SHA-256(patient_id)) to every patient that currently has no MRN.
2. Ensures one institution row exists (id=1, name="Test Institution").
3. Ensures one ICU unit row exists (name="ICU", type="icu").
4. For each patient in device_patient, inserts one bed row (name="Bed-<pid>")
   belonging to the ICU unit if it doesn't already exist.
5. For each patient in device_patient, deletes any existing encounter row and
   re-inserts one derived from device_patient:
   start_time = MIN(device_patient.start_time), end_time = MAX(end_time),
   visit_number = "1".
6. Prints the resulting MRN list and admission time bounds.
"""

import hashlib
import sqlite3
import time
import os

DATASET_LOCATION = os.environ.get("ATRIUMDB_DATASET_LOCATION", "/data/atriumdb")
DB_PATH = f"{DATASET_LOCATION}/meta/index.db"

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row


def hash_mrn(pid: int) -> str:
    return "MRN" + hashlib.sha256(str(pid).encode()).hexdigest()[:8].upper()


# ---------------------------------------------------------------------------
# Step 1 — assign scrambled MRNs to patients that have none
# ---------------------------------------------------------------------------
patients = conn.execute("SELECT id, mrn FROM patient").fetchall()
print(f"\nTotal patients: {len(patients)}")

assigned_mrns: dict[int, str] = {}
for row in patients:
    pid, mrn = row["id"], row["mrn"]
    if mrn:
        assigned_mrns[pid] = str(mrn)
        continue
    fake_mrn = hash_mrn(pid)
    conn.execute("UPDATE patient SET mrn = ? WHERE id = ?", (fake_mrn, pid))
    assigned_mrns[pid] = fake_mrn

conn.commit()
print(f"MRNs assigned or already present: {len(assigned_mrns)}")

# ---------------------------------------------------------------------------
# Step 2 — ensure institution + ICU unit exist
# ---------------------------------------------------------------------------
institution = conn.execute("SELECT id FROM institution WHERE id = 1").fetchone()
if not institution:
    conn.execute("INSERT INTO institution (id, name) VALUES (1, 'Test Institution')")
    conn.commit()
    print("Institution row inserted.")
else:
    print("Institution row already exists.")

icu_unit = conn.execute("SELECT id FROM unit WHERE name = 'ICU'").fetchone()
if not icu_unit:
    conn.execute(
        "INSERT INTO unit (institution_id, name, type) VALUES (1, 'ICU', 'icu')"
    )
    conn.commit()
    icu_unit_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    print(f"ICU unit inserted with id={icu_unit_id}.")
else:
    icu_unit_id = icu_unit["id"]
    print(f"ICU unit already exists with id={icu_unit_id}.")

# ---------------------------------------------------------------------------
# Step 3 — ensure every patient has a bed row pointing to the ICU unit
# ---------------------------------------------------------------------------
existing_beds: dict[str, int] = {
    r[0]: r[1]
    for r in conn.execute("SELECT name, id FROM bed").fetchall()
}

for pid in assigned_mrns:
    bed_name = f"Bed-{pid}"
    if bed_name not in existing_beds:
        conn.execute(
            "INSERT INTO bed (unit_id, name) VALUES (?, ?)",
            (icu_unit_id, bed_name),
        )
        bed_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        existing_beds[bed_name] = bed_id

conn.commit()
print(f"Bed rows total: {conn.execute('SELECT COUNT(*) FROM bed').fetchone()[0]}")

# ---------------------------------------------------------------------------
# Step 4 — derive one encounter per patient from device_patient.
# Always delete then re-insert so every run produces the same rows.
# ---------------------------------------------------------------------------
dp_rows = conn.execute(
    "SELECT patient_id, MIN(start_time) AS min_start, MAX(end_time) AS max_end "
    "FROM device_patient "
    "WHERE patient_id IN (SELECT id FROM patient) "
    "GROUP BY patient_id"
).fetchall()
print(f"Patients found in device_patient (with matching patient row): {len(dp_rows)}")

now_ns = time.time_ns()
for row in dp_rows:
    pid = row["patient_id"]
    bed_id = existing_beds[f"Bed-{pid}"]
    conn.execute("DELETE FROM encounter WHERE patient_id = ?", (pid,))
    conn.execute(
        "INSERT INTO encounter (patient_id, bed_id, start_time, end_time, "
        "source_id, visit_number, last_updated) VALUES (?, ?, ?, ?, 1, '1', ?)",
        (pid, bed_id, row["min_start"], row["max_end"], now_ns),
    )

conn.commit()
print(f"Encounter rows inserted: {len(dp_rows)}")

# ---------------------------------------------------------------------------
# Step 5 — print values to copy into test_dashboard_real_data.py
# ---------------------------------------------------------------------------
enc_rows = conn.execute(
    "SELECT patient_id, start_time, end_time FROM encounter"
).fetchall()

start_times = [r["start_time"] for r in enc_rows if r["start_time"] is not None]
end_times   = [r["end_time"]   for r in enc_rows if r["end_time"]   is not None]

print("\n--- Full mapping (patient_id → MRN → encounter window) ---")
for r in enc_rows:
    pid = r["patient_id"]
    print(f"  pid={pid:<6}  mrn={assigned_mrns.get(pid, '?'):<16}  "
          f"start={r['start_time']}  end={r['end_time']}")

if start_times:
    print(f"\nADMIT_START_NS: int = {min(start_times)}")
if end_times:
    print(f"ADMIT_END_NS:   int = {max(end_times)}")
elif start_times:
    print(f"ADMIT_END_NS:   int = {max(start_times)}")

conn.close()
