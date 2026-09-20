# rev-cleanse

[![CI](https://github.com/yashsabbavarapu/rev-cleanse/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/yashsabbavarapu/rev-cleanse/actions/workflows/ci.yml?query=branch%3Amain)

A standalone CLI that turns a dirty B2B lead export into a clean, de-duplicated
account/contact graph in an embedded DuckDB database, with a full audit trail
and strict idempotency.

CRM pollution is rarely one problem. It is tracking URLs (`?utm_source=`),
subdomain drift (`app.` / `blog.` / `portal.`), legal-suffix variation
(`Inc` / `LLC` / `Technologies`), the same person submitting a form twice, and
two sales reps disagreeing about a headcount. `rev-cleanse` resolves each of
those deterministically, and writes down *why* it made every call.

## Install

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## Run

```bash
python -m revcleanse.cli ingest --input fixtures/dirty_leads.csv --db crm.duckdb --audit merge_audit_log.json
```

```
+----------------------------+---------------------------------------------+
| rev-cleanse ingest         |                                             |
+----------------------------+---------------------------------------------+
| Raw Records Ingested       | 18                                          |
| Canonical Accounts Created | 10                                          |
| Duplicates Merged          | 8                                           |
| Total Contacts Stored      | 17                                          |
| Audit Entries Logged       | 8                                           |
| Idempotency Status         | BASELINE WRITE (+10 accounts, +17 contacts) |
+----------------------------+---------------------------------------------+
```

Run it a second time against the same database and the status flips to
`IDEMPOTENT REPLAY (no rows added)` with every count unchanged. Row counts can
be re-checked at any time with `python -m revcleanse.cli stats --db crm.duckdb`.

```bash
python -m pytest
```

## The pipeline

```
dirty CSV ──▶ normalize ──▶ resolve (2 passes) ──▶ conflict engine ──▶ DuckDB upsert
                  │                │                      │                   │
            domain/name/email   domain, then         timestamp wins      content-hash PKs
             canonicalization   fuzzy name ≥85%       + audit entry       + merge_audit
```

**1. Normalize** (`normalizer.py`), pure functions, no I/O.

| Input | Output |
| --- | --- |
| `https://www.app.linear.app:443/pricing?utm_source=ad` | `linear.app` |
| `Linear Orbit, Inc.` | `Linear Orbit` |
| `mailto:Alan@Linear.app` | `alan@linear.app` |

Subdomain stripping uses an explicit allow-list (`www`, `app`, `blog`,
`portal`, …) rather than a generic "keep the last two labels" rule. Because
`linear.app`'s TLD *is* `.app`, and the generic rule silently destroys it.
Peeling also stops above a registry suffix, so `app.co.uk` and `blog.co.uk`
never collapse onto a shared `co.uk`. Internationalized domains are converted
to punycode, and name sanitization is Unicode-aware. `北京科技 Ltd` keeps its
name instead of being reduced to a bare `Ltd` that would match every other
`Ltd` in the file.

**2. Resolve** (`resolver.py`). Two passes, cheapest and most certain first.

- *Stage 1, deterministic:* group by canonical domain. Exact and safe: `Stripe`
  and `Square` are never confused because their domains differ.
- *Stage 2, fuzzy fallback:* rows with no usable website are matched against the
  Stage-1 groups on sanitized company name (`rapidfuzz` token-sort ratio,
  threshold ≥ 85, `difflib` fallback if `rapidfuzz` is absent). This is what
  pulls a website-less `LINEAR ORBIT LLC` row into the `linear.app` account.

A domain-bearing row never enters Stage 2, so fuzzy matching can only ever
*add* a row to a group the domain pass already trusted.

**3. Resolve conflicts.** Applied per field, not per record:

1. A non-empty value fills an empty one, regardless of age.
2. Two conflicting non-empty values: the more recent `timestamp` wins.
3. Every decision (including a *rejected* stale value) becomes a
   `MergeAuditEntry`.

In the fixture, Ramp arrives with 500 employees (January) and 850 (April): 850
wins. Linear arrives with 120 (Jan 10), 140 (Mar 15) and 130 (Jan 5): 140 wins,
and the audit records `kept 140 over stale 130`. Row order never decides the
outcome.

**4. Persist** (`storage.py`). `accounts`, `contacts`, `merge_audit` in DuckDB,
plus `merge_audit_log.json` on disk for anyone without a SQL client.

## Idempotency guarantees

Re-running the same input must be a no-op. Three properties get us there:

1. **Content-addressed primary keys.** `account_id` is a hash of the business
   key (`domain:linear.app`, or `name:<sanitized>` when no domain exists) and
   `contact_id` is a hash of the normalized email. Identity is derived from the
   data, never from a sequence or insertion order, so the same CSV always
   mints the same IDs.
2. **Upserts, not appends.** Accounts and contacts use `INSERT OR REPLACE`, so a
   corrected headcount *revises* the account instead of forking a new one.
3. **Append-only audit.** `merge_audit` rows are keyed on a hash of the decision
   itself and written `ON CONFLICT DO NOTHING`, so a replay preserves the
   original `created_at` rather than restamping history.

Everything is written in a single transaction that rolls back on failure.

`tests/test_idempotency.py` asserts this end to end: it ingests the fixture
repeatedly into one database and compares full table contents (not just row
counts, which can hide churn) and separately checks that two databases built
independently from the same CSV come out identical.

Contacts are deduplicated on normalized email **within an account**, so the same
person arriving twice from two sources (`sofia@notion.so` and `SOFIA@NOTION.SO`)
is one contact. Identity is scoped to the account rather than global because a
shared or reused address genuinely appears at two companies; scoping it means a
row is never dropped, at the cost of two contact rows for one human.

## Boundary tradeoffs

Embedded DuckDB is the right call here, and it has a ceiling.

DuckDB gives a single-file, zero-infrastructure, $0 store with real SQL and
columnar scans. Ideal for an ingest that runs on a laptop or in CI. The costs:

| | Embedded DuckDB (this tool) | Warehouse (Snowflake/BigQuery) |
| --- | --- | --- |
| Concurrency | One writer, single node | Many concurrent writers |
| Scale | Comfortable to ~10⁷ rows on one box | Effectively unbounded |
| Ops | A file you can `rm` | Cluster, roles, cost controls |
| Latency | Milliseconds, in-process | Seconds, network round trip |

Where the boundaries sit:

- **Single-writer.** DuckDB takes an exclusive lock on the database file. This
  is a batch tool, not a service; two concurrent `ingest` runs will not work.
  A warehouse (or Postgres) is the move the moment writes are concurrent.
- Resolution is O(n·m) in Stage 2. Every domainless row is compared against
  known company names. Fine for thousands of rows, not for millions. That
  needs blocking keys (e.g. first token, or a phonetic key) to bound the
  candidate set before scoring.
- Fuzzy matching is a heuristic with irreducible false positives. At the
  ≥85 threshold, `Northwest Bank` and `Northeast Bank` score 92.9 and merge, one character apart, opposite meanings. Raising the bar would split genuine
  pairs like `First National Bank` / `First National Bancorp` (87.8). This is
  why fuzzy matching is confined to rows with no domain and why every merge is
  audited: the blast radius is capped and the decision is reversible. Real
  deployments should route low-confidence merges to human review.
- Last-write-wins is a policy, not a truth. A more recent timestamp is a
  proxy for "more correct". Source trust (an enrichment vendor over a webform)
  is often the better signal; the audit log is what makes that policy auditable
  and, if needed, reversible.
- **No incremental state.** Each run re-resolves the whole input. That is what
  keeps it deterministic and replayable, and it means cost grows with total
  input, not with what changed.

## Layout

```
revcleanse/
  models.py      Pydantic v2 schemas (RawLead, CanonicalAccount, CanonicalContact, MergeAuditEntry)
  normalizer.py  Domain canonicalization, legal-suffix stripping, email normalization
  resolver.py    Two-pass entity resolution + timestamp conflict engine
  storage.py     DuckDB schema, idempotent upserts, audit log
  cli.py         `ingest` / `stats` commands and the summary table
fixtures/
  dirty_leads.csv  18 messy rows -> 10 accounts, 17 contacts
tests/           105 tests across normalizer, resolver and idempotency
```
