"""Embedded DuckDB persistence with idempotent upserts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from .models import CanonicalAccount, CanonicalContact, MergeAuditEntry
from .resolver import stable_id

_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    account_id       VARCHAR PRIMARY KEY,
    canonical_domain VARCHAR,
    normalized_name  VARCHAR NOT NULL,
    employee_count   BIGINT,
    last_updated_at  VARCHAR NOT NULL
);
CREATE TABLE IF NOT EXISTS contacts (
    contact_id VARCHAR PRIMARY KEY,
    account_id VARCHAR NOT NULL,
    email      VARCHAR NOT NULL,
    first_name VARCHAR,
    last_name  VARCHAR
);
CREATE TABLE IF NOT EXISTS merge_audit (
    id                   VARCHAR PRIMARY KEY,
    surviving_account_id VARCHAR NOT NULL,
    source_row_id        VARCHAR NOT NULL,
    reason               VARCHAR NOT NULL,
    overrides_json       VARCHAR NOT NULL,
    created_at           VARCHAR NOT NULL
);
"""

_TABLES = ("accounts", "contacts", "merge_audit")


@dataclass
class WriteReport:
    """Row counts either side of a write, so the CLI can prove idempotency."""

    before: dict[str, int]
    after: dict[str, int]

    @property
    def deltas(self) -> dict[str, int]:
        return {t: self.after[t] - self.before[t] for t in self.after}

    @property
    def is_idempotent_replay(self) -> bool:
        """True when this run added nothing to a database that already had rows."""
        return any(self.before.values()) and not any(self.deltas.values())


def connect(db_path: str | Path) -> duckdb.DuckDBPyConnection:
    """Open (creating if needed) the embedded database and ensure the schema."""
    con = duckdb.connect(str(db_path))
    con.execute(_SCHEMA)
    return con


def counts(con: duckdb.DuckDBPyConnection) -> dict[str, int]:
    def one(table: str) -> int:
        row = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        return int(row[0]) if row else 0

    return {t: one(t) for t in _TABLES}


def persist(
    con: duckdb.DuckDBPyConnection,
    accounts: list[CanonicalAccount],
    contacts: list[CanonicalContact],
    audit: list[MergeAuditEntry],
) -> WriteReport:
    """Upsert a resolution result.

    Every primary key is a content hash of the business key, so replaying the
    same input rewrites the same rows instead of appending new ones.
    """
    before = counts(con)
    now = datetime.now(UTC).isoformat(timespec="seconds")

    con.execute("BEGIN TRANSACTION")
    try:
        _many(
            con,
            "INSERT OR REPLACE INTO accounts VALUES (?, ?, ?, ?, ?)",
            [
                (a.account_id, a.canonical_domain, a.normalized_name, a.employee_count, a.last_updated_at)
                for a in accounts
            ],
        )
        _many(
            con,
            "INSERT OR REPLACE INTO contacts VALUES (?, ?, ?, ?, ?)",
            [(c.contact_id, c.account_id, c.email, c.first_name, c.last_name) for c in contacts],
        )
        # Audit rows are append-only history: keep the original created_at on
        # replay rather than restamping an event we already recorded.
        _many(
            con,
            "INSERT INTO merge_audit VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (id) DO NOTHING",
            [(audit_id(e), e.surviving_account_id, e.source_row_id, e.reason, _overrides(e), now) for e in audit],
        )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise

    return WriteReport(before=before, after=counts(con))


def _many(con: duckdb.DuckDBPyConnection, sql: str, rows: list[tuple[object, ...]]) -> None:
    """executemany, tolerating the empty batch that DuckDB otherwise rejects."""
    if rows:
        con.executemany(sql, rows)


def audit_id(entry: MergeAuditEntry) -> str:
    """Deterministic key so the same merge decision is logged exactly once."""
    return stable_id("audit", f"{entry.surviving_account_id}|{entry.source_row_id}|{entry.reason}|{_overrides(entry)}")


def _overrides(entry: MergeAuditEntry) -> str:
    return json.dumps(entry.field_overrides, sort_keys=True)


def write_audit_log(path: str | Path, audit: list[MergeAuditEntry]) -> Path:
    """Write the merge audit to disk alongside the database."""
    target = Path(path)
    if target.parent != Path(""):
        target.parent.mkdir(parents=True, exist_ok=True)
    payload = [
        {
            "id": audit_id(e),
            "surviving_account_id": e.surviving_account_id,
            "source_row_id": e.source_row_id,
            "reason": e.reason,
            "field_overrides": e.field_overrides,
        }
        for e in sorted(audit, key=lambda e: (e.surviving_account_id, e.source_row_id))
    ]
    target.write_text(json.dumps(payload, indent=2) + "\n")
    return target
