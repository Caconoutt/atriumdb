"""prepare_dataset.py - seed scrambled MRNs, synthetic unit/bed records, and
synthetic encounter rows into a real dataset that has waveform data but no
ADT/encounter records.

Run this on your own machine, against your local copy of the dataset, BEFORE
copying it to the server.

    Windows:  python atriumdb_dashboard\\deploy\\prepare_dataset.py C:\\path\\to\\dataset
    macOS:    python3 atriumdb_dashboard/deploy/prepare_dataset.py /path/to/dataset

It touches only meta/index.db through the stdlib `sqlite3` module and never
imports the SDK, so it runs anywhere Python does - no Docker, no virtualenv, no
install. The dataset that lands on the server is already prepared, which is why
the `atriumdb-api` container can keep its read-only (`:ro`) mount.

The script is safe to re-run at any time - encounters are always deleted and
re-derived from device_patient, so every run produces identical results. A
timestamped backup of index.db is taken before the first write unless
--no-backup is passed.

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
7. Prints a row count for every table in the database. With --dump, prints the
   rows themselves as well.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sqlite3
import sys
import time

# Windows console note: stdout falls back to the ANSI code page (cp1252) when it
# is redirected to a file or a pipe, and any non-ASCII byte then raises
# UnicodeEncodeError - which would kill the run *after* it had already written to
# the database. Force UTF-8 so `python prepare_dataset.py ... > out.txt` is safe.
# (reconfigure() exists from Python 3.7; the guard keeps 3.6 usable.)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Internal SQLite bookkeeping tables - never interesting in a dump.
INTERNAL_TABLE_PREFIX = "sqlite_"


def hash_mrn(pid: int) -> str:
    return "MRN" + hashlib.sha256(str(pid).encode()).hexdigest()[:8].upper()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Windows: quote paths containing spaces, but do not leave a "
               "trailing backslash inside the quotes - cmd.exe treats \\\" as an "
               "escaped quote. Use \"C:\\path\\to\\dataset\" not "
               "\"C:\\path\\to\\dataset\\\".",
    )
    parser.add_argument(
        "dataset",
        nargs="?",
        default=os.environ.get("ATRIUMDB_DATASET_LOCATION"),
        help="Path to the dataset directory (the one holding meta and tsc). "
             "Defaults to the ATRIUMDB_DATASET_LOCATION environment variable.",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Skip the timestamped copy of meta/index.db taken before writing.",
    )
    parser.add_argument(
        "--dump",
        action="store_true",
        help="Print the contents of every table, not just row counts. This "
             "prints patient data to the console - see --tables and --limit.",
    )
    parser.add_argument(
        "--tables",
        metavar="A,B,C",
        help="Comma-separated table names to dump. Default: every table.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        metavar="N",
        help="Rows per table when dumping (default 20). Use 0 for no limit - "
             "note that block_index and interval_index can hold millions.",
    )
    args = parser.parse_args()
    if not args.dataset:
        parser.error(
            "no dataset path given and ATRIUMDB_DATASET_LOCATION is unset"
        )
    # Normalises separators and, importantly on Windows, strips the trailing
    # slash that a shell-completed path usually carries.
    args.dataset = os.path.normpath(args.dataset)
    return args


def open_dataset(dataset: str, backup: bool) -> sqlite3.Connection:
    """Validate the dataset layout, back up index.db, and return a connection."""
    db_path = os.path.join(dataset, "meta", "index.db")
    if not os.path.isdir(dataset):
        sys.exit(f"ERROR: not a directory: {dataset}")
    if not os.path.isfile(db_path):
        sys.exit(f"ERROR: no meta{os.sep}index.db under {dataset} - is this a dataset?")
    if not os.path.isdir(os.path.join(dataset, "tsc")):
        # Not fatal for this script, but a dataset without tsc is not one the
        # server can serve, so it almost certainly means the wrong path.
        print(f"WARNING: no tsc directory under {dataset}.")

    print(f"Dataset : {dataset}")
    print(f"Database: {db_path}")

    if backup:
        backup_path = f"{db_path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
        try:
            shutil.copy2(db_path, backup_path)
        except PermissionError:
            # The usual Windows cause: the file is open in DB Browser for SQLite
            # or similar, which takes a mandatory lock the way POSIX does not.
            sys.exit(
                f"ERROR: cannot copy {db_path} - permission denied.\n"
                "       Close any program holding the database open (DB Browser\n"
                "       for SQLite, a Python shell, an editor preview) and retry."
            )
        print(f"Backup  : {backup_path}")
    else:
        print("Backup  : skipped (--no-backup)")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def format_cell(value: object, width: int = 60) -> str:
    """Render one column value for the dump, keeping lines readable."""
    if value is None:
        return "NULL"
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<blob {len(bytes(value))} bytes>"
    text = str(value).replace("\n", "\\n").replace("\r", "\\r")
    return text if len(text) <= width else text[: width - 3] + "..."


def list_tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    ).fetchall()
    return [r["name"] for r in rows if not r["name"].startswith(INTERNAL_TABLE_PREFIX)]


def print_table_inventory(conn: sqlite3.Connection, tables: list[str]) -> None:
    """Row count for every table. No row contents, so no patient data."""
    print("\n--- Table inventory ---")
    name_width = max((len(t) for t in tables), default= 0)
    for table in tables:
        # Table names come from sqlite_master, not user input, so the quoted
        # interpolation here cannot be attacker-controlled.
        count = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        print(f"  {table:<{name_width}}  {count:>12,} rows")


def dump_tables(conn: sqlite3.Connection, tables: list[str], limit: int) -> None:
    """Print the rows of each table as a fixed-width grid."""
    print("\n--- Table contents ---")
    if limit:
        print(f"(showing at most {limit} rows per table; --limit 0 for all)")

    for table in tables:
        total = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        query = f'SELECT * FROM "{table}"'
        if limit:
            query += f" LIMIT {int(limit)}"
        rows = conn.execute(query).fetchall()

        shown = f"{len(rows)} of {total:,}" if limit and total > len(rows) else f"{total:,}"
        print(f"\n[{table}]  {shown} rows")
        if not rows:
            continue

        columns = rows[0].keys()
        cells = [[format_cell(row[c]) for c in columns] for row in rows]
        widths = [
            max(len(col), *(len(row[i]) for row in cells))
            for i, col in enumerate(columns)
        ]

        print("  " + "  ".join(c.ljust(w) for c, w in zip(columns, widths)))
        print("  " + "  ".join("-" * w for w in widths))
        for row in cells:
            print("  " + "  ".join(v.ljust(w) for v, w in zip(row, widths)))


def prepare(conn: sqlite3.Connection) -> None:
    """The five wrangling steps. Every one is idempotent."""
    # source_id=1 ("AtriumDB") is written at dataset creation and is what the
    # encounter rows below reference. SQLite does not enforce foreign keys by
    # default, so without this check a missing row would insert silently and
    # leave dangling references rather than failing here.
    if not conn.execute("SELECT id FROM source WHERE id = 1").fetchone():
        sys.exit("ERROR: no source row with id=1 - cannot attribute encounters.")

    # -----------------------------------------------------------------------
    # Step 1 - assign scrambled MRNs to patients that have none
    # -----------------------------------------------------------------------
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

    # -----------------------------------------------------------------------
    # Step 2 - ensure institution + ICU unit exist
    # -----------------------------------------------------------------------
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

    # -----------------------------------------------------------------------
    # Step 3 - ensure every patient has a bed row pointing to the ICU unit
    # -----------------------------------------------------------------------
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

    # -----------------------------------------------------------------------
    # Step 4 - derive one encounter per patient from device_patient.
    # Always delete then re-insert so every run produces the same rows.
    # -----------------------------------------------------------------------
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

    # -----------------------------------------------------------------------
    # Step 5 - print values to copy into test_dashboard_real_data.py
    # -----------------------------------------------------------------------
    enc_rows = conn.execute(
        "SELECT patient_id, start_time, end_time FROM encounter"
    ).fetchall()

    start_times = [r["start_time"] for r in enc_rows if r["start_time"] is not None]
    end_times   = [r["end_time"]   for r in enc_rows if r["end_time"]   is not None]

    print("\n--- Full mapping (patient_id -> MRN -> encounter window) ---")
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


def main() -> None:
    args = parse_args()
    conn = open_dataset(args.dataset, backup=not args.no_backup)

    try:
        prepare(conn)

        tables = list_tables(conn)
        if args.tables:
            requested = [t.strip() for t in args.tables.split(",") if t.strip()]
            unknown = [t for t in requested if t not in tables]
            if unknown:
                sys.exit(
                    f"ERROR: no such table(s): {', '.join(unknown)}\n"
                    f"       Available: {', '.join(tables)}"
                )
            tables = requested

        print_table_inventory(conn, tables)
        if args.dump:
            dump_tables(conn, tables, args.limit)
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower():
            sys.exit(
                f"ERROR: database is locked ({exc}).\n"
                "       Close any program holding it open (DB Browser for SQLite,\n"
                "       a Python shell) and retry. The backup taken above is intact."
            )
        raise
    finally:
        conn.close()

    print("\nDone. The dataset is ready to copy to the server.")


if __name__ == "__main__":
    main()
