"""Idempotency: re-ingesting the same CSV must never duplicate stored rows."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from revcleanse import storage
from revcleanse.cli import main
from revcleanse.models import RawLead
from revcleanse.resolver import resolve

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "dirty_leads.csv"
TABLES = ("accounts", "contacts", "merge_audit")


def run_ingest(db: Path, audit: Path, source: Path = FIXTURE) -> int:
    return main(["ingest", "--input", str(source), "--db", str(db), "--audit", str(audit)])


def snapshot(db: Path) -> dict[str, list[tuple]]:
    """Every row of every table, ordered, so we can compare runs exactly."""
    con = storage.connect(db)
    try:
        return {t: con.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall() for t in TABLES}
    finally:
        con.close()


@pytest.fixture
def paths(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "crm.duckdb", tmp_path / "merge_audit_log.json"


def test_second_ingest_adds_no_rows(paths: tuple[Path, Path]) -> None:
    db, audit = paths
    assert run_ingest(db, audit) == 0
    first = {t: len(rows) for t, rows in snapshot(db).items()}
    assert first == {"accounts": 10, "contacts": 17, "merge_audit": 8}

    assert run_ingest(db, audit) == 0
    second = {t: len(rows) for t, rows in snapshot(db).items()}
    assert second == first


def test_repeated_ingests_leave_rows_byte_identical(paths: tuple[Path, Path]) -> None:
    """Counts alone can hide churn, so compare the full table contents."""
    db, audit = paths
    run_ingest(db, audit)
    first = snapshot(db)
    for _ in range(3):
        run_ingest(db, audit)
    assert snapshot(db) == first


def test_audit_created_at_is_preserved_on_replay(paths: tuple[Path, Path]) -> None:
    """Audit rows are history: a replay must not restamp an existing entry."""
    db, audit = paths
    run_ingest(db, audit)
    con = storage.connect(db)
    before = con.execute("SELECT id, created_at FROM merge_audit ORDER BY id").fetchall()
    con.close()

    run_ingest(db, audit)
    con = storage.connect(db)
    after = con.execute("SELECT id, created_at FROM merge_audit ORDER BY id").fetchall()
    con.close()
    assert before == after


def test_audit_log_json_is_stable(paths: tuple[Path, Path]) -> None:
    db, audit = paths
    run_ingest(db, audit)
    first = audit.read_text()
    run_ingest(db, audit)
    assert audit.read_text() == first

    entries = json.loads(first)
    assert len(entries) == 8
    assert {e["source_row_id"] for e in entries} == {"r002", "r003", "r004", "r006", "r007", "r011", "r013", "r016"}
    assert len({e["id"] for e in entries}) == 8  # ids are unique


def test_report_flags_the_replay(paths: tuple[Path, Path]) -> None:
    db, _audit = paths
    leads = [RawLead(**row) for row in csv.DictReader(FIXTURE.open(newline="", encoding="utf-8"))]
    result = resolve(leads)

    con = storage.connect(db)
    try:
        first = storage.persist(con, result.accounts, result.contacts, result.audit)
        assert not first.is_idempotent_replay
        assert first.deltas == {"accounts": 10, "contacts": 17, "merge_audit": 8}

        second = storage.persist(con, result.accounts, result.contacts, result.audit)
        assert second.is_idempotent_replay
        assert second.deltas == {"accounts": 0, "contacts": 0, "merge_audit": 0}
    finally:
        con.close()


def test_updated_source_row_updates_in_place(tmp_path: Path, paths: tuple[Path, Path]) -> None:
    """A corrected headcount should revise the account, not fork a new one."""
    db, audit = paths
    run_ingest(db, audit)

    rows = list(csv.DictReader(FIXTURE.open(newline="", encoding="utf-8")))
    for row in rows:  # a fresher row for the same company
        if row["row_id"] == "r006":
            row["employee_count"] = "900"
            row["timestamp"] = "2026-06-01T09:00:00Z"
    revised = tmp_path / "revised.csv"
    with revised.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    run_ingest(db, audit, source=revised)
    con = storage.connect(db)
    try:
        assert con.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 10
        headcount = con.execute(
            "SELECT employee_count FROM accounts WHERE canonical_domain = 'ramp.com'"
        ).fetchone()[0]
        assert headcount == 900
    finally:
        con.close()


def test_ingest_into_a_fresh_database_is_reproducible(tmp_path: Path, paths: tuple[Path, Path]) -> None:
    """Two independent databases built from the same CSV must be identical."""
    db_a, audit_a = paths
    db_b, audit_b = tmp_path / "other.duckdb", tmp_path / "other.json"
    run_ingest(db_a, audit_a)
    run_ingest(db_b, audit_b)

    strip_created_at = lambda rows: [r[:-1] for r in rows]  # noqa: E731 - wall-clock column
    a, b = snapshot(db_a), snapshot(db_b)
    assert a["accounts"] == b["accounts"]
    assert a["contacts"] == b["contacts"]
    assert strip_created_at(a["merge_audit"]) == strip_created_at(b["merge_audit"])
    assert audit_a.read_text() == audit_b.read_text()
