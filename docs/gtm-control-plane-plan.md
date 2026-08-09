# GTM Control Plane — Implementation Plan

Status: **plan only, nothing built yet.**

## 1. Context

Fish Audio's GTM team runs outbound through **Instantly** (email) and **HeyReach** (LinkedIn).
Today, lists arrive as CSVs and Google Sheets and get pushed into those tools directly. That
means bad rows reach live sequences, the same person gets hit twice from two lists, and
prospects who are already in an open deal — or already owned by another rep — get cold-emailed.

This tool sits **in front of** Instantly and HeyReach. It is the place a list goes to get
cleaned, conflict-checked, and assigned. Nothing enters an outbound tool without passing
through it, and every push is recorded so the *next* list can be checked against it.

Explicitly **not** in scope: writing copy, sending anything itself, replacing the CRM,
replacing Clay enrichment. It is a gate and a ledger, not another outbound tool.

Intended outcome: a GTM rep drops a CSV, sees `842 rows · 771 ready · 48 warnings · 23 blocked`
with reasons, unchecks what they don't want, picks a campaign, and pushes — in under two minutes.

## 2. The flow

```
CSV / Sheet URL  →  column map  →  validate  →  conflict check  →  review  →  assign & push
                                       │              │                            │
                                   row issues     CRM + suppression +          Instantly
                                                  prior-push ledger             HeyReach
                                                        │                          │
                                                        └────── ledger ────────────┘
```

## 3. Architecture

Stay in this repo, same stack (FastAPI + Railway + nixpacks), but as a separate module so it
never tangles with the voice demo in `main.py`. It mounts under `/gtm` and can be extracted to
its own service later without a rewrite.

```
main.py                     # existing demo — one added line: app.include_router(gtm.router)
gtm/
  router.py                 # APIRouter, all /gtm routes
  db.py                     # SQLAlchemy Core (no ORM), Postgres in prod / SQLite locally
  schema.py                 # canonical contact model + pydantic request/response types
  ingest.py                 # CSV parse, Google Sheet fetch, column auto-mapping
  validate.py               # row-level rules → ok | warn | error
  conflicts.py              # conflict engine (ledger, suppression, CRM, in-flight)
  crm/base.py               # CrmAdapter interface
  crm/sheet.py              # v1: deals exported to a Sheet
  crm/attio.py              # v2
  destinations/instantly.py
  destinations/heyreach.py
  ui.py                     # one inline HTML string, same pattern as HTML_PAGE in main.py
tests/                      # pytest, fixture CSVs
```

**Persistence: Railway Postgres, not SQLite.** The ledger is the whole point — a container
restart must not lose "we already emailed this person." `DATABASE_URL` in prod, SQLite file
locally. SQLAlchemy Core (not the ORM) keeps it small and portable across both.

**No frontend build step.** Server-rendered HTML + vanilla JS, reusing the dark CSS variables
already in `main.py:238-260` so it looks like the rest of the tooling. Consistent with how this
repo already works; nothing to compile or deploy separately.

**Auth: required.** This holds prospect PII and brokers two API keys. HTTP Basic against
`GTM_ACCESS_TOKEN` (env) on every `/gtm` route. Not optional, even for v1.

## 4. Canonical schema

Everything normalizes to one shape before validation:

```
first_name, last_name, email, linkedin_url, company_name, company_domain,
title, seniority, phone, country, timezone, list_source, owner, custom_*
```

`custom_*` columns pass through to campaign personalization variables untouched.

Identity keys for dedupe: `lower(email)` and normalized LinkedIn slug
(`linkedin.com/in/<slug>`, query params and locale prefixes stripped). Company identity is
`company_domain`, lowercased, `www.` and protocol stripped.

## 5. Validation rules

Each row gets `ok` / `warn` / `error`. **Errors block the push; warnings are pushable** but
shown with a reason chip.

**Errors**
- No `email` *and* no `linkedin_url` — nothing to reach them with
- Malformed email (syntax), or unresolvable domain (MX lookup, cached per-domain)
- Known disposable / catch-all-junk domain
- LinkedIn URL that is a `/company/` page or a Sales Navigator link — HeyReach rejects these
- CSV injection: any cell starting with `=`, `+`, `-`, `@` (a real risk when the list came from a
  scraper and is going back out into an email template)
- A column mapped as a **personalization variable** that is empty — this is the single most
  common cause of "Hi ," going out, and it should be a hard block, not a warning

**Warnings**
- Role-based address (`info@`, `sales@`, `support@`)
- Free-mail address (`gmail.com`, etc.) used as a business contact
- `company_domain` derivable but absent, junk name values (`N/A`, `-`, all-caps, an email in a
  name field), mojibake / encoding damage
- Missing `title` or `company_name` where the campaign template references them

**Silent normalizations** (applied, logged, no flag): whitespace trim, name casing, domain and
LinkedIn URL canonicalization, phone to E.164 where country is known.

**Intra-file duplicates** collapse to one row with a note showing the source row numbers.

## 6. Conflict checks

Run after validation, against four sources. Each conflict type is `block` or `warn`, set in one
config dict so the team can loosen a rule without a code change.

| Conflict | Source | Default |
|---|---|---|
| `already_sequenced` — we pushed this person before | internal ledger | block, with which campaign + when |
| `suppressed` — unsubscribed, bounced, or do-not-contact | suppression table | block |
| `open_deal` — contact or their company has an open opportunity | CRM adapter | block |
| `existing_customer` | CRM adapter | block |
| `owner_conflict` — account belongs to another rep | CRM adapter | warn |
| `account_collision` — already have N contacts in flight at this domain | ledger | warn, N configurable (default 3) |
| `cooldown` — contacted within the last X days | ledger | warn, default 90 days |
| `in_destination` — live check: already in the Instantly workspace / HeyReach campaign | destination APIs | block |

`in_destination` runs twice: once at preview so the rep sees the real number, and again at push
time via Instantly's `skip_if_in_workspace` flag as a backstop.

**CRM source — needs a decision (see §10).** The interface is one method:
`lookup(emails, domains) -> {key: {status, owner, deal_stage, last_activity}}`. v1 ships a
`SheetCrmAdapter` reading a deals export from a Google Sheet, which works on day one with zero
integration. Attio slots in behind the same interface.

## 7. UI — four steps, one page

Super minimalist. No sidebar, no dashboard, no charts.

1. **Source** — drop a CSV or paste a Google Sheet URL.
2. **Map** — detected columns in a two-column table with auto-guessed dropdowns. Header-name
   matching gets this right most of the time; the rep just confirms.
3. **Review** — a summary bar, then a filterable table. Click a status to filter to it. Issue
   chips per row. Checkboxes to exclude. That's the whole screen.
   ```
   842 rows · 771 ready · 48 warnings · 23 blocked        [ready] [warn] [blocked] [all]
   ```
4. **Assign** — two dropdowns (Instantly campaign, HeyReach campaign — both fetched live), a
   dry-run preview showing counts and five sample payloads, then **Push**. Result line:
   `pushed 771 · skipped 12 (already in workspace) · failed 0`.

Plus `/gtm/history`: past imports, who ran them, what went where. Flat list, no pagination
until it needs it.

## 8. Push mechanics

- **Dry run always precedes a live push.** Never a single-click send.
- **Idempotency key** per `(import_id, contact_id, destination)` — a retry after a timeout
  cannot double-add.
- **Instantly**: bulk-add in chunks of 100 with `skip_if_in_workspace: true`; Bearer auth
  against `api.instantly.ai/api/v2`. Campaign list fetched for the dropdown.
- **HeyReach**: max 100 leads per request, 300 req/min — a simple token-bucket limiter.
  Campaign must be `IN_PROGRESS`; if it is paused, surface that as a clear blocking message
  rather than silently using the auto-resume flag.
- **Partial failure is per-row**, not per-batch. Every API response is stored; failures show in
  the run log with the reason and a retry button for just those rows.
- **Hard cap**: pushes over 500 contacts require a typed confirmation.
- Exact request shapes get verified against the live docs during implementation — both APIs have
  moved recently and the published summaries disagree with each other.

## 9. Phasing

| Phase | Scope | Rough size |
|---|---|---|
| **P0** | CSV → validate → review → push to Instantly. Ledger + internal dedupe + suppression list. Auth. | ~1 day |
| **P1** | HeyReach + LinkedIn-specific rules. Google Sheet URL ingest. | ~half day |
| **P2** | CRM conflict adapter (Sheet, then Attio). Account collision + cooldown rules. | ~1 day |
| **P3** | History/audit page. Instantly webhook → auto-suppress bounces and unsubscribes, so the suppression list maintains itself. | ~1 day |

P0 is genuinely useful alone: it stops bad rows and re-contacts, which is most of the pain.

## 10. Decisions needed before P2

1. **Where do "existing deals" live?** Attio, HubSpot, Salesforce, or a spreadsheet? The Attio
   connector is present in this workspace but **not authorized**, so I could not inspect the
   schema — it needs authorizing in claude.ai connector settings before I can map deal stages
   to block/warn. Until then, P0/P1 assume the Sheet adapter.
2. **Google Sheets access** — v1 fetches the CSV export URL, which requires the sheet be
   link-viewable. Private sheets need a service account (`gspread`). Fine to defer, but worth
   knowing which the team actually uses.
3. **Cooldown window and per-domain cap** — 90 days and 3 contacts are placeholders.
4. **Does Clay sit upstream of this?** If lists come out of Clay already enriched, the
   validation rules loosen considerably and we should ingest from Clay directly rather than
   via exported CSV.

## 11. Verification

- `pytest tests/` — fixture CSVs covering each validator and each conflict type, including a
  deliberately nasty file (injection cells, mojibake, Sales Navigator URLs, intra-file dupes).
- `MOCK_DESTINATIONS=1` runs the full flow against fake Instantly/HeyReach clients, so the UI
  and ledger can be demoed with no API keys and no risk of a live send.
- Against real keys: dry-run a 5-row file into a paused test campaign in each tool, confirm the
  preview counts match what actually lands, then re-push the same file and confirm the ledger
  reports 5 skipped and 0 added.
- End to end: two overlapping CSVs. The second must report every shared contact as
  `already_sequenced` and push none of them.

## 12. Environment variables

```
DATABASE_URL          # Railway Postgres; falls back to local SQLite
GTM_ACCESS_TOKEN      # HTTP Basic password for /gtm
INSTANTLY_API_KEY
HEYREACH_API_KEY
ATTIO_API_KEY         # P2
MOCK_DESTINATIONS     # 1 = no live sends
```
