# AI GTM Engineer

A [Claude Code](https://claude.ai/code) agent that runs your outbound GTM workflow end-to-end. Drop in a raw Apollo or Clay CSV export, tell it the signal, and it handles the rest.

## What it does

**Cleans & dedupes** — Maps any Apollo/Clay export to the canonical 37-column schema, drops duplicates against `outbound.csv` and `crm.txt` (matched by email → LinkedIn URL → name+company), and strips intra-file dupes.

**Detects the signal** — Reads the outbound trigger from the filename (job change, promotion, funding, merger, techstack match, competitor engagement, etc.). If the filename is ambiguous, it asks before proceeding.

**Personalizes copy** — Generates six fields per contact tailored to their title, company, and signal:
- LinkedIn connection request (≤300 chars)
- LinkedIn first DM
- LinkedIn follow-up DM
- Day 1 email + subject line
- Email sequence campaign (B2B founder / RevOps / GTM engineer / SDR-Sales / GTM)

Growth signals (job change, promotion, funding) open with a congrats hook. News-based signals (merger, partnership, new product) are research-gated — the agent finds the specific news item first, then personalizes around it.

**Round-robins LinkedIn accounts** — Distributes rows across Kushal → Aaron → Kundan automatically.

**Appends & reorders `outbound.csv`** — Actioned rows (non-empty LinkedIn status) stay on top; new and unactioned rows sort below by signal priority. A timestamped backup is saved before every write.

**Enriches emails & phones** — Calls the SyncGTM MCP waterfall (Apollo, RocketReach, LeadMagic, Datagma, and others) to find work emails, personal emails, and phone numbers from LinkedIn URLs. Checks credit balance before running.

Trigger it with `/ai-gtme` in Claude Code.

---

## Environment variables

Create a `.env` file at the repo root for direct API fallbacks (used when SyncGTM MCP is unavailable or you want to call providers directly):

```env
# Apollo.io — enrichment + export source
APOLLO_API_KEY=your_apollo_api_key

# RocketReach — email & phone enrichment fallback
ROCKETREACH_API_KEY=your_rocketreach_api_key

# Instantly — email sequencing
INSTANTLY_API_KEY=your_instantly_api_key
```

SyncGTM MCP (set up below) is the default for enrichment and covers Apollo, RocketReach, and others automatically — these keys are only needed as a fallback or for direct API calls.

---

## Setup: SyncGTM MCP (email & phone enrichment)

### 1. Get your API token

1. Sign up or log in at [app.syncgtm.com](https://app.syncgtm.com)
2. Go to **Settings**
3. Copy your API token — it starts with `rxk_`

### 2. Add the MCP server to Claude Code

```bash
claude mcp add --transport http syncgtm https://api.syncgtm.com/mcp --header "Authorization: Bearer rxk_YOUR_API_TOKEN"
```

Replace `rxk_YOUR_API_TOKEN` with your actual token, then restart Claude Code.

### Alternative: manual config (Cursor, VS Code, Windsurf)

```json
{
  "mcpServers": {
    "syncgtm": {
      "type": "http",
      "url": "https://api.syncgtm.com/mcp",
      "headers": {
        "Authorization": "Bearer rxk_YOUR_API_TOKEN"
      }
    }
  }
}
```
