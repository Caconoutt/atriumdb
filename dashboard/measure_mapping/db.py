"""Reference dashboard-DB access layer for the measure-resolution tables.

The mapping lives in the **dashboard DB**, not in AtriumDB. The dashboard team owns the
real migration; this SQLite implementation mirrors the normative schema from the spec
(section 3) so the resolver is runnable and testable on its own. Swap it for the project's
DB by re-implementing the same small surface:

    transaction(), upsert_measure(), upsert_mapping_if_seed_owned(),
    refresh_atriumdb_id(), set_atriumdb_id_by_pk(), query_measures(),
    all_measure_keys(), insert_unseeded()

Column names are normative -- keep them.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass

from .models import SeedRow

SCHEMA = """
CREATE TABLE IF NOT EXISTS measure (
    id                   INTEGER PRIMARY KEY,           -- dashboard-local, stable
    atriumdb_measure_id  INTEGER UNIQUE,                -- resolved id; NULL until resolvable
    tag                  TEXT    NOT NULL,
    freq_nhz             INTEGER NOT NULL,
    unit_code            TEXT    NOT NULL,              -- MDC unit string
    unit_label           TEXT,
    display_name         TEXT,
    UNIQUE (tag, freq_nhz, unit_code)
);

CREATE TABLE IF NOT EXISTS measure_mapping (
    measure_id        INTEGER PRIMARY KEY REFERENCES measure(id) ON DELETE CASCADE,
    measure_group     TEXT    NOT NULL,
    vital_sign        TEXT,
    review            INTEGER NOT NULL DEFAULT 0,
    conversion_factor REAL    NOT NULL DEFAULT 1.0,
    canonical_unit    TEXT,
    source            TEXT    NOT NULL DEFAULT 'seed',  -- 'seed' | 'admin'
    updated_at        TEXT,
    updated_by        TEXT
);

CREATE INDEX IF NOT EXISTS ix_mapping_vital_sign ON measure_mapping(vital_sign);
CREATE INDEX IF NOT EXISTS ix_mapping_group      ON measure_mapping(measure_group);
"""


@dataclass
class MeasureRecord:
    """A joined measure + mapping row, as returned by query_measures()."""
    id: int
    atriumdb_measure_id: int | None
    tag: str
    freq_nhz: int
    unit_code: str
    unit_label: str | None
    display_name: str | None
    measure_group: str
    vital_sign: str | None
    review: bool
    conversion_factor: float
    canonical_unit: str | None
    source: str


class DashboardDB:
    def __init__(self, path: str = ":memory:"):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")

    def ensure_schema(self) -> None:
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # -- transaction ---------------------------------------------------------
    @contextmanager
    def transaction(self):
        try:
            yield self
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # -- writes --------------------------------------------------------------
    def upsert_measure(self, *, tag, freq_nhz, unit_code, unit_label, display_name,
                       atriumdb_measure_id) -> int:
        """Upsert on the natural key (tag, freq_nhz, unit_code); return the dashboard PK.

        The cached ``atriumdb_measure_id`` is always refreshed (it is a per-deployment
        cache, re-resolved from the triple). Identity columns are stable.
        """
        cur = self.conn.execute(
            """
            INSERT INTO measure (tag, freq_nhz, unit_code, unit_label, display_name,
                                 atriumdb_measure_id)
            VALUES (:tag, :freq_nhz, :unit_code, :unit_label, :display_name, :aid)
            ON CONFLICT(tag, freq_nhz, unit_code) DO UPDATE SET
                unit_label          = excluded.unit_label,
                display_name        = excluded.display_name,
                atriumdb_measure_id = excluded.atriumdb_measure_id
            """,
            {"tag": tag, "freq_nhz": freq_nhz, "unit_code": unit_code,
             "unit_label": unit_label, "display_name": display_name,
             "aid": atriumdb_measure_id},
        )
        if cur.lastrowid:
            row = self.conn.execute(
                "SELECT id FROM measure WHERE tag=? AND freq_nhz=? AND unit_code=?",
                (tag, freq_nhz, unit_code),
            ).fetchone()
            return row["id"]
        return cur.lastrowid

    def upsert_mapping_if_seed_owned(self, *, measure_id, measure_group, vital_sign,
                                     review, conversion_factor, canonical_unit,
                                     source="seed", updated_by="seed") -> None:
        """Write semantics on re-seed only when the existing row is seed-owned (or new).

        Admin edits win: a row whose ``source='admin'`` is left untouched.
        """
        existing = self.conn.execute(
            "SELECT source FROM measure_mapping WHERE measure_id=?", (measure_id,)
        ).fetchone()
        if existing is not None and existing["source"] == "admin":
            return
        self.conn.execute(
            """
            INSERT INTO measure_mapping (measure_id, measure_group, vital_sign, review,
                                         conversion_factor, canonical_unit, source,
                                         updated_at, updated_by)
            VALUES (:mid, :grp, :vs, :rev, :cf, :cu, :src, datetime('now'), :by)
            ON CONFLICT(measure_id) DO UPDATE SET
                measure_group     = excluded.measure_group,
                vital_sign        = excluded.vital_sign,
                review            = excluded.review,
                conversion_factor = excluded.conversion_factor,
                canonical_unit    = excluded.canonical_unit,
                source            = excluded.source,
                updated_at        = excluded.updated_at,
                updated_by        = excluded.updated_by
            """,
            {"mid": measure_id, "grp": measure_group, "vs": vital_sign,
             "rev": 1 if review else 0, "cf": conversion_factor, "cu": canonical_unit,
             "src": source, "by": updated_by},
        )

    def refresh_atriumdb_id(self, *, tag, freq_nhz, unit_code, atriumdb_measure_id) -> None:
        """Update only the cached id for an existing measure (used by resync)."""
        self.conn.execute(
            """UPDATE measure SET atriumdb_measure_id=?
               WHERE tag=? AND freq_nhz=? AND unit_code=?""",
            (atriumdb_measure_id, tag, freq_nhz, unit_code),
        )

    def insert_unseeded(self, *, tag, freq_nhz, unit_code, unit_label, display_name,
                        atriumdb_measure_id) -> int:
        """Auto-insert an instance measure missing from the seed as needs_review."""
        pk = self.upsert_measure(
            tag=tag, freq_nhz=freq_nhz, unit_code=unit_code, unit_label=unit_label,
            display_name=display_name, atriumdb_measure_id=atriumdb_measure_id,
        )
        self.upsert_mapping_if_seed_owned(
            measure_id=pk, measure_group="needs_review", vital_sign=None, review=True,
            conversion_factor=1.0, canonical_unit=None, source="seed", updated_by="coverage",
        )
        return pk

    # -- reads ---------------------------------------------------------------
    def query_measures(self, *, measure_id=None, vital_sign=None,
                       measure_group=None) -> list[MeasureRecord]:
        clauses, params = [], []
        if measure_id is not None:
            clauses.append("m.id = ?")
            params.append(measure_id)
        if vital_sign is not None:
            clauses.append("mm.vital_sign = ?")
            params.append(vital_sign)
        if measure_group is not None:
            clauses.append("mm.measure_group = ?")
            params.append(measure_group)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self.conn.execute(
            f"""
            SELECT m.id, m.atriumdb_measure_id, m.tag, m.freq_nhz, m.unit_code,
                   m.unit_label, m.display_name, mm.measure_group, mm.vital_sign,
                   mm.review, mm.conversion_factor, mm.canonical_unit, mm.source
            FROM measure m
            JOIN measure_mapping mm ON mm.measure_id = m.id
            {where}
            ORDER BY m.id
            """,
            params,
        ).fetchall()
        return [
            MeasureRecord(
                id=r["id"], atriumdb_measure_id=r["atriumdb_measure_id"], tag=r["tag"],
                freq_nhz=r["freq_nhz"], unit_code=r["unit_code"], unit_label=r["unit_label"],
                display_name=r["display_name"], measure_group=r["measure_group"],
                vital_sign=r["vital_sign"], review=bool(r["review"]),
                conversion_factor=r["conversion_factor"], canonical_unit=r["canonical_unit"],
                source=r["source"],
            )
            for r in rows
        ]

    def all_measure_keys(self) -> set[tuple]:
        rows = self.conn.execute(
            "SELECT tag, freq_nhz, unit_code FROM measure"
        ).fetchall()
        return {(r["tag"], r["freq_nhz"], r["unit_code"]) for r in rows}
