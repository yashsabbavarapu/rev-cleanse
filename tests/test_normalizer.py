"""Normalizer unit tests: URLs, tracking params, ports, subdomains, suffixes."""

from __future__ import annotations

import pytest

from revcleanse.normalizer import canonicalize_domain, normalize_email, sanitize_company_name


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # The spec's worked example: scheme + www + subdomain + port + path + utm.
        ("https://www.app.linear.app:443/pricing?utm_source=ad", "linear.app"),
        # Schemes.
        ("http://ramp.com", "ramp.com"),
        ("https://ramp.com", "ramp.com"),
        ("ramp.com", "ramp.com"),
        ("HTTPS://RAMP.COM", "ramp.com"),
        # Paths and fragments.
        ("linear.app/careers", "linear.app"),
        ("https://squareup.com/us/en", "squareup.com"),
        ("https://figma.com/files#recent", "figma.com"),
        ("https://www.ramp.com/", "ramp.com"),
        # Tracking queries.
        ("https://linear.app?utm=demo", "linear.app"),
        ("https://notion.so/product?utm_campaign=q1&utm_medium=cpc", "notion.so"),
        ("https://datadoghq.com/pricing?ref=blog", "datadoghq.com"),
        ("https://stripe.com/?gclid=abc123", "stripe.com"),
        # Ports.
        ("https://vercel.com:443", "vercel.com"),
        ("http://portal.vercel.com:8080/login", "vercel.com"),
        # Subdomains, including a multi-level stack.
        ("https://blog.ramp.com/post/1", "ramp.com"),
        ("https://www.app.notion.so", "notion.so"),
        ("https://support.figma.com/help", "figma.com"),
        # A short TLD that is also a common subdomain word must survive.
        ("https://www.linear.app", "linear.app"),
    ],
)
def test_canonicalize_domain(raw: str, expected: str) -> None:
    assert canonicalize_domain(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "   ", "not a url", "localhost", "ramp", "://broken", "www.com"])
def test_canonicalize_domain_rejects_invalid(raw: str | None) -> None:
    assert canonicalize_domain(raw) is None


def test_canonicalize_domain_is_idempotent() -> None:
    once = canonicalize_domain("https://www.app.linear.app:443/pricing?utm_source=ad")
    assert canonicalize_domain(once) == once


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Linear Orbit, Inc.", "Linear Orbit"),
        ("Linear Orbit Incorporated", "Linear Orbit"),
        ("LINEAR ORBIT LLC", "LINEAR ORBIT"),
        ("Ramp Business Corp", "Ramp Business"),
        ("Ramp Business Corporation", "Ramp Business"),
        ("Ramp Business Co.", "Ramp Business"),
        ("Datadog Ltd", "Datadog"),
        ("Acme Limited", "Acme"),
        ("Acme Company", "Acme"),
        ("Linear Orbit Technologies", "Linear Orbit"),
        ("Brex Group", "Brex"),
        # Stacked suffixes peel all the way down.
        ("Acme Technologies Group Inc", "Acme"),
        # Punctuation and whitespace.
        ("  Figma,   Incorporated  ", "Figma"),
        ("Hewlett-Packard", "Hewlett Packard"),
        ("AT&T", "AT&T"),
        # Not a legal suffix: left alone.
        ("Anthropic PBC", "Anthropic PBC"),
        ("Stripe Inc", "Stripe"),
        ("Square Ltd", "Square"),
        ("", ""),
    ],
)
def test_sanitize_company_name(raw: str, expected: str) -> None:
    assert sanitize_company_name(raw) == expected


def test_sanitize_company_name_never_empties_a_real_name() -> None:
    """A name made only of suffixes keeps its last token rather than vanishing."""
    assert sanitize_company_name("Group") == "Group"
    assert sanitize_company_name("Technologies Inc") == "Technologies"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  GRACE@Linear.app ", "grace@linear.app"),
        ("mailto:alan@linear.app", "alan@linear.app"),
        ("MAILTO:Alan@Linear.app", "alan@linear.app"),
        ("  mailto: spaced@x.com ", "spaced@x.com"),
        ("already@clean.com", "already@clean.com"),
        ("", ""),
    ],
)
def test_normalize_email(raw: str, expected: str) -> None:
    assert normalize_email(raw) == expected


# --- Regressions -----------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # A registry suffix is not a company: peeling "app"/"blog" off these
        # would strand two unrelated firms on a shared "co.uk".
        ("https://www.acme.co.uk", "acme.co.uk"),
        ("https://blog.acme.co.uk", "acme.co.uk"),
        ("https://www.acme.com.au/pricing", "acme.com.au"),
    ],
)
def test_public_suffix_is_never_treated_as_a_company_domain(raw: str, expected: str) -> None:
    assert canonicalize_domain(raw) == expected


@pytest.mark.parametrize(
    "raw", ["co.uk", "https://co.uk", "https://www.co.uk", "https://app.co.uk", "https://blog.co.uk"]
)
def test_domain_without_a_company_label_is_rejected(raw: str) -> None:
    assert canonicalize_domain(raw) is None


def test_internationalized_domain_becomes_punycode() -> None:
    assert canonicalize_domain("https://münchen-tech.de") == "xn--mnchen-tech-thb.de"


def test_protocol_relative_url() -> None:
    assert canonicalize_domain("//acme.com/pricing?utm_source=x") == "acme.com"


def test_port_is_stripped_without_a_scheme() -> None:
    assert canonicalize_domain("acme.com:8080/path") == "acme.com"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Stripping non-ASCII letters used to leave "Soci t G n rale", and
        # reduced a non-Latin name to its bare legal suffix.
        ("Société Générale SA", "Société Générale"),
        ("Grüner Käse GmbH", "Grüner Käse"),
        ("Nestlé Group", "Nestlé"),
        ("北京科技 Ltd", "北京科技"),
        ("Ação Digital Ltda", "Ação Digital"),
        ("Zürich Versicherung AG", "Zürich Versicherung"),
    ],
)
def test_non_ascii_company_names_survive(raw: str, expected: str) -> None:
    assert sanitize_company_name(raw) == expected


def test_distinct_non_latin_names_stay_distinct() -> None:
    """Both used to collapse to 'Ltd' and merge into one account."""
    assert sanitize_company_name("北京科技 Ltd") != sanitize_company_name("上海软件 Ltd")


@pytest.mark.parametrize("raw", ["<a@b.com>", " <A@B.COM> ", "mailto:<a@b.com>"])
def test_angle_bracketed_email(raw: str) -> None:
    assert normalize_email(raw) == "a@b.com"
