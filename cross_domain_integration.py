#!/usr/bin/env python3
"""
cross_domain_integration.py — Gold Tier: Cross-Domain Integration

Bridges personal (Gmail, WhatsApp) and business (LinkedIn, Twitter/X,
Facebook, Instagram, YouTube) domains into a unified action pipeline.

Responsibilities:
  1. Poll /Needs_Action for items from all watchers (EMAIL_, WHATSAPP_,
     LINKEDIN_, TWITTER_, FACEBOOK_, INSTAGRAM_, YOUTUBE_, FILE_)
  2. Classify each item as PERSONAL, BUSINESS, or CROSS domain
  3. Detect cross-domain correlations — same contact appearing in both a
     personal source (email/WhatsApp) and a business source (LinkedIn etc.)
     within the configurable correlation window
  4. Create unified /Cross_Domain/Needs_Action/ files with:
       - Full domain classification metadata
       - MCP server routing instructions for Claude Code
       - Cross-domain correlation context
  5. Route sensitive items to /Pending_Approval (HITL safeguard)
  6. Route auto-approved items to /Cross_Domain/Needs_Action
     (picked up by Orchestrator → Claude Code → MCP server)
  7. Full structured audit logging to /Logs/YYYY-MM-DD.json

Source → Domain → MCP Route mapping:
  EMAIL_*      → PERSONAL  → gmail-send MCP          (auto-approved)
  WHATSAPP_*   → PERSONAL  → pending_approval         (HITL required)
  LINKEDIN_*   → BUSINESS  → linkedin-post MCP        (HITL required)
  TWITTER_*    → BUSINESS  → twitter-post MCP         (HITL required)
  FACEBOOK_*   → BUSINESS  → social-post MCP          (HITL required)
  INSTAGRAM_*  → BUSINESS  → social-post MCP          (HITL required)
  YOUTUBE_*    → BUSINESS  → youtube MCP              (auto-approved)
  FILE_*       → PERSONAL  → pending_approval         (HITL required)

Cross-domain correlation:
  When the same sender appears in both PERSONAL and BUSINESS sources
  within CORRELATION_WINDOW_HOURS, a CROSS_ file is created linking both
  items so Claude has full context for a unified, coordinated response.

Usage:
    uv run python cross_domain_integration.py
    uv run python cross_domain_integration.py --dry-run
    uv run python cross_domain_integration.py --poll-interval 60
    uv run python cross_domain_integration.py --correlation-window 48
    uv run python cross_domain_integration.py --no-routing   # classify + log only

Security note:
    Tokens in .mcp.json must be moved to .env and referenced via env vars.
    Never commit live OAuth credentials to git.

Environment variables (all optional, have defaults):
    VAULT_PATH                   Path to Obsidian vault
    DRY_RUN                      true/false (default: true)
    CROSS_DOMAIN_POLL_INTERVAL   Seconds between scans (default: 30)
    CROSS_DOMAIN_CORRELATION_WINDOW  Hours to look back (default: 24)
"""

import os
import re
import json
import time
import signal
import hashlib
import argparse
from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional

from dotenv import load_dotenv

from base_watcher import BaseWatcher

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

VAULT_PATH = Path(
    os.getenv("VAULT_PATH", "/mnt/d/AI_Employee_Vault/AI_Employee_Vault")
)
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
POLL_INTERVAL = int(os.getenv("CROSS_DOMAIN_POLL_INTERVAL", "30"))
CORRELATION_WINDOW_HOURS = int(os.getenv("CROSS_DOMAIN_CORRELATION_WINDOW", "24"))

# ── Directory Layout ──────────────────────────────────────────────────────────

NEEDS_ACTION      = VAULT_PATH / "Needs_Action"
PENDING_APPROVAL  = VAULT_PATH / "Pending_Approval"
DONE              = VAULT_PATH / "Done"
LOGS              = VAULT_PATH / "Logs"
CROSS_DOMAIN_DIR  = VAULT_PATH / "Cross_Domain"
CROSS_NEEDS_ACTION = CROSS_DOMAIN_DIR / "Needs_Action"
CROSS_DONE         = CROSS_DOMAIN_DIR / "Done"
CROSS_INDEX        = CROSS_DOMAIN_DIR / "index.json"

# ── Source Classification Rules ───────────────────────────────────────────────
# Maps filename prefix → (domain, source_type, mcp_server, requires_approval)
# mcp_server=None means no direct MCP → routed to /Pending_Approval instead.

SOURCE_RULES: dict[str, tuple[str, str, Optional[str], bool]] = {
    "EMAIL_":     ("personal",  "email",     "gmail-send",    False),
    "WHATSAPP_":  ("personal",  "whatsapp",  None,            True),
    "LINKEDIN_":  ("business",  "linkedin",  "linkedin-post", True),
    "TWITTER_":   ("business",  "twitter",   "twitter-post",  True),
    "FACEBOOK_":  ("business",  "facebook",  "social-post",   True),
    "INSTAGRAM_": ("business",  "instagram", "social-post",   True),
    "YOUTUBE_":   ("business",  "youtube",   "youtube",       False),
    "FILE_":      ("personal",  "file",      None,            True),
    # CROSS_ prefix = our own output; never re-process
    "CROSS_":     ("cross",     "cross",     None,            False),
}

# Keywords used to match cross-domain topics for correlation
CORRELATION_TOPICS: list[str] = [
    "invoice", "payment", "project", "proposal", "partnership",
    "meeting", "deadline", "contract", "hiring", "collaboration",
    "pricing", "budget", "opportunity", "review", "feedback",
    "urgent", "asap", "offer", "interview", "demo", "quote",
]

# MCP action verbs per source type (used in routing instructions for Claude Code)
MCP_ACTIONS: dict[str, str] = {
    "email":     "send_reply",
    "linkedin":  "linkedin_publish_post",
    "twitter":   "twitter_post_tweet",
    "facebook":  "social_post_to_page",
    "instagram": "social_post_to_instagram",
    "youtube":   "youtube_reply_to_comment",
    "whatsapp":  "whatsapp_send_message",
}


# ── Domain Item ───────────────────────────────────────────────────────────────

@dataclass
class DomainItem:
    """A single classified action item from any watcher source."""

    source_file: Path
    filename: str
    domain: str               # "personal" | "business" | "cross"
    source_type: str          # "email" | "whatsapp" | "linkedin" | ...
    mcp_route: Optional[str]  # MCP server name, or None → Pending_Approval
    requires_approval: bool
    sender: str
    content_preview: str
    priority: str
    detected_at: datetime
    frontmatter: dict = field(default_factory=dict)
    matched_topics: list[str] = field(default_factory=list)
    correlation_ids: list[str] = field(default_factory=list)
    item_hash: str = ""

    def __post_init__(self):
        if not self.item_hash:
            raw = f"{self.filename}{self.sender}{self.content_preview[:50]}"
            self.item_hash = hashlib.sha256(raw.encode()).hexdigest()[:16]


# ── Correlation Index ─────────────────────────────────────────────────────────

class CorrelationIndex:
    """
    Persisted cross-domain event log for correlation detection.

    Stored at /Cross_Domain/index.json. Entries outside the correlation
    window are evicted automatically on every load and add() call.
    """

    def __init__(self, index_path: Path, window_hours: int):
        self.path = index_path
        self.window = timedelta(hours=window_hours)
        self.entries: list[dict] = []
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.entries = data if isinstance(data, list) else []
            except (json.JSONDecodeError, ValueError, OSError):
                self.entries = []
        self._evict_old()

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.write_text(
                json.dumps(self.entries, indent=2, default=str),
                encoding="utf-8",
            )
        except OSError as exc:
            # Non-fatal: log to stderr, correlation still works in-memory
            import sys
            print(f"[CorrelationIndex] Warning: could not save index: {exc}", file=sys.stderr)

    def _evict_old(self):
        cutoff = (datetime.now(timezone.utc) - self.window).isoformat()
        before = len(self.entries)
        self.entries = [e for e in self.entries if e.get("detected_at", "") >= cutoff]
        if len(self.entries) < before:
            self._save()

    def add(self, item: DomainItem):
        """Add a classified item to the index and persist."""
        self.entries.append({
            "item_hash":   item.item_hash,
            "filename":    item.filename,
            "domain":      item.domain,
            "source_type": item.source_type,
            "sender":      item.sender,
            "sender_norm": _normalize_sender(item.sender),
            "topics":      item.matched_topics,
            "detected_at": item.detected_at.isoformat(),
        })
        self._save()

    def find_correlations(self, item: DomainItem) -> list[dict]:
        """
        Return index entries that correlate with item across the opposite domain.

        Correlation criteria (either is sufficient):
          - Same normalized sender name in a PERSONAL ↔ BUSINESS pairing
          - Overlapping topic keywords across opposite domains
        """
        self._evict_old()
        correlations: list[dict] = []
        sender_norm = _normalize_sender(item.sender)
        item_topics = set(item.matched_topics)

        for entry in self.entries:
            # Only link personal ↔ business (not same-domain)
            if entry.get("domain") == item.domain:
                continue
            if entry.get("source_type") == "cross":
                continue

            # Sender match: both non-empty and one contains the other
            entry_sender_norm = entry.get("sender_norm", "")
            same_sender = bool(
                sender_norm
                and entry_sender_norm
                and (
                    sender_norm in entry_sender_norm
                    or entry_sender_norm in sender_norm
                )
            )

            shared_topics = item_topics & set(entry.get("topics", []))

            if same_sender or shared_topics:
                correlations.append({
                    **entry,
                    "match_reason": "sender" if same_sender else "topics",
                    "shared_topics": sorted(shared_topics),
                })

        return correlations


# ── Cross-Domain Integrator ───────────────────────────────────────────────────

class CrossDomainIntegrator(BaseWatcher):
    """
    Monitors /Needs_Action for items from all domain watchers, classifies
    them, detects cross-domain correlations, and routes each item to either:
      - /Cross_Domain/Needs_Action/  (auto-approved; picked up by Orchestrator)
      - /Pending_Approval/           (HITL required; human moves to /Approved/)

    Extends BaseWatcher:
      check_for_updates() → returns list[Path] of new .md files in /Needs_Action
      create_action_file(path) → classifies + correlates + writes output
    """

    def __init__(
        self,
        vault_path: str,
        poll_interval: int = POLL_INTERVAL,
        dry_run: bool = DRY_RUN,
        correlation_window_hours: int = CORRELATION_WINDOW_HOURS,
    ):
        super().__init__(vault_path, check_interval=poll_interval)
        self.dry_run = dry_run

        # Input: standard Needs_Action (all watchers write here)
        self.source_dir = self.needs_action                                 # /Needs_Action
        # Output dirs
        self.output_dir       = self.vault_path / "Cross_Domain" / "Needs_Action"
        self.cross_done       = self.vault_path / "Cross_Domain" / "Done"
        self.pending_approval = self.vault_path / "Pending_Approval"

        for d in [self.output_dir, self.cross_done, self.pending_approval]:
            d.mkdir(parents=True, exist_ok=True)

        self.index = CorrelationIndex(
            CROSS_INDEX, window_hours=correlation_window_hours
        )

        # Set of filenames already dispatched this session (dedup)
        self.processed_files: set[str] = set()

        self.logger.info(
            f"CrossDomainIntegrator ready | dry_run={dry_run} | "
            f"poll={poll_interval}s | correlation_window={correlation_window_hours}h"
        )

    # ── BaseWatcher interface ──────────────────────────────────────────────────

    def check_for_updates(self) -> list[Path]:
        """
        Scan /Needs_Action for unprocessed .md files.
        Skips CROSS_ prefixed files (our own output) to avoid loops.
        Returns files sorted oldest-first.
        """
        if not self.source_dir.exists():
            return []

        new_files: list[Path] = []
        try:
            candidates = sorted(
                self.source_dir.iterdir(),
                key=lambda f: f.stat().st_mtime,
            )
        except OSError as exc:
            self.logger.error(f"Cannot scan {self.source_dir}: {exc}")
            return []

        for f in candidates:
            if not f.is_file() or f.suffix != ".md":
                continue
            if f.name.upper().startswith("CROSS_"):
                continue  # skip our own output
            if f.name in self.processed_files:
                continue
            new_files.append(f)

        return new_files

    def create_action_file(self, source_path: Path) -> Path:
        """
        Full pipeline for a single action file:
          classify → detect correlations → write output → update index → log.

        Returns the path of the created output file.
        Returns empty Path() on skip or error.
        """
        filename = source_path.name

        # ── 1. Classify ───────────────────────────────────────────────────────
        try:
            item = self._classify(source_path)
        except Exception as exc:
            self.logger.error(f"Classification failed for {filename}: {exc}", exc_info=True)
            self.processed_files.add(filename)
            return Path()

        if item is None:
            self.logger.debug(f"Unrecognised prefix — skipping: {filename}")
            self.processed_files.add(filename)
            return Path()

        if item.domain == "cross":
            # Our own output re-appeared — skip silently
            self.processed_files.add(filename)
            return Path()

        self.logger.info(
            f"[{item.domain.upper():8}] {filename}"
            f"  source={item.source_type}"
            f"  mcp={item.mcp_route or 'pending_approval'}"
            f"  sender={item.sender!r}"
            f"  topics={item.matched_topics or ['none']}"
        )

        # ── 2. Detect correlations ────────────────────────────────────────────
        try:
            correlations = self.index.find_correlations(item)
        except Exception as exc:
            self.logger.warning(f"Correlation detection error for {filename}: {exc}")
            correlations = []

        if correlations:
            match_reasons = {c["match_reason"] for c in correlations}
            self.logger.info(
                f"  CORRELATION: {len(correlations)} item(s) linked "
                f"via {match_reasons}"
            )
            item.correlation_ids = [c["item_hash"] for c in correlations]

        # ── 3. Route + write output ───────────────────────────────────────────
        output_path: Path

        if self.dry_run:
            dest = "Pending_Approval" if item.requires_approval else "Cross_Domain/Needs_Action"
            self.logger.info(f"  [DRY RUN] Would route → /{dest}/")
            output_path = Path(f"[DRY RUN] {filename}")
        elif item.requires_approval:
            output_path = self._write_pending_approval(item, correlations)
        else:
            output_path = self._write_cross_domain_file(item, correlations)

        # ── 4. Update correlation index ───────────────────────────────────────
        try:
            self.index.add(item)
        except Exception as exc:
            self.logger.warning(f"Failed to update correlation index: {exc}")

        # ── 5. Mark as processed ──────────────────────────────────────────────
        self.processed_files.add(filename)

        # ── 6. Audit log ──────────────────────────────────────────────────────
        self._audit_log(item, correlations, str(output_path))

        return output_path

    # ── Classification ────────────────────────────────────────────────────────

    def _classify(self, source_path: Path) -> Optional[DomainItem]:
        """
        Determine domain/source_type/mcp_route from the filename prefix,
        then extract sender + preview from YAML frontmatter.
        Returns None for unrecognised prefixes.
        """
        filename = source_path.name
        rule = self._match_prefix(filename)
        if rule is None:
            return None

        domain, source_type, mcp_route, requires_approval = rule

        fm = _parse_frontmatter(source_path)
        sender  = (fm.get("from") or fm.get("sender") or "Unknown").strip('"').strip("'")
        preview = (fm.get("preview") or fm.get("snippet") or "").strip('"').strip("'")[:300]
        priority = fm.get("priority", "medium").strip('"').strip("'")

        # Scan full file content for topic keywords
        matched_topics: list[str] = []
        try:
            text_lower = source_path.read_text(encoding="utf-8", errors="replace").lower()
            matched_topics = [t for t in CORRELATION_TOPICS if t in text_lower]
        except OSError as exc:
            self.logger.warning(f"Could not read {filename} for topic scan: {exc}")

        return DomainItem(
            source_file=source_path,
            filename=filename,
            domain=domain,
            source_type=source_type,
            mcp_route=mcp_route,
            requires_approval=requires_approval,
            sender=sender,
            content_preview=preview,
            priority=priority,
            detected_at=datetime.now(timezone.utc),
            frontmatter=fm,
            matched_topics=matched_topics,
        )

    @staticmethod
    def _match_prefix(filename: str) -> Optional[tuple]:
        """Return the SOURCE_RULES entry matching the filename prefix, or None."""
        upper = filename.upper()
        for prefix, rule in SOURCE_RULES.items():
            if upper.startswith(prefix):
                return rule
        return None

    # ── Output Writers ────────────────────────────────────────────────────────

    def _write_cross_domain_file(
        self, item: DomainItem, correlations: list[dict]
    ) -> Path:
        """
        Write a unified action file to /Cross_Domain/Needs_Action/.
        This file is picked up by the Orchestrator → Claude Code → MCP.
        """
        now = item.detected_at
        ts = now.strftime("%Y-%m-%d_%H%M%S")
        safe_sender = re.sub(r"[^\w-]", "_", item.sender)[:30].strip("_")
        filename = (
            f"CROSS_{item.domain.upper()}_{item.source_type.upper()}"
            f"_{safe_sender}_{ts}.md"
        )
        output_path = self.output_dir / filename

        topics_str   = ", ".join(item.matched_topics) if item.matched_topics else "none"
        corr_block   = _build_correlation_block(correlations)
        mcp_block    = _build_mcp_block(item)
        corr_flag    = str(bool(correlations)).lower()

        content = f"""---
type: cross_domain_item
domain: {item.domain}
source_type: {item.source_type}
source_file: "{item.source_file}"
sender: "{item.sender}"
mcp_route: {item.mcp_route or "none"}
requires_approval: false
correlated: {corr_flag}
correlation_count: {len(correlations)}
correlation_ids: [{", ".join(item.correlation_ids)}]
matched_topics: [{topics_str}]
priority: {item.priority}
item_hash: {item.item_hash}
detected: {now.isoformat()}
status: pending
---

# Cross-Domain [{item.domain.upper()}]: {item.source_type.capitalize()} from {item.sender}

## Classification
| Field | Value |
|-------|-------|
| **Domain** | {item.domain.capitalize()} |
| **Source** | {item.source_type.capitalize()} |
| **MCP Route** | `{item.mcp_route}` |
| **Priority** | {item.priority.upper()} |
| **Topics Detected** | {topics_str} |
| **Cross-Domain Correlation** | {"Yes — " + str(len(correlations)) + " item(s) linked" if correlations else "No"} |

## Message Preview
> {item.content_preview or "_(no preview — see original file)_"}

## Original Action File
`{item.source_file}`

{corr_block}
{mcp_block}

## Suggested Actions
- [ ] Review original file: `{item.source_file}`
- [ ] Execute response via `{item.mcp_route}` MCP server
{"- [ ] **CROSS-DOMAIN**: Coordinate response with correlated items listed above" if correlations else ""}
- [ ] Move original file to `/Done/` after handling
- [ ] Update `Dashboard.md`

---
*Generated by CrossDomainIntegrator — {now.strftime("%Y-%m-%d %H:%M:%S")} UTC*
"""
        try:
            output_path.write_text(content, encoding="utf-8")
            self.logger.info(f"  → Cross_Domain/Needs_Action/{filename}")
        except OSError as exc:
            self.logger.error(f"Failed to write {filename}: {exc}")
            return Path()

        return output_path

    def _write_pending_approval(
        self, item: DomainItem, correlations: list[dict]
    ) -> Path:
        """
        Write an approval request to /Pending_Approval/ for sensitive items.
        Human moves the file to /Approved/ to allow the Orchestrator to execute.
        """
        now = item.detected_at
        ts = now.strftime("%Y-%m-%d_%H%M%S")
        safe_sender = re.sub(r"[^\w-]", "_", item.sender)[:30].strip("_")
        filename = (
            f"APPROVAL_{item.source_type.upper()}_{safe_sender}_{ts}.md"
        )
        output_path = self.pending_approval / filename

        topics_str = ", ".join(item.matched_topics) if item.matched_topics else "none"
        corr_block = _build_correlation_block(correlations)
        expires    = (now + timedelta(hours=48)).isoformat()

        content = f"""---
type: approval_request
action: cross_domain_response
domain: {item.domain}
source_type: {item.source_type}
source_file: "{item.source_file}"
sender: "{item.sender}"
mcp_route: {item.mcp_route or "none"}
correlated: {str(bool(correlations)).lower()}
correlation_count: {len(correlations)}
correlation_ids: [{", ".join(item.correlation_ids)}]
matched_topics: [{topics_str}]
priority: {item.priority}
item_hash: {item.item_hash}
created: {now.isoformat()}
expires: {expires}
status: pending
---

# Approval Required: {item.source_type.capitalize()} from {item.sender}

## Why This Needs Approval
A **{item.domain}** message from **{item.sender}** via **{item.source_type}**
requires human review before the AI Employee responds.
{"This item is linked to a cross-domain correlation — see context below." if correlations else ""}

## Message Preview
> {item.content_preview or "_(no preview — see original file)_"}

## Classification
| Field | Value |
|-------|-------|
| **Domain** | {item.domain.capitalize()} |
| **Source** | {item.source_type.capitalize()} |
| **MCP Route** | `{item.mcp_route or "pending — no MCP configured"}` |
| **Priority** | {item.priority.upper()} |
| **Topics** | {topics_str} |
| **Expires** | {expires[:19].replace("T", " ")} UTC |

## Original File
`{item.source_file}`

{corr_block}

## Actions
| Action | How |
|--------|-----|
| **Approve** | Move this file to `/Approved/` |
| **Reject** | Move this file to `/Rejected/` |
| **Change MCP Route** | Edit `mcp_route:` above, then move to `/Approved/` |

---
*HITL gate — generated by CrossDomainIntegrator — {now.strftime("%Y-%m-%d %H:%M:%S")} UTC*
"""
        try:
            output_path.write_text(content, encoding="utf-8")
            self.logger.info(f"  → Pending_Approval/{filename}")
        except OSError as exc:
            self.logger.error(f"Failed to write approval file {filename}: {exc}")
            return Path()

        return output_path

    # ── Audit Logging ─────────────────────────────────────────────────────────

    def _audit_log(
        self, item: DomainItem, correlations: list[dict], output_path: str
    ):
        """
        Append a structured entry to today's /Logs/YYYY-MM-DD.json.
        Matches the schema used by BaseWatcher.log_action() and Orchestrator.
        """
        self.log_action(
            "cross_domain_routed",
            {
                "source_file":       item.filename,
                "domain":            item.domain,
                "source_type":       item.source_type,
                "sender":            item.sender,
                "mcp_route":         item.mcp_route,
                "requires_approval": item.requires_approval,
                "priority":          item.priority,
                "matched_topics":    item.matched_topics,
                "correlations":      len(correlations),
                "correlation_ids":   item.correlation_ids,
                "output_file":       output_path,
                "dry_run":           self.dry_run,
            },
        )


# ── Markdown Helpers ──────────────────────────────────────────────────────────

def _build_correlation_block(correlations: list[dict]) -> str:
    """Build the cross-domain correlation section for output files."""
    if not correlations:
        return ""

    rows = ""
    for c in correlations:
        detected = c.get("detected_at", "")[:19].replace("T", " ")
        shared   = ", ".join(c.get("shared_topics", [])) or "—"
        rows += (
            f"| {c.get('source_type', '?').capitalize()}"
            f" | {c.get('sender', '?')}"
            f" | {c.get('match_reason', '?')}"
            f" | {shared}"
            f" | {detected} |\n"
        )

    return f"""## Cross-Domain Correlation
**{len(correlations)} linked item(s) detected from the opposite domain.**

| Source | Sender | Match Reason | Shared Topics | Detected |
|--------|--------|-------------|--------------|---------|
{rows}
> **Recommendation**: Coordinate your response across both domains for a consistent message.
"""


def _build_mcp_block(item: DomainItem) -> str:
    """
    Build the MCP routing instructions block embedded in output files.
    Claude Code reads this section to know which MCP tool to invoke.
    """
    if not item.mcp_route:
        return ""

    action = MCP_ACTIONS.get(item.source_type, "respond")

    return f"""## MCP Routing Instructions
<!-- Claude Code: call the MCP server below to execute the response -->

| Field | Value |
|-------|-------|
| **MCP Server** | `{item.mcp_route}` |
| **Action** | `{action}` |
| **Sender** | `{item.sender}` |
| **Priority** | `{item.priority}` |
| **Source File** | `{item.source_file.name}` |

**Execution steps for Claude Code**:
1. Read Company_Handbook.md and the relevant skill in /Skills/
2. Read the original file at `{item.source_file}`
3. Draft a response appropriate for `{item.source_type}`
4. Invoke the `{item.mcp_route}` MCP server → `{action}`
5. Log the result to `/Logs/`
6. Move the original file to `/Done/` with a timestamp prefix
7. Update `Dashboard.md`
"""


def _parse_frontmatter(path: Path) -> dict:
    """
    Extract key: value pairs from YAML-like frontmatter (between --- delimiters).
    Returns an empty dict if the file has no frontmatter or cannot be read.
    No external YAML dependency — simple line-by-line parse sufficient for our schema.
    """
    fm: dict = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return fm

    if not text.startswith("---"):
        return fm

    # Find the closing --- on its own line
    end = text.find("\n---", 3)
    if end == -1:
        return fm

    block = text[3:end].strip()
    for line in block.splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        fm[key.strip()] = val.strip()

    return fm


def _normalize_sender(name: str) -> str:
    """
    Normalize a sender string for fuzzy matching.
    Strips email syntax, lowercases, collapses whitespace, removes domain noise.
    """
    # Remove email address syntax: First Last <first@domain.com> → First Last
    name = re.sub(r"<[^>]+>", "", name)
    # Remove punctuation used in email headers
    name = re.sub(r"[\"'<>@]", " ", name).lower()
    # Remove common filler domain words that appear in extracted names
    name = re.sub(
        r"\b(gmail|yahoo|outlook|hotmail|company|corp|ltd|inc|llc|via)\b",
        " ",
        name,
    )
    # Collapse whitespace
    name = re.sub(r"\s+", " ", name).strip()
    return name


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Cross-Domain Integrator — unified personal + business action routing"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  uv run python cross_domain_integration.py               # normal mode
  uv run python cross_domain_integration.py --dry-run     # safe: classify + log only
  uv run python cross_domain_integration.py --poll-interval 60 --correlation-window 48
""",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Classify and log without writing any output files (safe for testing)",
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=POLL_INTERVAL,
        help=f"Seconds between /Needs_Action scans (default: {POLL_INTERVAL})",
    )
    parser.add_argument(
        "--correlation-window",
        type=int,
        default=CORRELATION_WINDOW_HOURS,
        help=f"Hours to look back for cross-domain correlations (default: {CORRELATION_WINDOW_HOURS})",
    )
    parser.add_argument(
        "--no-routing",
        action="store_true",
        help="Alias for --dry-run: classify + log, skip writing output files",
    )
    return parser.parse_args()


# ── Entry Point ───────────────────────────────────────────────────────────────

def main():
    global DRY_RUN, POLL_INTERVAL, CORRELATION_WINDOW_HOURS

    args = parse_args()
    if args.dry_run or args.no_routing:
        DRY_RUN = True
    POLL_INTERVAL = args.poll_interval
    CORRELATION_WINDOW_HOURS = args.correlation_window

    integrator = CrossDomainIntegrator(
        vault_path=str(VAULT_PATH),
        poll_interval=POLL_INTERVAL,
        dry_run=DRY_RUN,
        correlation_window_hours=CORRELATION_WINDOW_HOURS,
    )

    # Graceful shutdown on SIGINT / SIGTERM
    def _handle_signal(signum, frame):
        integrator.logger.info(f"Signal {signum} received — shutting down gracefully.")
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    integrator.logger.info("=" * 62)
    integrator.logger.info("  Personal AI Employee — Cross-Domain Integrator")
    integrator.logger.info(f"  Vault      : {VAULT_PATH}")
    integrator.logger.info(f"  Source     : {NEEDS_ACTION}")
    integrator.logger.info(f"  Output     : {CROSS_NEEDS_ACTION}")
    integrator.logger.info(f"  Approval   : {PENDING_APPROVAL}")
    integrator.logger.info(f"  Dry Run    : {DRY_RUN}")
    integrator.logger.info(f"  Poll       : {POLL_INTERVAL}s")
    integrator.logger.info(f"  Corr Window: {CORRELATION_WINDOW_HOURS}h")
    integrator.logger.info("=" * 62)

    # Delegate to BaseWatcher.run() — calls check_for_updates() + create_action_file()
    integrator.run()


if __name__ == "__main__":
    main()
