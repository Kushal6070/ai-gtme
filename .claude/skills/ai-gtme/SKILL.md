---
name: ai-gtme
description: "Use when asked to 'clean up outbound', 'run ai-gtme', 'prep outbound list', 'clean up this CSV / Apollo export / Clay export', 'write LinkedIn DMs or connection requests for outbound', 'build sequence campaigns', 'set up Day 1 emails', or 'find / enrich emails for outbound'. Cleans a raw export to the canonical outbound schema, dedupes against outbound.csv, fills the signal, personalizes LinkedIn + email copy, appends to outbound.csv, and enriches emails via SyncGTM MCP (default)."
metadata:
  version: 2.0.0
allowed-tools: Bash(python:*), Bash(python3:*), Read, Write, Edit
---

# ai-gtme — Outbound cleaner + LinkedIn/email personalizer

Turns one raw Apollo/Clay CSV export into a deduped, fully personalized outbound list ready for LinkedIn + email sequencing. All deterministic logic lives in the bundled engine **`clean_outbound.py`** — this doc is how to run it and the reference for every rule it applies.

One run does:

1. **Detect the signal** from the input filename (or `--signal`).
2. **Dedup** the input against `crm.txt` + `outbound.csv` (and itself).
3. **Clean columns** to the canonical schema — read **live** from `outbound.csv`'s header.
4. **Personalize** LinkedIn connection request, first DM, follow-up DM, account (round-robin), email sequence campaign, email Subject, and Day 1 email.
5. **Sort** by signal priority.
6. **Append + reorder** `outbound.csv` (actioned rows on top, empty-status rows sorted by priority below).

---

## How to run

```bash
# Auto-detect the signal from the filename (preferred):
python ".claude/skills/ai-gtme/clean_outbound.py" --input "job changed.csv"

# Force a signal when the filename is ambiguous:
python ".claude/skills/ai-gtme/clean_outbound.py" --input raw.csv --signal "Funding raised"

# Preview without touching real files (writes to temp/):
python ".claude/skills/ai-gtme/clean_outbound.py" --input raw.csv --dry-run
```

Run from repo root (`d:/dev/sync`). Use `python` on Windows (`python3` also accepted).

**Workflow for the agent:**
1. Get the input CSV path (ask the user; raw exports usually sit at repo root or `sales/`).
2. Confirm the detected signal. If the filename has no clear signal, **ask the user** which of `Job changed / Promoted / Funding raised / Merger / Techstack match / Competitor engagement` applies and pass `--signal`.
3. For an unfamiliar or large file, run `--dry-run` first and read `temp/cleaned-<name>.csv` to spot-check copy.
4. Run for real, then report the summary the script prints.

**Inputs read:** the raw CSV, `sales/outbound list/outbound.csv` (schema + dedup).
**Outputs written:** appends/reorders `sales/outbound list/outbound.csv`; review copy `temp/cleaned-<name>.csv`; safety backup `temp/outbound.before-append.csv`.

---

## Find Emails (enrichment)

When asked to **find / enrich emails** (or phones) for a contact or a list, **default to SyncGTM MCP** — it runs waterfall enrichment across Apollo, RocketReach, LeadMagic, Datagma, and others automatically. Call `mcp__syncgtm__check_credits` first to verify balance.

**Key MCP tools** (prefix `mcp__syncgtm__`):
- `find_work_email` — 1 credit — requires `linkedin_url`; returns work email via waterfall.
- `find_personal_email` — 3 credits — requires `linkedin_url`.
- `find_work_phone` / `find_mobile_number` — 15 credits each — requires `linkedin_url`.
- `enrich_person` — 2 credits — broader enrichment (contact + company info) from any identifier.
- `verify_email` — 0.3 credits — confirm deliverability before sending.

**For outbound rows**: `Person Linkedin Url` is the best identifier — call `find_work_email` with it, write the result into `Email`, and set `Email Status`. Use `enrich_person` when more contact fields are needed.

Fall back to direct API calls (RocketReach/Apollo via `.env` keys) only when explicitly asked or when MCP is unavailable. Full API docs in `sales/sales-integration-docs.txt`.

---

## Canonical schema (read live from outbound.csv)

The output schema is **whatever `outbound.csv`'s header currently is** — to add, rename, or remove a column, edit that header and the engine follows it. Today it is 37 columns:

```
Date added, First Name, Last Name, Title, Company Name, Email, Email Status, Seniority,
Sub Departments, Work Direct Phone, Mobile Phone, Corporate Phone, Industry, Keywords,
Person Linkedin Url, Website, Company Linkedin Url, Facebook Url, Twitter Url, City, Country,
Technologies, Annual Revenue, Total Funding, Latest Funding, Latest Funding Amount, Last Raised,
Outbound signal, LinkedIn connection request, LinkedIn first DM, LinkedIn follow up DM,
LinkedIn account, LinkedIn status, Email sequence campaign, Subject, Day 1 email, Sequence Status
```

Each column is filled one of three ways: **generated** (signal, the 3 LinkedIn fields, account, campaign, Subject, Day 1 email; plus `Date added` = run date `dd/mm`; note news-based signals leave `LinkedIn connection request` + `Day 1 email` blank for research — but `Subject` is still filled), **left blank** (`LinkedIn status`, `Sequence Status` — empty = not yet actioned), or **mapped from the raw export** via a case-insensitive alias map. Source aliases:

| Canonical column | Accepts source header |
|---|---|
| Company Name | `Company Name` / `Company` / `Company Name for Emails` |
| Person Linkedin Url | `Person Linkedin Url` / `LinkedIn` / `Person LinkedIn` |
| Company Linkedin Url | `Company Linkedin Url` / `Company LinkedIn` |
| Sub Departments | `Sub Departments` / `Subdepartments` / `Departments` |
| Last Raised | `Last Raised` / `Last Raised At` |
| Subject | `Subject` / `Email Subject` / `Subject Line` |

All other columns map by exact (case-insensitive) name. Anything not in the schema is dropped.

---

## 0. Signal from the filename

The filename decides `Outbound signal` for every row (priority order top→bottom):

| Filename contains | Outbound signal |
|---|---|
| job change / changed jobs / new job | **Job changed** |
| promot* | **Promoted** |
| fund / raise / series / seed | **Funding raised** |
| merger / merge / acqui* | **Merger** † |
| partner / alliance | **Partnership** † |
| new product / product launch / unveil / launch | **New product** † |
| techstack / tech stack / uses / crm / a tool name (Salesforce, HubSpot, …) | **Techstack match[: Tool]** |
| competitor / engage* | **Competitor engagement** |
| (none detected) | engine exits — **ask the user, pass `--signal`** |

† **News-based** — the engine leaves the news-referencing copy blank; see [News-based signals — research first](#news-based-signals--research-first).

## 1. Dedup (removed before cleaning)

Builds a seen-set from `outbound.csv`, then drops any input row that matches — keyed by **Email** → (if blank) **Person LinkedIn URL** → (if both blank) **First+Last+Company**. Emails lowercased; URLs stripped of protocol/`www`/trailing-slash/query. Intra-file duplicates dropped too. Counts reported.

## 2–5. Personalization

**Growth-news signals (Job changed, Promoted, Funding raised) open with "congrats"** in both the LinkedIn connection request and the Day 1 email. Techstack/Competitor use a non-congrats opener. **News-based signals (Merger, Partnership, New product) are research-gated — see below.**

**Company name is never mentioned in the LinkedIn connection request or Day 1 email.** Job titles are trimmed before use — anything after `@`, `|`, ` at `, or `, {company}` is stripped so only the role itself appears (e.g. `"Head of Sales @ Acme"` → `"Head of Sales"`). Hiring-signal copy uses `"saw you're hiring"` with no company reference.

### News-based signals — research first

For **Merger / Acquisition, Partnership, and New product** signals the hook depends on a *specific* company news item, so the engine deliberately leaves **`LinkedIn connection request`** and **`Day 1 email`** BLANK (the other fields — first DM, follow-up DM, account, campaign, contact data — are still filled). The report flags these rows as `research-gated`. Before filling those two fields:

1. **Find the news** for each company — the specific deal / partner / product, ideally with a date and one concrete detail. Use web search (or the `deep-research` skill for a batch), the company newsroom, or its LinkedIn page.
2. **Then personalize**, mirroring the templates and opening with "congrats" on the *concrete* news — e.g. *"{First}, congrats on the partnership with Stripe"* or *"{First}, congrats on launching {Product}"* — never a generic "congrats on the partnership".
3. If no real news is found for a row, **leave it blank** rather than inventing one.

**Title → audience / objective** (used in the LinkedIn connection request + first DM; the Day 1 email uses the fixed positioning line instead):

| Title contains | Audience | Objective |
|---|---|---|
| founder, ceo, co-founder, owner, president | founders | grow pipeline faster |
| revops, revenue ops, sales ops | revops teams | clean up their pipeline and data |
| gtm engineer, growth engineer | GTM engineers | ship outbound systems faster |
| sales, cro, ae, sdr, bdr, biz dev | sales teams | improve their outbound |
| marketing, demand gen, growth | marketing teams | drive more pipeline |
| (default) | GTM teams | run better outbound |

**Title → Email sequence campaign** (precedence: founder → revops → gtm engineer → sales/revenue → default):
`B2B founder` · `RevOps` · `GTM engineer` · `SDR-Sales` · `GTM`. The sales/revenue bucket (`SDR-Sales`) catches **any sales or revenue title** — SDR, BDR, sales, account executive, business development, CRO/chief revenue, **head of sales**, **head of revenue**, VP sales/revenue (RevOps is matched first, so `revenue operations` still routes to RevOps).

**Email Subject** (signal-based, short — news-gated kept ≤4 words). Filled for **every** row, including news-gated signals (the subject doesn't need the specific news detail the Day 1 email body does). Preserves an input `Subject` if already present.

| Signal | Subject |
|---|---|
| Job changed | `Congrats on the new role` |
| Promoted | `Congrats on the promotion` |
| Funding raised | `Congrats on the funding` |
| Merger | `Congrats on the merger` |
| Partnership | `Congrats on the partnership` |
| New product | `Congrats on the launch` |
| Techstack match | `Your GTM stack` |
| Competitor engagement | `Exploring GTM options?` |
| Hiring … (freeform) | `Saw you're hiring` |
| (default) | `Quick GTM idea` |

**LinkedIn account:** round-robin **Kushal → Aaron → Kundan**.

### Personalization (mirrored from the team's voice)

Personalize and create columns: LinkedIn connection message, Day 1 email, Subject line. 

See personalization.txt to see how to write for different ICPs based on signal column.

## 6. Append + reorder

- Cleaned rows sorted by signal priority: **Job changed → Promoted → Funding raised → Merger → Partnership → New product → Techstack match → Competitor engagement → other**.
- `outbound.csv` rewritten: rows with a **non-empty `LinkedIn status`** stay on top in existing order; rows with empty `LinkedIn status` (existing + new) sorted by signal priority below. (Legacy/freeform signals like "Recently changed jobs" are bucketed into the right priority.)

---

## Report

```
--- ai-gtme complete ---
input            : <path>
signal           : <signal>
date added       : dd/mm
schema cols      : 37 (from outbound.csv header)
rows in          : N
dupes vs outbound : N
dupes in-file    : N
cleaned rows     : N
account split    : Kushal=N, Aaron=N, Kundan=N
outbound rows    : N  -> sales/outbound list/outbound.csv
review copy      : temp/cleaned-<name>.csv
research-gated   : N rows   # news signals only — fill connection request + Day 1 email after finding company news
```

## Edge cases

- **No signal in filename** → engine exits with a message; ask the user and pass `--signal`.
- **Missing First Name** → falls back to `Full Name`'s first token; if still empty, the copy drops the name and capitalizes the opener.
- **Missing Title/Company** → templates degrade gracefully (e.g. "congrats on the new role" with no title).
- **Connection request > 300 chars** → the intro is trimmed at a word boundary; the second sentence is preserved.
- **Schema changes** → edit `outbound.csv`'s header; the engine re-derives columns from it (it does not hardcode the list).
- **Existing campaign files with an old header** → the engine appends with the current schema; if you changed the schema, clear/rebuild `sales/sequence campaigns/` so headers match.
- **Re-running** rewrites + reorders all of `outbound.csv` (backup saved to `temp/outbound.before-append.csv` each run).
