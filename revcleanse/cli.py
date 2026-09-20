"""Command-line entry point: `python -m revcleanse.cli ingest ...`."""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .models import RawLead
from .resolver import ResolutionResult, resolve
from .storage import WriteReport, connect, counts, persist, write_audit_log

_REQUIRED = {"row_id", "first_name", "last_name", "email", "company_name", "source", "timestamp"}


def load_leads(path: str | Path) -> list[RawLead]:
    """Read the inbound CSV, reporting the offending line on a bad row."""
    try:
        # noqa justification: the handle is closed by the `with` below; opening
        # separately is what lets us report a readable error for a bad path.
        handle = Path(path).open(newline="", encoding="utf-8-sig")  # noqa: SIM115
    except OSError as exc:
        raise SystemExit(f"error: cannot read {path}: {exc.strerror}") from exc
    with handle:
        reader = csv.DictReader(handle)
        missing = _REQUIRED - set(reader.fieldnames or [])
        if missing:
            raise SystemExit(f"error: {path} is missing required column(s): {', '.join(sorted(missing))}")
        leads: list[RawLead] = []
        for line_no, row in enumerate(reader, start=2):
            known: dict[str, Any] = {k: v for k, v in row.items() if k in RawLead.model_fields}
            try:
                leads.append(RawLead(**known))
            except ValidationError as exc:
                raise SystemExit(f"error: {path} line {line_no}: {exc.errors()[0]['msg']}") from exc

    # row_id is the audit trail's anchor: duplicates would silently collapse
    # two different merge decisions into one log entry.
    counter = Counter(lead.row_id for lead in leads)
    duplicates = sorted(row_id for row_id, n in counter.items() if n > 1)
    if duplicates:
        raise SystemExit(f"error: {path} has duplicate row_id(s): {', '.join(duplicates[:5])}")
    return leads


def render_summary(result: ResolutionResult, report: WriteReport, raw_count: int, db: Path, audit: Path) -> str:
    """ASCII summary table of the run."""
    status = (
        "IDEMPOTENT REPLAY (no rows added)"
        if report.is_idempotent_replay
        else f"BASELINE WRITE (+{report.deltas['accounts']} accounts, +{report.deltas['contacts']} contacts)"
    )
    rows = [
        ("Raw Records Ingested", str(raw_count)),
        ("Canonical Accounts Created", str(len(result.accounts))),
        ("Duplicates Merged", str(result.duplicates_merged)),
        ("Total Contacts Stored", str(report.after["contacts"])),
        ("Audit Entries Logged", str(report.after["merge_audit"])),
        ("Idempotency Status", status),
    ]
    width_l = max(len(label) for label, _ in rows)
    width_r = max(len(value) for _, value in rows)
    bar = f"+-{'-' * width_l}-+-{'-' * width_r}-+"
    lines = [bar, f"| {'rev-cleanse ingest'.ljust(width_l)} | {''.ljust(width_r)} |", bar]
    lines += [f"| {label.ljust(width_l)} | {value.ljust(width_r)} |" for label, value in rows]
    lines += [bar, f"  database   : {db}", f"  audit log  : {audit}"]
    return "\n".join(lines)


def ingest(args: argparse.Namespace) -> int:
    leads = load_leads(args.input)
    result = resolve(leads)
    con = connect(args.db)
    try:
        report = persist(con, result.accounts, result.contacts, result.audit)
    finally:
        con.close()
    audit_path = write_audit_log(args.audit, result.audit)
    print(render_summary(result, report, len(leads), Path(args.db), audit_path))
    return 0


def stats(args: argparse.Namespace) -> int:
    con = connect(args.db)
    try:
        for table, count in counts(con).items():
            print(f"{table:<12} {count}")
    finally:
        con.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="revcleanse", description="Clean and de-duplicate a CRM lead export.")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("ingest", help="normalize, resolve and upsert a lead CSV")
    run.add_argument("--input", required=True, help="path to the dirty lead CSV")
    run.add_argument("--db", default="crm.duckdb", help="DuckDB file (default: crm.duckdb)")
    run.add_argument("--audit", default="merge_audit_log.json", help="audit log path")
    run.set_defaults(func=ingest)

    show = sub.add_parser("stats", help="print current row counts")
    show.add_argument("--db", default="crm.duckdb", help="DuckDB file (default: crm.duckdb)")
    show.set_defaults(func=stats)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
