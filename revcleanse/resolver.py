"""Entity resolution: group raw leads into accounts and settle field conflicts."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .models import CanonicalAccount, CanonicalContact, MergeAuditEntry, RawLead
from .normalizer import canonicalize_domain, normalize_email, sanitize_company_name

FUZZY_THRESHOLD = 85.0
_EPOCH = datetime(1970, 1, 1)

try:  # rapidfuzz is fast and precise; difflib keeps the tool dependency-light.
    from rapidfuzz import fuzz as _fuzz

    def similarity(a: str, b: str) -> float:
        return float(_fuzz.token_sort_ratio(a.lower(), b.lower()))

except ImportError:  # pragma: no cover - exercised only without rapidfuzz
    from difflib import SequenceMatcher

    def similarity(a: str, b: str) -> float:
        return SequenceMatcher(None, a.lower(), b.lower()).ratio() * 100.0


def parse_timestamp(value: str) -> datetime:
    """Parse an ISO-8601 timestamp to naive UTC; unparseable values sort oldest."""
    raw = (value or "").strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return _EPOCH
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


def stable_id(prefix: str, value: str) -> str:
    """Short, stable content hash used for every primary key in the pipeline."""
    return hashlib.sha1(f"{prefix}:{value}".encode()).hexdigest()[:16]


def _same(a: object, b: object) -> bool:
    if isinstance(a, str) and isinstance(b, str):
        return a.strip().lower() == b.strip().lower()
    return a == b


@dataclass
class _Bucket:
    """Leads believed to describe one company, plus per-field provenance."""

    key: str
    account_id: str
    domain: str | None
    names: list[str] = field(default_factory=list)
    leads: list[tuple[RawLead, datetime, str, str]] = field(default_factory=list)


@dataclass
class ResolutionResult:
    accounts: list[CanonicalAccount]
    contacts: list[CanonicalContact]
    audit: list[MergeAuditEntry]

    @property
    def duplicates_merged(self) -> int:
        """Raw rows that folded into an account already seeded by another row."""
        return sum(max(len(a.source_row_ids) - 1, 0) for a in self.accounts)


def resolve(leads: list[RawLead]) -> ResolutionResult:
    """Two-pass resolution: exact domain first, fuzzy company name second."""
    buckets: dict[str, _Bucket] = {}
    deferred: list[tuple[RawLead, datetime, str]] = []  # (lead, ts, sanitized name)

    # Stage 1 - deterministic grouping on canonical domain.
    for lead in leads:
        stamped = (lead, parse_timestamp(lead.timestamp), sanitize_company_name(lead.company_name))
        domain = canonicalize_domain(lead.website)
        if domain is None:
            deferred.append(stamped)
            continue
        bucket = buckets.get(domain)
        if bucket is None:
            bucket = buckets[domain] = _Bucket(domain, stable_id("domain", domain), domain)
        bucket.leads.append((*stamped, "domain_match"))
        if stamped[2]:
            bucket.names.append(stamped[2])

    # Stage 2 - fuzzy fallback on sanitized company name for domainless rows.
    for stamped in deferred:
        sname = stamped[2]
        target = _best_match(sname, buckets) if sname else None
        reason = "fuzzy_name_match"
        if target is None:
            key = f"name::{sname.lower()}" if sname else f"row::{stamped[0].row_id}"
            target = buckets.get(key)
            if target is None:
                target = buckets[key] = _Bucket(key, stable_id("name", key), None)
                reason = "new_account"
        target.leads.append((*stamped, reason))
        if sname:
            target.names.append(sname)

    return _build(buckets)


def _best_match(sname: str, buckets: dict[str, _Bucket]) -> _Bucket | None:
    """Highest-scoring bucket at or above the similarity threshold, else None."""
    best: _Bucket | None = None
    best_score = 0.0
    for bucket in buckets.values():
        for candidate in bucket.names:
            score = similarity(sname, candidate)
            if score > best_score:
                best, best_score = bucket, score
    return best if best_score >= FUZZY_THRESHOLD else None


def _build(buckets: dict[str, _Bucket]) -> ResolutionResult:
    accounts: list[CanonicalAccount] = []
    contacts: list[CanonicalContact] = []
    audit: list[MergeAuditEntry] = []

    for bucket in sorted(buckets.values(), key=lambda b: b.account_id):
        fields: dict[str, object] = {"canonical_domain": bucket.domain, "normalized_name": None, "employee_count": None}
        provenance: dict[str, datetime] = {}
        row_ids: list[str] = []
        contact_ids: list[str] = []
        seen_emails: set[str] = set()
        latest = _EPOCH

        for index, (lead, ts, sname, reason) in enumerate(bucket.leads):
            overrides: dict[str, str] = {}
            for name, incoming in (("normalized_name", sname), ("employee_count", lead.employee_count)):
                note = _apply(fields, provenance, name, incoming, ts)
                if note:
                    overrides[name] = note

            row_ids.append(lead.row_id)
            latest = max(latest, ts)

            email = normalize_email(lead.email)
            if email and email not in seen_emails:
                contact_id = stable_id("contact", f"{bucket.account_id}|{email}")
                seen_emails.add(email)
                contact_ids.append(contact_id)
                contacts.append(
                    CanonicalContact(
                        contact_id=contact_id,
                        account_id=bucket.account_id,
                        first_name=lead.first_name,
                        last_name=lead.last_name,
                        email=email,
                        source_row_id=lead.row_id,
                    )
                )

            if index == 0:
                continue  # the seeding row is not itself a merge
            audit.append(
                MergeAuditEntry(
                    surviving_account_id=bucket.account_id,
                    source_row_id=lead.row_id,
                    reason=reason,
                    field_overrides=overrides,
                )
            )

        accounts.append(
            CanonicalAccount(
                account_id=bucket.account_id,
                canonical_domain=fields["canonical_domain"],  # type: ignore[arg-type]
                normalized_name=fields["normalized_name"] or "",  # type: ignore[arg-type]
                employee_count=fields["employee_count"],  # type: ignore[arg-type]
                contact_ids=contact_ids,
                source_row_ids=row_ids,
                last_updated_at=latest.isoformat(),
            )
        )

    return ResolutionResult(accounts=accounts, contacts=contacts, audit=audit)


def _apply(
    fields: dict[str, object],
    provenance: dict[str, datetime],
    name: str,
    incoming: object,
    ts: datetime,
) -> str | None:
    """Merge one field. Returns an audit note when the incoming value mattered.

    Precedence: non-empty beats empty; between two conflicting non-empty values
    the one carrying the more recent timestamp wins.
    """
    if incoming is None or incoming == "":
        return None

    current = fields.get(name)
    if current is None or current == "":
        fields[name] = incoming
        provenance[name] = ts
        return f"filled empty -> {incoming}"

    if _same(current, incoming):
        provenance[name] = max(provenance.get(name, ts), ts)
        return None

    if ts > provenance.get(name, _EPOCH):
        fields[name] = incoming
        provenance[name] = ts
        return f"{current} -> {incoming} (newer timestamp wins)"
    return f"kept {current} over stale {incoming}"
