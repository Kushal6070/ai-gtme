#!/usr/bin/env python3
"""sync-gtme — outbound CSV cleaner + LinkedIn/email personalizer.

Pipeline for one raw Apollo/Clay CSV export:
  1. Detect the Outbound signal from the input filename (or --signal).
  2. Dedup the input against crm.txt + outbound.csv (and itself).
  3. Clean columns down to the canonical schema -- which is read LIVE from the
     header of sales/outbound list/outbound.csv, so header edits there (rename
     / add / remove a column) are respected automatically.
  4. Fill personalized LinkedIn connection request, first DM, follow-up DM,
     account (round-robin), email sequence campaign, and Day 1 email.
  5. Sort cleaned rows by signal priority.
  6. Append into outbound.csv and reorder: rows with a non-empty LinkedIn status
     stay on top in existing order; empty-status rows are sorted by priority below.
  7. Split the new rows per campaign into sales/sequence campaigns/<campaign>.csv.

Usage:
  python ".claude/skills/sync-gtme/clean_outbound.py" --input "job changed.csv"
  python ".claude/skills/sync-gtme/clean_outbound.py" --input raw.csv --signal "Funding raised"
  python ".claude/skills/sync-gtme/clean_outbound.py" --input raw.csv --dry-run

Run from the repo root (d:/dev/sync).
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

# --- defaults (relative to repo root / CWD) ---
DEFAULT_OUTBOUND = "sales/outbound list/outbound.csv"
DEFAULT_CRM = "sales/outbound list/crm.txt"
DEFAULT_CAMPAIGNS = "sales/sequence campaigns"

LINKEDIN_ACCOUNTS = ["Kushal", "Aaron", "Kundan"]

# Signal sort priority (lower = higher). Growth-news signals lead.
SIGNAL_PRIORITY = {
    "job changed": 1,
    "promoted": 2,
    "funding raised": 3,
    "merger": 4,
    "partnership": 5,
    "new product": 6,
    "techstack match": 7,
    "competitor engagement": 8,
}

# Canonical capitalization for an explicitly-passed --signal.
CANON = {
    "job changed": "Job changed",
    "promoted": "Promoted",
    "funding raised": "Funding raised",
    "merger": "Merger",
    "partnership": "Partnership",
    "new product": "New product",
    "techstack match": "Techstack match",
    "competitor engagement": "Competitor engagement",
}

# Growth-news signals open with "congrats".
GROWTH_SIGNALS = ("job changed", "promoted", "funding raised", "merger")

# News-based signals: the hook depends on a SPECIFIC company news item, so the
# engine leaves the news-referencing copy (LinkedIn connection request + Day 1
# email) BLANK. Research the company's news first, then write those two fields.
NEWS_SIGNALS = ("merger", "partnership", "new product")

# Output columns the script GENERATES (matched case-insensitively, paren-stripped).
GENERATED = {
    "outbound signal", "linkedin connection request", "linkedin first dm",
    "linkedin follow up dm", "linkedin account", "email sequence campaign", "subject", "day 1 email",
}
# Output columns left blank for new rows (progress trackers).
BLANK = {"linkedin status", "sequence status"}

# canonical output column (normalized) -> candidate source headers (normalized).
ALIASES = {
    "company name": ["company name", "company", "company name for emails"],
    "person linkedin url": ["person linkedin url", "linkedin", "person linkedin", "linkedin url"],
    "last raised": ["last raised", "last raised at"],
    "sub departments": ["sub departments", "subdepartments", "departments", "sub department"],
    "company linkedin url": ["company linkedin url", "company linkedin"],
    "work direct phone": ["work direct phone", "phone"],
    "day 1 email": ["day 1 email", "day 1 - email"],
    "linkedin connection request": ["linkedin connection request", "linkedin connection dm"],
    "subject": ["subject", "email subject", "subject line"],
}

# crm.txt column (normalized) <- cleaned-row column (normalized) when names differ.
# crm.txt keeps its own (wider) schema; this fills its dedup-critical fields so
# future runs catch these contacts. "full name" / "location" are derived below.
CRM_FROM_CLEAN = {
    "linkedin": ["person linkedin url"],
    "company": ["company name"],
    "company name for emails": ["company name"],
    "company linkedin": ["company linkedin url"],
    "signal": ["outbound signal"],
    "custom first signal": ["outbound signal"],
    "linkedin connection dm": ["linkedin connection request"],
    "status": ["linkedin status"],
    "last raised at": ["last raised"],
    "day 1 - email": ["day 1 email"],
}

TOOLS = {
    "salesforce": "Salesforce", "hubspot": "HubSpot", "pipedrive": "Pipedrive",
    "outreach": "Outreach", "salesloft": "Salesloft", "apollo": "Apollo",
    "clay": "Clay", "zoominfo": "ZoomInfo", "gong": "Gong", "marketo": "Marketo",
}


# ---------- helpers ----------
def norm(s: str) -> str:
    s = re.sub(r"\(.*?\)", "", s or "")
    s = s.replace("_", " ")
    return re.sub(r"\s+", " ", s).strip().lower()


def nmap(row: dict) -> dict:
    return {norm(k): (v or "") for k, v in row.items()}


def norm_email(e: str) -> str:
    return (e or "").strip().lower()


def norm_url(u: str) -> str:
    u = (u or "").strip().lower()
    if not u:
        return ""
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^www\.", "", u)
    u = u.split("?")[0].split("#")[0]
    return u.rstrip("/")


def name_key(first: str, last: str, company: str) -> str:
    return "|".join([(first or "").strip().lower(), (last or "").strip().lower(),
                     (company or "").strip().lower()])


def read_rows(path: str | Path) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    with p.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def add_seen(rows: list[dict], emails: set, urls: set, names: set) -> None:
    for row in rows:
        m = nmap(row)
        e = norm_email(m.get("email", ""))
        if e:
            emails.add(e)
        u = norm_url(m.get("person linkedin url") or m.get("linkedin") or "")
        if u:
            urls.add(u)
        nk = name_key(m.get("first name", ""), m.get("last name", ""),
                      m.get("company name") or m.get("company") or "")
        if nk.strip("|"):
            names.add(nk)


def find_col(schema: list[str], target_norm: str) -> str | None:
    for c in schema:
        if norm(c) == target_norm:
            return c
    return None


def priority(signal: str) -> int:
    """Signal -> sort rank. Recognizes the canonical signals plus common
    freeform/legacy phrasings already in outbound.csv (e.g. 'Recently changed
    jobs'), so the reorder behaves sensibly on existing data."""
    n = norm(signal)
    if not n:
        return 99
    for k, v in SIGNAL_PRIORITY.items():
        if n.startswith(k):
            return v
    if re.search(r"job change|changed job|new job|new role", n):
        return SIGNAL_PRIORITY["job changed"]
    if "promot" in n:
        return SIGNAL_PRIORITY["promoted"]
    if re.search(r"fund|raise|series|seed", n):
        return SIGNAL_PRIORITY["funding raised"]
    if re.search(r"merger|merge|acqui", n):
        return SIGNAL_PRIORITY["merger"]
    if re.search(r"partner|alliance", n):
        return SIGNAL_PRIORITY["partnership"]
    if re.search(r"new product|product launch|product release|unveil|launch", n):
        return SIGNAL_PRIORITY["new product"]
    if re.search(r"tech ?stack|\buses\b|\bcrm\b", n):
        return SIGNAL_PRIORITY["techstack match"]
    if re.search(r"competitor|engag", n):
        return SIGNAL_PRIORITY["competitor engagement"]
    return 99


def detect_signal(filename: str) -> str:
    f = filename.lower()
    if re.search(r"job[\s_-]*chang|chang\w*[\s_-]*job|new job", f):
        return "Job changed"
    if "promot" in f:
        return "Promoted"
    if re.search(r"fund|raise|raised|series|seed", f):
        return "Funding raised"
    if re.search(r"merger|\bmerge|acqui", f):
        return "Merger"
    if re.search(r"partner|alliance", f):
        return "Partnership"
    if re.search(r"new product|product launch|product release|new release|unveil|launch", f):
        return "New product"
    if re.search(r"tech[\s_-]*stack|techstack|\buses\b|\bcrm\b", f) or any(t in f for t in TOOLS):
        tool = next((TOOLS[t] for t in TOOLS if t in f), "")
        return f"Techstack match: {tool}" if tool else "Techstack match"
    if re.search(r"competitor|engag", f):
        return "Competitor engagement"
    return ""


# ---------- personalization ----------
def title_profile(title: str):
    """Return (audience, objective, email_goal) for a job title."""
    t = (title or "").lower()

    def has(*ws):
        return any(w in t for w in ws)

    # "president" only counts as a founder-tier title when it's not "vice president".
    pres = "president" in t and "vice president" not in t and "vp" not in t
    if pres or has("founder", "co-found", "ceo", "chief executive", "owner"):
        return ("founders", "grow pipeline faster", "build stronger pipeline")
    if has("revops", "rev ops", "revenue ops", "revenue operations", "sales ops", "sales operations"):
        return ("revops teams", "clean up their pipeline and data", "fix CRM data and pipeline leaks")
    if has("gtm engineer", "growth engineer", "automation engineer"):
        return ("GTM engineers", "ship outbound systems faster", "automate signal-driven outbound")
    if has("sales", "cro", "chief revenue", "account executive", "business development",
            "sdr", "bdr", "sales development", "head of revenue", "head of sales",
            "vp sales", "vp revenue", "revenue officer", "revenue"):
        return ("sales teams", "improve their outbound", "book more meetings")
    if has("marketing", "demand gen", "growth", "abm"):
        return ("marketing teams", "drive more pipeline", "turn signals into pipeline")
    return ("GTM teams", "run better outbound", "ship signal-driven plays faster")


def campaign_for(title: str) -> str:
    t = (title or "").lower()

    def has(*ws):
        return any(w in t for w in ws)

    pres = "president" in t and "vice president" not in t and "vp" not in t
    if pres or has("founder", "co-found", "ceo", "chief executive", "owner"):
        return "B2B founder"
    if has("revops", "rev ops", "revenue ops", "revenue operations", "sales ops", "sales operations"):
        return "RevOps"
    if has("gtm engineer", "growth engineer", "automation engineer"):
        return "GTM engineer"
    if has("sdr", "bdr", "sales", "cro", "chief revenue", "account executive",
            "business development", "sales development", "head of revenue", "head of sales",
            "vp sales", "vp revenue", "revenue officer", "revenue"):
        return "SDR-Sales"
    return "GTM"


def _trim_title(title: str, company: str = "") -> str:
    """Strip company name / LinkedIn bio suffixes from a job title.

    Handles patterns like:
      'Head of Sales @ WorkBright | GTM Leader'  -> 'Head of Sales'
      'VP GTM @ Codacy'                          -> 'VP GTM'
      'Co-founder at Trendskout. We help...'     -> 'Co-founder'
      'Go to Market Manager Niko'                -> 'Go to Market Manager'  (co=Niko)
    """
    import re as _re
    t = (title or "").strip()
    # Strip ' @ ...' and everything after
    t = _re.split(r'\s*@\s*', t)[0].strip()
    # Strip ' | ...' separator (LinkedIn bio append)
    t = _re.split(r'\s*\|\s*', t)[0].strip()
    # Strip '. <uppercase sentence>' (bio appended after period).
    # Require ≥4 word chars before the period so abbreviations like 'Sr.' / 'Dr.' are not split.
    t = _re.split(r'(?<=[A-Za-z]{4})\.\s+[A-Z]', t)[0].strip()
    # Strip ' at <company>' suffix
    if company:
        co = company.strip()
        for pat in [
            rf',\s*{_re.escape(co)}.*$',       # ', Salesforce - Retail...'
            rf'\s+-\s+{_re.escape(co)}.*$',     # ' - Forcepoint'
            rf'\s+at\s+{_re.escape(co)}.*$',    # ' at Trendskout'
            rf'\s+{_re.escape(co)}\s*$',        # ' Niko'  (trailing)
        ]:
            t = _re.sub(pat, '', t, flags=_re.IGNORECASE).strip()
    return t


def linkedin_intro(signal: str, title: str, company: str) -> str:
    s = norm(signal)
    t = _trim_title(title, company)
    if s.startswith("job changed"):
        return f"congrats on the new role as {t}" if t else "congrats on the new role"
    if s.startswith("promoted"):
        return f"congrats on the promotion to {t}" if t else "congrats on the promotion"
    if s.startswith("funding raised"):
        return "congrats on the recent raise"
    if s.startswith("merger"):
        return "congrats on the merger"
    if s.startswith("partnership"):
        return "congrats on the partnership"
    if s.startswith("new product"):
        return "congrats on the product launch"
    if s.startswith("techstack match"):
        return "noticed you run a sharp GTM stack"
    if s.startswith("competitor engagement"):
        return "saw you're weighing options in the GTM space"
    if "hiring" in s:
        return "saw you're hiring"
    return "wanted to reach out"


def email_opening(signal: str, title: str, company: str) -> str:
    s = norm(signal)
    t = _trim_title(title, company)
    if s.startswith("job changed"):
        return "congrats on the new role"
    if s.startswith("promoted"):
        return f"congrats on the promotion to {t}" if t else "congrats on the well-earned promotion"
    if s.startswith("funding raised"):
        return "congrats on the recent round"
    if s.startswith("merger"):
        return "congrats on the merger"
    if s.startswith("partnership"):
        return "congrats on the partnership"
    if s.startswith("new product"):
        return "congrats on the product launch"
    if s.startswith("techstack match"):
        return "saw the GTM stack you've built"
    if s.startswith("competitor engagement"):
        return "noticed you're exploring GTM tooling"
    if "hiring" in s:
        return "saw you're hiring"
    return "wanted to reach out"


def subject_line(signal: str) -> str:
    """Signal-based email subject (short — news-gated signals stay <=4 words).
    Filled for every row regardless of news-gating (the subject doesn't need the
    specific news detail the Day 1 email body does)."""
    s = norm(signal)
    if s.startswith("job changed"):
        return "Congrats on the new role"
    if s.startswith("promoted"):
        return "Congrats on the promotion"
    if s.startswith("funding raised"):
        return "Congrats on the funding"
    if s.startswith("merger"):
        return "Congrats on the merger"
    if s.startswith("partnership"):
        return "Congrats on the partnership"
    if s.startswith("new product"):
        return "Congrats on the launch"
    if s.startswith("techstack match"):
        return "Your GTM stack"
    if s.startswith("competitor engagement"):
        return "Exploring GTM options?"
    if "hiring" in s:  # freeform "Hiring SDRs / GTM Engineers / RevOps" signals
        return "Saw you're hiring"
    return "Quick GTM idea"


def first_of(name: str) -> str:
    parts = (name or "").strip().split()
    return parts[0] if parts else ""


def _lead(first: str, clause: str) -> str:
    if first:
        return f"{first}, {clause}"
    return clause[:1].upper() + clause[1:] if clause else ""


def connection_request(first: str, signal: str, title: str, company: str) -> str:
    aud, _, _ = title_profile(title)
    intro = linkedin_intro(signal, title, company)
    tail = f"\n\nBuilding AI tools and agents to help {aud} outreach better.\n\nWould love to connect."
    msg = f"{_lead(first, intro)}.{tail}"
    if len(msg) <= 300:
        return msg
    # Trim the intro at a word boundary so the second sentence stays intact.
    prefix = f"{first}, " if first else ""
    budget = 300 - len(tail) - len(prefix) - 1  # -1 for trailing period
    words = intro.split()
    while words and len(" ".join(words)) > budget:
        words.pop()
    introt = " ".join(words) or intro[:max(0, budget)]
    return f"{_lead(first, introt)}.{tail}"


def first_dm(first: str, title: str) -> str:
    aud, obj, _ = title_profile(title)
    lead = f"Great to connect {first}." if first else "Great to connect."
    return f"{lead} We built an AI playbook for {aud} to {obj}. Thought I'd share."


def followup_dm(first: str) -> str:
    lead = (f"{first}, did you get a chance to look at the playbook?" if first
            else "Did you get a chance to look at the playbook?")
    return f"{lead} Happy to walk you through it."


# Fixed positioning line for the Day 1 email body.
POSITIONING_EMAIL = ("We build go-to-market AI systems that help revenue teams "
                     "automate and improve outreach.")


def day1_email(first: str, signal: str, title: str, company: str) -> str:
    opening = email_opening(signal, title, company)
    return (f"{_lead(first, opening)}.\n\n"
            f"{POSITIONING_EMAIL}\n\n"
            f"Can I share a playbook that explains how?")


# ---------- io ----------
def write_csv(path: Path, schema: list[str], rows: list[dict], *, atomic: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    target = path.with_suffix(path.suffix + ".tmp") if atomic else path
    with target.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=schema, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in schema})
    if atomic:
        os.replace(target, path)


def crm_value(col: str, cm: dict) -> str:
    """Resolve one crm.txt column from a cleaned row's normalized map `cm`."""
    n = norm(col)
    if n == "full name":
        return f"{cm.get('first name', '').strip()} {cm.get('last name', '').strip()}".strip()
    if n == "location":
        return ", ".join(x for x in (cm.get("city", "").strip(), cm.get("country", "").strip()) if x)
    if n in cm and (cm[n] or "").strip():
        return cm[n]
    for cand in CRM_FROM_CLEAN.get(n, []):
        if cand in cm and (cm[cand] or "").strip():
            return cm[cand]
    return ""


def append_to_crm(cleaned: list[dict], crm_path: str, fallback_schema: list[str],
                  dry_run: bool) -> tuple[Path, int]:
    """Append the cleaned rows to crm.txt, mapped to crm.txt's own header, so it
    stays the running dedup master. Rewrites header + existing + new atomically."""
    crm_p = Path(crm_path)
    if crm_p.exists():
        with crm_p.open("r", encoding="utf-8-sig", newline="") as f:
            rdr = csv.DictReader(f)
            crm_schema = list(rdr.fieldnames or []) or list(fallback_schema)
            crm_rows = list(rdr)
    else:
        crm_schema = list(fallback_schema)
        crm_rows = []

    mapped = []
    for r in cleaned:
        cm = {norm(k): (v or "") for k, v in r.items()}
        mapped.append({c: crm_value(c, cm) for c in crm_schema})

    target = crm_p if not dry_run else Path("temp/crm.dryrun.txt")
    if not dry_run and crm_p.exists():
        shutil.copy2(crm_p, Path("temp/crm.before-append.txt"))

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=crm_schema, extrasaction="ignore")
        w.writeheader()
        for r in crm_rows:
            w.writerow({c: r.get(c, "") for c in crm_schema})
        for r in mapped:
            w.writerow({c: r.get(c, "") for c in crm_schema})
    os.replace(tmp, target)
    return target, len(mapped)


# ---------- main ----------
def main() -> None:
    ap = argparse.ArgumentParser(description="sync-gtme outbound cleaner")
    ap.add_argument("--input", required=True, help="raw Apollo/Clay CSV to clean")
    ap.add_argument("--signal", default="auto",
                    help="Outbound signal, or 'auto' to detect from filename")
    ap.add_argument("--date", default="", help="Date added as dd/mm (default: today)")
    ap.add_argument("--outbound", default=DEFAULT_OUTBOUND)
    ap.add_argument("--crm", default=DEFAULT_CRM)
    ap.add_argument("--campaigns-dir", default=DEFAULT_CAMPAIGNS)
    ap.add_argument("--dry-run", action="store_true",
                    help="write results to temp/ instead of the real files")
    ap.add_argument("--force", action="store_true",
                    help="skip dedup check — reprocess contacts already in CRM/outbound")
    args = ap.parse_args()

    inp = Path(args.input)
    if not inp.exists():
        sys.exit(f"ERROR: input not found: {inp}")

    date_added = args.date or datetime.now().strftime("%d/%m")

    # signal
    if args.signal and args.signal.lower() != "auto":
        signal = CANON.get(norm(args.signal), args.signal)
        news_gated = norm(signal).startswith(NEWS_SIGNALS)
    else:
        signal = detect_signal(inp.name)
        if not signal:
            # Check for a per-row signal column below; if absent, exit with guidance.
            # We'll defer the exit until after in_rows is loaded.
            signal = ""
        news_gated = norm(signal).startswith(NEWS_SIGNALS) if signal else False

    # canonical schema = live outbound.csv header
    outbound_path = Path(args.outbound)
    if not outbound_path.exists():
        sys.exit(f"ERROR: outbound not found: {outbound_path}")
    with outbound_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        schema = list(reader.fieldnames or [])
        existing_rows = list(reader)
    if not schema:
        sys.exit("ERROR: outbound.csv has no header")

    col_sig = find_col(schema, "outbound signal")
    col_listat = find_col(schema, "linkedin status")
    col_camp = find_col(schema, "email sequence campaign")

    # dedup seen-sets from crm.txt + outbound.csv
    emails: set = set()
    urls: set = set()
    names: set = set()
    if not args.force:
        add_seen(read_rows(args.crm), emails, urls, names)
        add_seen(existing_rows, emails, urls, names)

    # classify each output column once
    roles = []
    for col in schema:
        n = norm(col)
        if n == "date added":
            roles.append((col, "date", None))
        elif n in GENERATED:
            roles.append((col, "gen", n))
        elif n in BLANK:
            roles.append((col, "blank", None))
        else:
            roles.append((col, "src", ALIASES.get(n, [n])))

    in_rows = read_rows(inp)
    total_in = len(in_rows)

    # Per-row signal mode: when the input file has a "signal" column and no fixed
    # --signal was given, each row gets its own signal from that column.
    in_headers_norm = {norm(k) for k in (in_rows[0].keys() if in_rows else [])}
    if not signal and "signal" not in in_headers_norm:
        sys.exit('ERROR: could not detect a signal from the filename and no "signal" column found. '
                 'Pass --signal "Job changed|Promoted|Funding raised|Merger|'
                 'Partnership|New product|Techstack match|Competitor engagement".')
    per_row_signal = (args.signal.lower() == "auto" and "signal" in in_headers_norm)

    cleaned: list[dict] = []
    dup_existing = 0
    dup_self = 0
    local_e: set = set()
    local_u: set = set()
    local_n: set = set()
    idx = 0
    for raw in in_rows:
        m = nmap(raw)
        e = norm_email(m.get("email", ""))
        u = norm_url(m.get("person linkedin url") or m.get("linkedin") or "")
        nk = name_key(m.get("first name", ""), m.get("last name", ""),
                      m.get("company name") or m.get("company") or "")
        if (e and e in emails) or (u and u in urls) or (not e and not u and nk in names):
            dup_existing += 1
            continue
        if (e and e in local_e) or (u and u in local_u) or (not e and not u and nk in local_n):
            dup_self += 1
            continue
        if e:
            local_e.add(e)
        if u:
            local_u.add(u)
        if nk.strip("|"):
            local_n.add(nk)

        title = m.get("title", "")
        company = m.get("company name") or m.get("company") or ""
        fn = first_of(m.get("first name", "") or m.get("full name", ""))

        # Resolve this row's signal (per-row or fixed).
        if per_row_signal:
            raw_sig = m.get("signal", "").strip()
            row_signal = CANON.get(norm(raw_sig), raw_sig) if raw_sig else signal
        else:
            row_signal = signal
        row_news_gated = norm(row_signal).startswith(NEWS_SIGNALS)

        out = {}
        for col, kind, info in roles:
            if kind == "date":
                out[col] = date_added
            elif kind == "blank":
                out[col] = ""
            elif kind == "gen":
                if info == "outbound signal":
                    out[col] = row_signal
                elif info == "linkedin connection request":
                    # Preserve existing value from input if already filled.
                    existing_val = ""
                    for cand in ALIASES.get("linkedin connection request", ["linkedin connection request"]):
                        if cand in m and (m[cand] or "").strip():
                            existing_val = m[cand]
                            break
                    if existing_val:
                        out[col] = existing_val
                    else:
                        out[col] = connection_request(fn, row_signal, title, company)
                elif info == "linkedin first dm":
                    out[col] = first_dm(fn, title)
                elif info == "linkedin follow up dm":
                    out[col] = followup_dm(fn)
                elif info == "linkedin account":
                    out[col] = LINKEDIN_ACCOUNTS[idx % len(LINKEDIN_ACCOUNTS)]
                elif info == "email sequence campaign":
                    out[col] = campaign_for(title)
                elif info == "subject":
                    # Preserve existing value from input if already filled.
                    existing_subj = ""
                    for cand in ALIASES.get("subject", ["subject"]):
                        if cand in m and (m[cand] or "").strip():
                            existing_subj = m[cand]
                            break
                    # Filled for every row (incl. news-gated): signal-only subject.
                    out[col] = existing_subj or subject_line(row_signal)
                elif info == "day 1 email":
                    # Preserve existing value from input if already filled.
                    existing_day1 = ""
                    for cand in ALIASES.get("day 1 email", ["day 1 email"]):
                        if cand in m and (m[cand] or "").strip():
                            existing_day1 = m[cand]
                            break
                    if existing_day1:
                        out[col] = existing_day1
                    else:
                        out[col] = day1_email(fn, row_signal, title, company)
                else:
                    out[col] = ""
            else:  # src
                val = ""
                for cand in info:
                    if cand in m and (m[cand] or "").strip():
                        val = m[cand]
                        break
                out[col] = val
        cleaned.append(out)
        idx += 1

    # sort cleaned by signal priority (handles mixed signals defensively)
    if col_sig:
        cleaned.sort(key=lambda r: priority(r.get(col_sig, "")))

    # When --force, drop existing rows that match any contact in cleaned (upsert).
    if args.force and cleaned:
        clean_emails = {norm_email(r.get(find_col(schema, "email") or "Email", "")) for r in cleaned if norm_email(r.get(find_col(schema, "email") or "Email", ""))}
        clean_urls = {norm_url(r.get(find_col(schema, "person linkedin url") or "Person Linkedin Url", "")) for r in cleaned if norm_url(r.get(find_col(schema, "person linkedin url") or "Person Linkedin Url", ""))}
        def _is_replaced(row: dict) -> bool:
            m2 = nmap(row)
            e2 = norm_email(m2.get("email", ""))
            u2 = norm_url(m2.get("person linkedin url") or m2.get("linkedin") or "")
            return (e2 and e2 in clean_emails) or (u2 and u2 in clean_urls)
        existing_rows = [r for r in existing_rows if not _is_replaced(r)]

    # append + reorder outbound.csv
    combined = existing_rows + cleaned
    if col_listat:
        top = [r for r in combined if (r.get(col_listat, "") or "").strip()]
        bottom = [r for r in combined if not (r.get(col_listat, "") or "").strip()]
    else:
        top, bottom = [], combined
    if col_sig:
        bottom.sort(key=lambda r: priority(r.get(col_sig, "")))
    final = top + bottom

    if args.dry_run:
        out_target = Path("temp/outbound.dryrun.csv")
        camp_dir = Path("temp/sequence-campaigns-dryrun")
    else:
        out_target = outbound_path
        camp_dir = Path(args.campaigns_dir)
        # safety backup before rewriting the master list
        shutil.copy2(outbound_path, Path("temp/outbound.before-append.csv"))

    write_csv(out_target, schema, final, atomic=not args.dry_run)

    # always write a review copy of just the cleaned rows
    review = Path("temp") / f"cleaned-{inp.stem}.csv"
    write_csv(review, schema, cleaned)

    # split new rows per campaign
    by_camp: dict[str, list[dict]] = {}
    for r in cleaned:
        camp = (r.get(col_camp, "") if col_camp else "") or "GTM"
        by_camp.setdefault(camp, []).append(r)
    camp_dir.mkdir(parents=True, exist_ok=True)

    # Email/URL sets for fast upsert matching in campaign files.
    if args.force and cleaned:
        _force_emails = clean_emails
        _force_urls = clean_urls
    else:
        _force_emails = _force_urls = set()

    for camp, rows in sorted(by_camp.items()):
        p = camp_dir / f"{camp}.csv"
        # --force: read existing rows, drop those matching cleaned contacts, rewrite.
        if args.force and p.exists() and not args.dry_run:
            with p.open("r", encoding="utf-8-sig", newline="") as f:
                rdr = csv.DictReader(f)
                existing_camp_rows = list(rdr)
            kept = []
            for er in existing_camp_rows:
                em2 = nmap(er)
                ee = norm_email(em2.get("email", ""))
                eu = norm_url(em2.get("person linkedin url") or em2.get("linkedin") or "")
                if not ((ee and ee in _force_emails) or (eu and eu in _force_urls)):
                    kept.append(er)
            with p.open("w", encoding="utf-8", newline="") as f:
                w = csv.DictWriter(f, fieldnames=schema, extrasaction="ignore")
                w.writeheader()
                for er in kept:
                    w.writerow({c: er.get(c, "") for c in schema})
                for r in rows:
                    w.writerow({c: r.get(c, "") for c in schema})
        else:
            existed = p.exists()
            with p.open("a", encoding="utf-8", newline="") as f:
                w = csv.DictWriter(f, fieldnames=schema, extrasaction="ignore")
                if not existed:
                    w.writeheader()
                for r in rows:
                    w.writerow({c: r.get(c, "") for c in schema})

    # add the same rows to crm.txt (the dedup master), mapped to its header
    crm_target, crm_added = append_to_crm(cleaned, args.crm, schema, args.dry_run)

    # report
    acct_split = {a: sum(1 for r in cleaned if r.get(find_col(schema, "linkedin account"), "") == a)
                  for a in LINKEDIN_ACCOUNTS}
    print("--- sync-gtme complete ---")
    print(f"input            : {inp}")
    print(f"signal           : {'(per-row)' if per_row_signal else signal}")
    print(f"date added       : {date_added}")
    print(f"schema cols      : {len(schema)} (from outbound.csv header)")
    print(f"rows in          : {total_in}")
    print(f"dupes vs crm/ob  : {dup_existing}")
    print(f"dupes in-file    : {dup_self}")
    print(f"cleaned rows     : {len(cleaned)}")
    print(f"campaigns        : " + ", ".join(f"{k}={len(v)}" for k, v in sorted(by_camp.items())))
    print(f"account split    : " + ", ".join(f"{k}={v}" for k, v in acct_split.items()))
    print(f"outbound rows    : {len(final)}  -> {out_target}")
    print(f"added to crm     : {crm_added}  -> {crm_target}")
    print(f"review copy      : {review}")
    print(f"campaign dir     : {camp_dir}")
    news_col = find_col(schema, "outbound signal")
    li_col = find_col(schema, "linkedin connection request")
    gated_count = sum(
        1 for r in cleaned
        if norm(r.get(news_col, "") if news_col else "").startswith(NEWS_SIGNALS)
        and not (r.get(li_col, "") if li_col else "").strip()
    )
    if gated_count:
        print(f"research-gated   : {gated_count} rows -- find each company's news, then fill "
              f"'LinkedIn connection request' + 'Day 1 email'")
    if args.dry_run:
        print("** DRY RUN -- real outbound.csv and sales/sequence campaigns/ were NOT touched **")


if __name__ == "__main__":
    main()
