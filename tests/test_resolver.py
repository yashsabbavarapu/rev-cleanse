"""Resolver tests: domain grouping, fuzzy fallback, timestamp conflicts."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from revcleanse.models import RawLead
from revcleanse.resolver import FUZZY_THRESHOLD, parse_timestamp, resolve, similarity

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "dirty_leads.csv"


def lead(row_id: str, company: str, website: str | None, ts: str, employees: int | None = None, email: str | None = None) -> RawLead:
    return RawLead(
        row_id=row_id,
        first_name="Test",
        last_name=row_id,
        email=email or f"{row_id}@example.com",
        company_name=company,
        website=website,
        employee_count=employees,
        source="test",
        timestamp=ts,
    )


@pytest.fixture
def fixture_leads() -> list[RawLead]:
    with FIXTURE.open(newline="", encoding="utf-8") as handle:
        return [RawLead(**row) for row in csv.DictReader(handle)]


def account_by_name(result, name: str):
    matches = [a for a in result.accounts if a.normalized_name.lower() == name.lower()]
    assert len(matches) == 1, f"expected exactly one {name!r} account, got {len(matches)}"
    return matches[0]


# --- Stage 1: deterministic domain matching -------------------------------


def test_differently_dressed_urls_collapse_to_one_account() -> None:
    leads = [
        lead("a", "Linear Orbit, Inc.", "https://www.app.linear.app:443/pricing?utm_source=ad", "2026-01-10T09:00:00Z"),
        lead("b", "Linear Orbit Inc", "https://linear.app?utm=demo", "2026-02-02T11:30:00Z"),
        lead("c", "Linear Orbit Technologies", "linear.app/careers", "2026-03-15T08:15:00Z"),
    ]
    result = resolve(leads)
    assert len(result.accounts) == 1
    assert result.accounts[0].canonical_domain == "linear.app"
    assert result.accounts[0].source_row_ids == ["a", "b", "c"]
    assert result.duplicates_merged == 2


def test_distinct_companies_are_not_merged() -> None:
    """Stripe and Square are different businesses and must stay apart."""
    leads = [
        lead("a", "Stripe Inc", "https://stripe.com", "2026-03-01T09:00:00Z"),
        lead("b", "Square Ltd", "https://squareup.com/us/en", "2026-03-02T09:00:00Z"),
    ]
    result = resolve(leads)
    assert len(result.accounts) == 2
    assert {a.canonical_domain for a in result.accounts} == {"stripe.com", "squareup.com"}
    assert result.duplicates_merged == 0


def test_domain_beats_a_confusingly_similar_name() -> None:
    """Two firms sharing a name but not a domain stay separate."""
    leads = [
        lead("a", "Apex Systems", "https://apex-systems.com", "2026-01-01T00:00:00Z"),
        lead("b", "Apex Systems", "https://apexsystems.io", "2026-01-02T00:00:00Z"),
    ]
    assert len(resolve(leads).accounts) == 2


# --- Stage 2: fuzzy name fallback -----------------------------------------


def test_missing_website_joins_by_company_name() -> None:
    leads = [
        lead("a", "Linear Orbit, Inc.", "https://linear.app", "2026-01-10T09:00:00Z"),
        lead("b", "LINEAR ORBIT LLC", None, "2026-01-05T10:00:00Z"),
    ]
    result = resolve(leads)
    assert len(result.accounts) == 1
    account = result.accounts[0]
    assert account.canonical_domain == "linear.app"  # inherited from the domain-bearing row
    assert set(account.source_row_ids) == {"a", "b"}
    assert [e.reason for e in result.audit] == ["fuzzy_name_match"]


def test_near_miss_names_merge_above_threshold() -> None:
    leads = [
        lead("a", "Ramp Business Corp", "https://ramp.com", "2026-01-20T12:00:00Z"),
        lead("b", "Ramp Buisness", None, "2026-02-20T12:00:00Z"),  # typo in the CRM
    ]
    assert similarity("Ramp Business", "Ramp Buisness") >= FUZZY_THRESHOLD
    assert len(resolve(leads).accounts) == 1


def test_unrelated_domainless_names_stay_separate() -> None:
    leads = [
        lead("a", "Stripe", None, "2026-01-01T00:00:00Z"),
        lead("b", "Square", None, "2026-01-02T00:00:00Z"),
    ]
    assert similarity("Stripe", "Square") < FUZZY_THRESHOLD
    result = resolve(leads)
    assert len(result.accounts) == 2
    assert result.duplicates_merged == 0


# --- Conflict resolution ---------------------------------------------------


def test_more_recent_timestamp_wins_a_scalar_conflict() -> None:
    leads = [
        lead("a", "Ramp", "https://ramp.com", "2026-01-20T12:00:00Z", employees=500),
        lead("b", "Ramp", "https://ramp.com", "2026-04-01T09:45:00Z", employees=850),
    ]
    result = resolve(leads)
    assert result.accounts[0].employee_count == 850
    assert "500 -> 850" in result.audit[0].field_overrides["employee_count"]


def test_stale_row_cannot_overwrite_a_fresher_value() -> None:
    """Row order must not decide the winner - the timestamp does."""
    leads = [
        lead("a", "Ramp", "https://ramp.com", "2026-04-01T09:45:00Z", employees=850),
        lead("b", "Ramp", "https://ramp.com", "2026-01-20T12:00:00Z", employees=500),
    ]
    result = resolve(leads)
    assert result.accounts[0].employee_count == 850
    assert "kept 850" in result.audit[0].field_overrides["employee_count"]


def test_non_empty_value_fills_a_null() -> None:
    leads = [
        lead("a", "Vercel", "https://vercel.com", "2026-03-20T11:00:00Z", employees=None),
        lead("b", "Vercel", "https://vercel.com", "2026-01-30T10:10:00Z", employees=1200),
    ]
    result = resolve(leads)
    # 1200 fills the gap even though its row is older: empty never beats a value.
    assert result.accounts[0].employee_count == 1200


def test_matching_values_produce_no_audit_noise() -> None:
    leads = [
        lead("a", "Figma Inc.", "https://figma.com/files", "2026-03-11T15:30:00Z", employees=1100),
        lead("b", "Figma, Incorporated", "https://www.figma.com", "2026-03-12T09:20:00Z", employees=1100),
    ]
    result = resolve(leads)
    assert len(result.audit) == 1
    assert result.audit[0].field_overrides == {}


def test_last_updated_at_is_the_newest_row() -> None:
    leads = [
        lead("a", "Ramp", "https://ramp.com", "2026-01-20T12:00:00Z"),
        lead("b", "Ramp", "https://ramp.com", "2026-04-01T09:45:00Z"),
    ]
    assert resolve(leads).accounts[0].last_updated_at.startswith("2026-04-01")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-01-10T09:00:00Z", "2026-01-10T09:00:00"),
        ("2026-01-10T09:00:00+00:00", "2026-01-10T09:00:00"),
        ("2026-01-10T14:00:00+05:00", "2026-01-10T09:00:00"),  # normalized to UTC
        ("2026-01-10", "2026-01-10T00:00:00"),
        ("garbage", "1970-01-01T00:00:00"),  # unparseable sorts oldest, never wins
    ],
)
def test_parse_timestamp(raw: str, expected: str) -> None:
    assert parse_timestamp(raw).isoformat() == expected


# --- Contacts and identifiers ---------------------------------------------


def test_same_person_across_rows_yields_one_contact() -> None:
    leads = [
        lead("a", "Notion Labs", "https://notion.so", "2026-02-18T14:00:00Z", email="sofia@notion.so"),
        lead("b", "Notion Labs", "notion.so", "2026-02-19T08:00:00Z", email="SOFIA@NOTION.SO"),
    ]
    result = resolve(leads)
    assert len(result.contacts) == 1
    assert result.contacts[0].email == "sofia@notion.so"


def test_ids_are_stable_across_runs(fixture_leads: list[RawLead]) -> None:
    """Deterministic hashes are what make the DuckDB upsert idempotent."""
    first, second = resolve(fixture_leads), resolve(fixture_leads)
    assert [a.account_id for a in first.accounts] == [a.account_id for a in second.accounts]
    assert [c.contact_id for c in first.contacts] == [c.contact_id for c in second.contacts]


def test_ids_do_not_depend_on_row_order(fixture_leads: list[RawLead]) -> None:
    shuffled = list(reversed(fixture_leads))
    assert {a.account_id for a in resolve(fixture_leads).accounts} == {a.account_id for a in resolve(shuffled).accounts}


# --- End-to-end expectations on the shipped fixture ------------------------


def test_fixture_resolves_as_documented(fixture_leads: list[RawLead]) -> None:
    result = resolve(fixture_leads)
    assert len(fixture_leads) == 18
    assert len(result.accounts) == 10
    assert len(result.contacts) == 17  # r010/r011 are the same person
    assert result.duplicates_merged == 8

    linear = account_by_name(result, "Linear Orbit")
    assert linear.canonical_domain == "linear.app"
    assert sorted(linear.source_row_ids) == ["r001", "r002", "r003", "r004"]
    assert linear.employee_count == 140  # the 2026-03-15 row, not the 2026-01-05 one

    ramp = account_by_name(result, "Ramp Business")
    assert sorted(ramp.source_row_ids) == ["r005", "r006", "r007"]
    assert ramp.employee_count == 850  # 850 (April) beats 500 (January)

    assert account_by_name(result, "Stripe").canonical_domain == "stripe.com"
    assert account_by_name(result, "Square").canonical_domain == "squareup.com"

    brex = account_by_name(result, "Brex")  # no website anywhere, no name match
    assert brex.canonical_domain is None


# --- Regressions -----------------------------------------------------------


def test_one_person_at_two_companies_keeps_both_contacts() -> None:
    """Global email identity used to drop the contact from one of the accounts."""
    leads = [
        lead("a", "Alpha", "https://alpha.com", "2026-01-01T00:00:00Z", email="x@shared.com"),
        lead("b", "Beta", "https://beta.com", "2026-01-02T00:00:00Z", email="x@shared.com"),
    ]
    result = resolve(leads)
    assert len(result.accounts) == 2
    assert len(result.contacts) == 2
    assert all(len(a.contact_ids) == 1 for a in result.accounts)
    # ... and each contact is attached to the account it actually came from.
    assert {c.account_id for c in result.contacts} == {a.account_id for a in result.accounts}


def test_unrelated_non_latin_companies_do_not_merge() -> None:
    leads = [
        lead("a", "北京科技 Ltd", None, "2026-01-01T00:00:00Z"),
        lead("b", "上海软件 Ltd", None, "2026-01-02T00:00:00Z"),
    ]
    assert len(resolve(leads).accounts) == 2


def test_unrelated_companies_on_a_shared_registry_suffix_do_not_merge() -> None:
    leads = [
        lead("a", "Alpha", "https://app.co.uk", "2026-01-01T00:00:00Z"),
        lead("b", "Beta", "https://blog.co.uk", "2026-01-02T00:00:00Z"),
    ]
    assert len(resolve(leads).accounts) == 2
