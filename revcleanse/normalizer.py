"""Canonicalization primitives: domains, company names, emails.

These are pure functions with no I/O so they stay trivially testable and
cheap to call once per row.
"""

from __future__ import annotations

import re

# Subdomains we treat as noise rather than as a distinct business unit.
# This is an explicit allow-list on purpose: a generic "keep the last two
# labels" rule would mangle `linear.app`, whose TLD *is* `.app`.
_NOISE_SUBDOMAINS = frozenset(
    {"www", "www2", "app", "apps", "blog", "portal", "go", "get", "info", "m", "shop", "support", "careers"}
)

# Legal entity suffixes stripped from the tail of a company name.
_LEGAL_SUFFIXES = frozenset(
    [
        "inc", "incorporated", "llc", "llp", "corp", "corporation", "ltd", "limited", "co", "company", "technologies", "technology",
        "group", "holdings", "gmbh", "ag", "sa", "sas", "bv", "nv", "plc", "ltda", "srl", "spa", "pty", "oy", "ab", "aps"
    ]
)

# Multi-label registry suffixes. A domain must keep one label *above* these,
# otherwise unrelated firms collapse onto a shared registry suffix.
_PUBLIC_SUFFIXES = frozenset(
    [
        "co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk", "com.au", "net.au", "org.au", "co.nz", "co.jp", "co.kr", "co.za", "com.br",
        "com.mx", "co.in", "com.sg", "com.hk", "com.cn", "com.tr", "com.ar", "com.pl", "co.il"
    ]
)

_DOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")
# `\w` is Unicode-aware, so accented and non-Latin names survive intact.
_PUNCT_RE = re.compile(r"[^\w&\s]+|_+")
_WS_RE = re.compile(r"\s+")


def canonicalize_domain(url: str | None) -> str | None:
    """Reduce any URL-ish string to its lowercase root domain.

    Strips scheme, credentials, `www.`/`app.`-style subdomains, port numbers,
    paths, query strings (`utm_*`, `ref`, `gclid`, ...) and fragments.

    >>> canonicalize_domain("https://www.app.linear.app:443/pricing?utm_source=ad")
    'linear.app'
    """
    if not url or not url.strip():
        return None

    host = url.strip().lower()
    host = re.sub(r"^[a-z][a-z0-9+.-]*://", "", host)  # scheme
    if host.startswith("//"):
        host = host[2:]                                 # protocol-relative //host/path
    host = host.split("@")[-1]                          # user:pass@ credentials
    # Everything from the first path/query/fragment delimiter onward is noise.
    host = re.split(r"[/?#]", host, maxsplit=1)[0]
    host = host.split(":")[0]                           # :443, :8080
    host = host.strip().strip(".")
    if not host:
        return None

    if not host.isascii():  # internationalized domain -> punycode
        try:
            host = host.encode("idna").decode("ascii")
        except (UnicodeError, UnicodeDecodeError):
            return None

    if not _DOMAIN_RE.match(host):
        return None

    labels = host.split(".")
    # Peel noise subdomains, but never past the registrable label: stripping
    # "app" off "app.co.uk" would leave the bare registry suffix "co.uk".
    while len(labels) > 2 and labels[0] in _NOISE_SUBDOMAINS and ".".join(labels[1:]) not in _PUBLIC_SUFFIXES:
        labels = labels[1:]
    if labels[0] in _NOISE_SUBDOMAINS and (len(labels) == 2 or ".".join(labels[1:]) in _PUBLIC_SUFFIXES):
        return None  # "www.com" / "app.co.uk" carry no company name

    root = ".".join(labels)
    if root in _PUBLIC_SUFFIXES:
        return None
    tld = labels[-1]
    if len(tld) < 2 or not tld.isalpha():
        return None
    return root


def sanitize_company_name(name: str) -> str:
    """Strip legal entity suffixes and punctuation from a company name.

    >>> sanitize_company_name("Linear Orbit, Inc.")
    'Linear Orbit'
    """
    if not name or not name.strip():
        return ""

    cleaned = _WS_RE.sub(" ", _PUNCT_RE.sub(" ", name)).strip()
    if not cleaned:
        return ""

    tokens = cleaned.split(" ")
    # Suffixes stack ("Acme Technologies Inc"), so peel from the tail.
    while len(tokens) > 1 and tokens[-1].lower() in _LEGAL_SUFFIXES:
        tokens = tokens[:-1]
    return " ".join(tokens)


def normalize_email(email: str) -> str:
    """Lowercase, trim, and drop a `mailto:` prefix."""
    if not email:
        return ""
    value = email.strip().lower()
    if value.startswith("mailto:"):
        value = value[len("mailto:") :].strip()
    return value.strip("<>").strip()
