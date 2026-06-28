# Personal AI Employee — Gold Tier

> Autonomous FTE powered by **Claude Code** + **Obsidian** + **MCP servers**
> Built for Panaversity Hackathon 0: *Building Autonomous FTEs in 2026*

---

## What This Is

A fully autonomous AI employee that monitors every channel your business runs on
(email, WhatsApp, LinkedIn, Twitter, Facebook, Instagram, YouTube, filesystem),
correlates signals across personal and business domains, drafts responses and
documents, manages accounting in Odoo, and publishes approved social content —
all with a human-in-the-loop (HITL) safety gate before any sensitive action executes.

Claude Code is the reasoning engine. An Obsidian vault is the memory and
workspace. Python watchers are the senses. Node.js MCP servers are the hands.

---

## Tier Progress

| Tier | Status | Scope |
|------|--------|-------|
| Bronze | ✅ Complete | Vault structure, file watcher, Gmail, base infra |
| Silver | ✅ Complete | WhatsApp, LinkedIn, orchestrator, HITL, scheduler, 7 skills |
| **Gold** | ✅ **Complete** | Twitter, Facebook/IG, cross-domain, Odoo, social-post MCP |
| Platinum | — | Multi-agent swarms, self-improvement loop |

---

## Architecture Overview

```
┌──────────────────────────────────────────────────────────────┐
│                       Orchestrator                           │
│           (orchestrator.py — master process)                 │
│                                                              │
│  ┌──────────────┐   ┌──────────────┐   ┌─────────────────┐  │
│  │   Watchers   │   │  HITL Loop   │   │    Scheduler    │  │
│  │  (8 agents)  │   │  (/Approved  │   │  (CEO briefing  │  │
│  │              │   │   polling)   │   │   Mon 07:00)    │  │
│  └──────┬───────┘   └──────┬───────┘   └─────────────────┘  │
│         │                  │                                  │
│         ▼                  ▼                                  │
│    /Needs_Action      Claude Code                            │
│    /Cross_Domain      (reasoning engine)                     │
│    /Pending_Approval                                         │
└──────────────────────────────────────────────────────────────┘
         │
         │  5 MCP Servers
         ▼
┌──────────────────────────────────────────────────────────────┐
│  linkedin-post  │  gmail-send  │  youtube                    │
│  odoo-accounting  │  social-post                             │
└──────────────────────────────────────────────────────────────┘
```

### Data Flow

```
Channel signal
    → Watcher detects it
    → Creates *.md in /Needs_Action
    → CrossDomainIntegrator correlates across channels
    → Orchestrator picks it up → spawns ralph_loop
    → ralph_loop builds prompt + reads skill file
    → Claude Code reasons, drafts response
    → Sensitive action → /Pending_Approval  ← Human reviews
    → Human moves to /Approved
    → Orchestrator executes via MCP tool
    → Result logged to /Logs, archived to /Done
    → Dashboard.md updated
```

---

## Components

### Watchers (Python — `uv run python`)

All watchers extend `BaseWatcher` and implement two methods:
`check_for_updates() → list[dict]` and `create_action_file(item) → Path`.

| Watcher | Script | Domain | Output prefix |
|---------|--------|--------|---------------|
| File | `filesystem_watcher.py` | Personal | `FILE_` |
| Gmail | `gmail_watcher.py` | Personal | `EMAIL_` |
| WhatsApp | `whatsapp_watcher.py` | Personal | `WHATSAPP_` |
| LinkedIn | `linkedin_watcher.py` | Business | `LINKEDIN_` |
| YouTube | `youtube_watcher.py` | Business | `YOUTUBE_` |
| Twitter/X | `twitter_watcher.py` | Business | `TWITTER_MENTION_`, `TWITTER_DM_` |
| Facebook + Instagram | `facebook_watcher.py` | Business | `FACEBOOK_COMMENT_`, `FACEBOOK_MESSAGE_`, `INSTAGRAM_COMMENT_`, `INSTAGRAM_MENTION_` |
| Cross-Domain | `cross_domain_integration.py` | Both | `CROSS_` |

#### Twitter Watcher (`twitter_watcher.py`)
- Uses `tweepy.Client` (API v2) — more reliable than scraping
- Monitors @mentions with `since_id` pagination (no duplicates ever re-processed)
- Monitors DMs via `get_dm_events`
- Rate-limit aware: `TooManyRequests` → skips cycle, retries next poll
- Default poll interval: 150 s (safe within 5 requests/15 min free-tier limit)
- Credentials: 5 env vars — Bearer Token + OAuth 1.0a (key/secret + token/secret)

#### Facebook + Instagram Watcher (`facebook_watcher.py`)
- Single watcher covers both platforms via Facebook Graph API v19.0
- Facebook: new comments on page posts + unread Messenger conversations
- Instagram: new comments on media + @mention tags via `/tags` endpoint
- Expired token detection: Graph error code 190 → actionable log warning
- Rate limits: codes 4, 17, 32, 613 handled gracefully (skip cycle)
- Single long-lived Page Access Token covers both platforms

#### Cross-Domain Integrator (`cross_domain_integration.py`)
- Scans `/Needs_Action` every 30 s for all watcher output files
- Classifies each item by domain (personal vs business) using `SOURCE_RULES`
- `CorrelationIndex` detects the same sender appearing across multiple channels
  within a configurable sliding window (default 24 h)
- Correlated items written to `/Cross_Domain/Needs_Action/CROSS_*.md`
- Items requiring approval written to `/Pending_Approval/APPROVAL_*.md`
- Index persisted at `/Cross_Domain/index.json` — survives restarts

```python
SOURCE_RULES = {
    "EMAIL_":     ("personal",  "email",     "gmail-send",    False),
    "WHATSAPP_":  ("personal",  "whatsapp",  None,            True),
    "LINKEDIN_":  ("business",  "linkedin",  "linkedin-post", True),
    "TWITTER_":   ("business",  "twitter",   "social-post",   True),
    "FACEBOOK_":  ("business",  "facebook",  "social-post",   True),
    "INSTAGRAM_": ("business",  "instagram", "social-post",   True),
    "YOUTUBE_":   ("business",  "youtube",   "youtube",       False),
    "FILE_":      ("personal",  "file",      None,            True),
}
```

---

### MCP Servers (Node.js — `node index.js`)

All servers use `@modelcontextprotocol/sdk` with stdio transport.
All write operations respect `DRY_RUN=true` by default.

#### `linkedin-post`
Post, draft, and delete LinkedIn content.

| Tool | Description |
|------|-------------|
| `linkedin_get_profile` | Verify credentials, fetch name/URN |
| `linkedin_draft_post` | Save draft to `/Pending_Approval` |
| `linkedin_publish_post` | Publish after approval |
| `linkedin_delete_post` | Delete by post URN |

#### `gmail-send`
Send and manage Gmail messages.

| Tool | Description |
|------|-------------|
| `gmail_send_email` | Send email (HITL in DRY_RUN) |
| `gmail_draft_email` | Save to Gmail drafts |

#### `youtube`
Manage YouTube comments and channel data.

| Tool | Description |
|------|-------------|
| `youtube_reply_comment` | Reply to a comment |
| `youtube_get_video_stats` | Fetch video metrics |

#### `odoo-accounting`
Full accounts-receivable management via Odoo Community 19+ JSON-RPC.

| Tool | HITL | Description |
|------|------|-------------|
| `odoo_get_customer` | No | Look up customer by name → Odoo partner ID |
| `odoo_create_invoice` | Yes | Create customer invoice (draft or confirmed) |
| `odoo_post_payment` | Yes | Record inbound payment against an invoice |
| `odoo_get_balance` | No | Total outstanding AR for a customer |
| `odoo_list_invoices` | No | List invoices with state/date/customer filters |
| `odoo_accounting_summary` | No | Full AR snapshot for CEO briefings |

Auth flow: `POST /web/session/authenticate` → `session_id` cookie cached for 8 h,
auto-refreshed on expiry. All model operations use `POST /web/dataset/call_kw`.

#### `social-post`
Publish and analyse content across Facebook, Instagram, and Twitter/X.

| Tool | Platform | HITL | Description |
|------|----------|------|-------------|
| `facebook_post_message` | Facebook | Yes | Post text + optional link to a Page |
| `instagram_post_message` | Instagram | Yes | 2-step publish: container → media_publish |
| `twitter_post_tweet` | Twitter/X | Yes | Post tweet (reply/quote threading supported) |
| `social_generate_summary` | All 3 | No | Likes, comments, shares, reach metrics |
| `social_rate_limit_status` | All 3 | No | Current sliding-window usage per platform |

Rate-limit guards (in-process sliding window):

| Platform | Guard | Platform hard limit |
|----------|-------|---------------------|
| Facebook | 25 posts / hr | ~200 API calls/hr |
| Instagram | 24 posts / day | 50 content publishes/day |
| Twitter | 15 tweets / day | 17/day (free tier) |

---

### Orchestrator (`orchestrator.py`)

Master process that ties everything together.

```bash
uv run python orchestrator.py [--watchers gmail whatsapp twitter ...]
```

Responsibilities:
1. **ProcessManager** — starts each watcher as a subprocess, restarts on crash with configurable back-off
2. **Needs_Action poller** — picks up new `*.md` files, spawns `ralph_loop.py`
3. **HITL poller** — watches `/Approved`, executes approved actions via `claude -p`
4. **Scheduler** — runs cron-like tasks (Monday 07:00 CEO briefing, hourly dashboard update)

All 8 watchers registered:

```python
WATCHER_REGISTRY = {
    "file", "gmail", "whatsapp", "linkedin",
    "youtube", "twitter", "facebook", "cross_domain",
}
```

---

### Ralph Wiggum Loop (`ralph_loop.py`)

Keeps Claude iterating on a task until it outputs `TASK_COMPLETE`.

```bash
# Process all items in /Needs_Action
uv run python ralph_loop.py

# Process a single file
uv run python ralph_loop.py --file Needs_Action/TWITTER_MENTION_client_2026-02-25.md

# Custom prompt
uv run python ralph_loop.py --prompt "Generate this week's CEO briefing"

# Adjust max iterations
uv run python ralph_loop.py --max-iterations 15
```

Skill routing (filename prefix → skill file):

| Prefix(es) | Skill |
|------------|-------|
| `EMAIL_` | `email_triage.md` |
| `WHATSAPP_` | `whatsapp_reply.md` |
| `LINKEDIN_` | `linkedin_post.md` |
| `TWITTER_`, `FACEBOOK_`, `INSTAGRAM_`, `TW_TWEET_`, `FB_POST_`, `IG_POST_` | `social_media_post.md` |
| `YOUTUBE_` | `youtube_reply.md` |
| `ODOO_` | `accounting_audit.md` |
| `CROSS_`, `APPROVAL_`, `FILE_` | `task_planner.md` |

---

### Agent Skills (`/Skills/`)

Each skill defines: inputs, step-by-step instructions, output format, and rules.
Ralph loop reads the relevant skill file before building Claude's prompt.

| Skill | File | Purpose |
|-------|------|---------|
| Email Triage | `email_triage.md` | Classify and draft replies to emails |
| WhatsApp Reply | `whatsapp_reply.md` | Draft WhatsApp message responses |
| LinkedIn Post | `linkedin_post.md` | Draft and publish LinkedIn content |
| YouTube Reply | `youtube_reply.md` | Reply to YouTube comments |
| CEO Briefing | `ceo_briefing.md` | Monday morning executive summary |
| Invoice Generator | `invoice_generator.md` | Generate client invoices |
| Subscription Audit | `subscription_audit.md` | Monthly subscription review |
| Task Planner | `task_planner.md` | Break down any multi-step task |
| **Social Media Post** | `social_media_post.md` | Facebook / Instagram / Twitter publishing |
| **Accounting Audit** | `accounting_audit.md` | Odoo AR audit + invoice/payment workflow |

---

## Vault Structure

```
AI_Employee_Vault/
├── Inbox/                     Raw drops (unprocessed)
├── Needs_Action/              Watcher output — Claude's priority queue
├── Cross_Domain/
│   ├── Needs_Action/          Correlated cross-channel items
│   └── index.json             Correlation index (persisted across restarts)
├── Pending_Approval/          Sensitive drafts awaiting human sign-off
├── Approved/                  Human-approved → auto-executed by orchestrator
├── Rejected/                  Human-rejected → logged, no action taken
├── Done/                      Completed items (archive)
├── Plans/                     Claude-generated plan files with checkboxes
├── Active_Project/            Currently active project files
├── Accounting/                Invoice and payment records (Markdown)
├── Briefings/                 CEO briefing files
├── Logs/                      JSON audit logs (one file per day: YYYY-MM-DD.json)
├── Skills/                    10 agent skill definitions
│
├── mcp-servers/
│   ├── linkedin-post/         Node.js — LinkedIn REST API v2
│   ├── gmail-send/            Node.js — Gmail API v1
│   ├── youtube/               Node.js — YouTube Data API v3
│   ├── odoo-accounting/       Node.js — Odoo JSON-RPC
│   └── social-post/           Node.js — Graph API + Twitter API v2
│
├── base_watcher.py            BaseWatcher ABC
├── orchestrator.py            Master process
├── ralph_loop.py              Claude iteration loop
├── hitl_workflow.py           HITL execution helper
├── main.py                    Entry point
├── cross_domain_integration.py  Cross-channel correlation engine
├── filesystem_watcher.py
├── gmail_watcher.py
├── whatsapp_watcher.py
├── linkedin_watcher.py
├── youtube_watcher.py
├── twitter_watcher.py
├── facebook_watcher.py
│
├── Dashboard.md               Real-time status (auto-updated after every action)
├── Company_Handbook.md        Rules of engagement — approval thresholds + tone
├── CLAUDE.md                  Claude Code project instructions
├── .mcp.json                  MCP server registry (5 servers)
├── .env.example               All env var documentation
└── pyproject.toml             Python project (uv)
```

---

## Setup

### Prerequisites

| Requirement | Version | Notes |
|-------------|---------|-------|
| Python | 3.13+ | Managed via `uv` |
| Node.js | 18+ | Via `nvm` — use `nvm use 24` |
| Claude Code | 2.0+ | `npm i -g @anthropic-ai/claude-code` |
| Playwright browsers | latest | `uv run playwright install chromium` |
| Odoo Community | 17–19 | Optional — only needed for accounting tools |

### 1. Clone and install

```bash
git clone <repo>
cd AI_Employee_Vault

# Python dependencies
uv sync

# Playwright browsers (for WhatsApp + LinkedIn watchers)
uv run playwright install chromium

# Node.js MCP server dependencies (run once per server)
for dir in mcp-servers/*/; do
  echo "Installing $dir..."
  (cd "$dir" && npm install)
done
```

### 2. Configure environment

```bash
cp .env.example .env
# Open .env and fill in the credentials for the channels you want to use
```

Credentials by feature:

| Feature | Required env vars |
|---------|-------------------|
| Gmail watcher | `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, `GMAIL_CREDENTIALS_PATH` |
| WhatsApp watcher | `WHATSAPP_SESSION_PATH` |
| LinkedIn watcher | `LINKEDIN_SESSION_DIR` |
| LinkedIn post MCP | `LINKEDIN_ACCESS_TOKEN` |
| Twitter watcher + MCP | `TWITTER_BEARER_TOKEN`, `TWITTER_API_KEY`, `TWITTER_API_SECRET`, `TWITTER_ACCESS_TOKEN`, `TWITTER_ACCESS_TOKEN_SECRET` |
| Facebook + Instagram | `FACEBOOK_PAGE_ACCESS_TOKEN`, `FACEBOOK_PAGE_ID`, `INSTAGRAM_USER_ID` |
| Odoo accounting | `ODOO_URL`, `ODOO_DB`, `ODOO_USER`, `ODOO_PASS` |

### 3. Fill in `.mcp.json`

Add real credential values to the `env` blocks in `.mcp.json`, or set them in
your system environment and leave the values empty.

### 4. Run

```bash
# Start everything — all 8 watchers + HITL + scheduler
uv run python orchestrator.py

# Start specific watchers only
uv run python orchestrator.py --watchers gmail twitter facebook

# Run a single watcher in the foreground
uv run python twitter_watcher.py
uv run python facebook_watcher.py
uv run python cross_domain_integration.py

# Process a specific action file manually
uv run python ralph_loop.py --file Needs_Action/TWITTER_MENTION_client_2026-02-25.md

# On-demand CEO briefing
uv run python ralph_loop.py --prompt "Generate this week's CEO briefing"

# Dry run — see prompts without invoking Claude
uv run python ralph_loop.py --dry-run
```

---

## HITL (Human-in-the-Loop) Flow

```
Claude drafts sensitive action
          │
          ▼
/Pending_Approval/APPROVAL_*.md   ← Claude writes here automatically
          │
          │  Human opens the file in Obsidian and decides
          │
          ├── Move to /Approved/   → Orchestrator detects it
          │                           → Calls claude -p to execute
          │                           → Result archived to /Done
          │
          └── Move to /Rejected/   → Logged, no further action
```

**Always requires approval:**
- Any payment or financial transaction
- Emails to new or unknown contacts
- All social media posts and replies
- Deleting or modifying files outside the vault
- Any action above the thresholds in `Company_Handbook.md`

**Auto-approved (no human needed):**
- Reading any vault file
- Creating plan files
- Updating `Dashboard.md`
- Writing to `/Logs`

---

## Credential Setup Guides

### Twitter / X
1. Go to [developer.twitter.com](https://developer.twitter.com) → Create Project + App
2. Set app permissions to **Read + Write + Direct Messages**
3. Generate: API Key, API Secret, Access Token, Access Token Secret, Bearer Token
4. Free tier: 5 mention requests/15 min · 17 tweets/day
5. Add all 5 values to `.env` under `TWITTER_*`

### Facebook + Instagram
1. [developers.facebook.com](https://developers.facebook.com) → Create App
2. Add products: **Facebook Login**, **Pages API**, **Instagram Graph API**
3. Generate a long-lived Page Access Token in the Graph API Explorer:
   - Select your App + Page
   - Request permissions: `pages_manage_posts`, `pages_read_engagement`,
     `pages_messaging`, `instagram_basic`, `instagram_content_publish`,
     `instagram_manage_comments`
   - Long-lived tokens valid ~60 days; refresh before expiry
4. Find Page ID: `GET /me/accounts`
5. Find Instagram Business User ID: `GET /{page_id}?fields=instagram_business_account`

### Odoo Community 19+
```bash
# Quickstart with Docker
docker run -d -p 8069:8069 -e PASSWORD=admin --name odoo odoo:19

# Or use an existing Odoo 17/18/19 installation
```
1. Create your company database at `http://localhost:8069`
2. Install the **Invoicing** or **Accounting** app
3. Create a dedicated API user with the **Billing** access group
4. Set `ODOO_URL`, `ODOO_DB`, `ODOO_USER`, `ODOO_PASS` in `.env`

---

## Rate Limits Reference

| Platform | Watcher poll interval | MCP post guard | Platform hard limit |
|----------|-----------------------|----------------|---------------------|
| Gmail | On demand | — | 100 sends/day (free) |
| WhatsApp | 30 s | — | No official limit |
| LinkedIn | 90 s | — | ~100 API calls/day |
| Twitter | 150 s | 15 tweets/day | 5 mention req/15 min |
| Facebook | 180 s | 25 posts/hr | ~200 API calls/hr |
| Instagram | 180 s | 24 posts/day | 50 publishes/day |
| YouTube | 60 s | — | 10,000 units/day |

---

## Logging and Audit Trail

Every action is appended to a daily JSON log at `/Logs/YYYY-MM-DD.json`.

```json
{
  "timestamp": "2026-02-25T07:30:00.000Z",
  "watcher": "TwitterWatcher",
  "action_type": "twitter_mention_flagged",
  "details": {
    "author": "Jane Smith (@janesmith)",
    "type": "mention",
    "preview": "Hey @yourbrand can you help with...",
    "keywords": ["help"],
    "tweet_url": "https://twitter.com/i/web/status/12345",
    "action_file": "/Needs_Action/TWITTER_MENTION_Jane_Smith_2026-02-25.md"
  }
}
```

Financial actions additionally write Markdown records to `/Accounting/`
with YAML frontmatter, queryable from Obsidian using Dataview.

---

## Error Recovery

| Scenario | Behaviour |
|----------|-----------|
| Watcher subprocess crash | Orchestrator restarts it after `RESTART_DELAY` seconds |
| Twitter rate limit (`TooManyRequests`) | Watcher skips cycle, logs warning, retries next poll |
| Facebook token expired (code 190) | Actionable warning logged; human must refresh token |
| Facebook rate limit (codes 4/17/32/613) | Watcher skips cycle gracefully, no crash |
| Odoo session expired mid-request | MCP re-authenticates and retries the call once |
| Odoo server unreachable | Error returned to Claude; skill creates a retry /Needs_Action item |
| Instagram insights permission missing | Summary degrades gracefully (basic metrics still returned) |
| Cross-domain duplicate item | SHA-256 `item_hash` deduplication — never processed twice |
| Claude invocation timeout | ralph_loop retries up to `max_iterations`, then logs failure |

---

## Weekly CEO Briefing

Runs automatically every **Monday at 07:00** via the orchestrator scheduler.

Sections generated by `ceo_briefing.md` skill:
- **Executive Summary** — top 3 priorities for the week
- **Needs Action** — items in queue awaiting Claude's attention
- **Pending Approval** — items awaiting human review
- **Accounting Summary** — AR snapshot via `odoo_accounting_summary` (last 7 days)
- **Social Media Summary** — engagement across Facebook, Instagram, Twitter
- **Completed This Week** — items moved to /Done
- **Overdue Follow-ups** — flagged for immediate attention

Output: `/Briefings/YYYY-MM-DD_Monday_Briefing.md`

---

## Project Information

**Hackathon**: Panaversity Hackathon 0 — Building Autonomous FTEs in 2026

**Tech stack**:

| Layer | Technology |
|-------|-----------|
| Reasoning engine | Claude Code (Sonnet 4.6) via `claude -p` |
| Watcher agents | Python 3.13, `uv`, `watchdog`, `tweepy`, `facebook-sdk`, `playwright` |
| MCP servers | Node.js 24, `@modelcontextprotocol/sdk`, `twitter-api-v2`, `zod` |
| Memory / workspace | Obsidian vault (Markdown + YAML frontmatter) |
| Accounting backend | Odoo Community 19+ (JSON-RPC) |
| OS / platform | WSL2 on Windows (NTFS at `/mnt/d/`) |

**WSL2 note**: `inotify` does not work on NTFS mounts (`/mnt/d/`).
All file-based watchers use `watchdog.observers.polling.PollingObserver`.
#   A I _ E m p l o y e e _ V a u l t _ H e c k a t h o n _ 0  
 