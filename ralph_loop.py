"""Ralph Wiggum Loop — Persistent multi-step task driver for the AI Employee.

Named after the Claude Code "Ralph Wiggum" Stop hook pattern — it keeps Claude
working on a task until it declares completion or all iterations are exhausted.

Architecture Overview
---------------------
The loop has four layers:

  ┌─────────────────────────────────────────────────────────────────┐
  │  Task Discovery  (process_all_needs_action)                      │
  │    Scans /Needs_Action AND /Cross_Domain/Needs_Action            │
  │    Sorts by: priority tier (critical→low) then mtime (oldest)   │
  └─────────────────────────┬───────────────────────────────────────┘
                             │ one file at a time
  ┌──────────────────────────▼──────────────────────────────────────┐
  │  Context Parsing  (_parse_task_context)                          │
  │    Reads YAML frontmatter → TaskContext                          │
  │    Determines: source_type, mcp_route, priority, skill, iters   │
  └──────────────────────────┬──────────────────────────────────────┘
                             │
  ┌──────────────────────────▼──────────────────────────────────────┐
  │  Ralph Loop Core  (ralph_loop)                                   │
  │    iter 1:   build_initial_prompt(ctx)  → invoke_claude          │
  │    iter 2–N: build_continuation_prompt(ctx) → invoke_claude      │
  │    after each: CompletionChecker.is_complete()?                  │
  └────────────────┬──────────────────────────┬─────────────────────┘
                   │ completed                 │ max iterations hit
          ┌────────▼────────┐        ┌─────────▼────────────────────┐
          │  _move_to_done  │        │  _escalate_stalled_task       │
          │  (no-op if      │        │  writes RALPH_STALLED_*.md    │
          │  Claude already │        │  to /Pending_Approval/ so     │
          │  moved the file)│        │  human can decide next step   │
          └─────────────────┘        └──────────────────────────────┘

Completion Modes
----------------
"promise"    (default)
    Claude outputs <promise>TASK_COMPLETE</promise> in its response text.
    Simple and reliable for single-file tasks with predictable output.

"done_file"  (recommended for cross-domain items)
    The action file has disappeared from its source directory — i.e. Claude
    already executed the "move to /Done/" step as part of the task.
    This is the canonical signal for cross-domain tasks because the MCP
    routing instructions explicitly instruct Claude to move the file.

"file_glob"  (legacy / custom)
    A file matching a specific glob pattern now exists (e.g., "/Plans/PLAN_*.md").
    Useful for tasks that produce a known output artifact.

You can also pass --combine-done-file to check done_file as a secondary signal
alongside the primary mode, catching edge cases where Claude moved the file but
forgot to emit the promise tag.

Per-Source-Type Iteration Limits
---------------------------------
Different task types have different complexity and therefore need different
iteration budgets:

    email, twitter, youtube → 4–5 iters   (single-step reply + send)
    linkedin, whatsapp      → 4–6 iters   (draft + HITL potential)
    file, odoo              → 6–8 iters   (analysis + multi-sub-step)
    cross                   → 10 iters    (read + draft + MCP + coordinate)

The global --max-iterations CLI flag acts as a hard ceiling that overrides these
per-type caps. Use it to override for a specific one-off run.

Cross-Domain Integration
------------------------
Files written by CrossDomainIntegrator have CROSS_ prefix and carry:
  - mcp_route:           which MCP server to invoke (e.g., "linkedin-post")
  - correlation_ids:     SHA hashes of correlated items from the other domain
  - matched_topics:      business keywords that triggered the correlation
  - requires_approval:   false (HITL was already handled by the integrator)

The ralph_loop injects all of this into both the initial and continuation
prompts so Claude has full context for a coordinated, multi-channel response.

Stalled Task Escalation
-----------------------
When max_iterations is reached without completion:
  1. A RALPH_STALLED_*.md is written to /Pending_Approval/ with context and
     options for the human operator.
  2. A hidden .ralph_<stem>.json sidecar is updated with the total attempt count
     so that if the operator retries, the history is visible.
  3. The original action file stays in /Needs_Action for manual handling.

Usage
-----
    # Process all items (Needs_Action + Cross_Domain/Needs_Action)
    uv run python ralph_loop.py

    # Process a single file
    uv run python ralph_loop.py --file /path/to/Needs_Action/EMAIL_xxx.md

    # Custom prompt
    uv run python ralph_loop.py --prompt "Audit all subscriptions in /Accounting"

    # Cap iterations
    uv run python ralph_loop.py --max-iterations 15

    # done_file mode — complete when the file is moved to /Done/
    uv run python ralph_loop.py --completion-mode done_file

    # Promise + done_file secondary check (safest for production)
    uv run python ralph_loop.py --combine-done-file

    # Process only cross-domain items
    uv run python ralph_loop.py --source-filter cross

    # Skip /Cross_Domain/Needs_Action (standard items only)
    uv run python ralph_loop.py --no-cross-domain

    # Dry run — build prompts but don't invoke Claude
    uv run python ralph_loop.py --dry-run
"""

import os
import re
import sys
import json
import glob
import shutil
import argparse
import subprocess
import logging
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

VAULT_PATH = Path(
    os.getenv("VAULT_PATH", "/mnt/d/AI_Employee_Vault/AI_Employee_Vault")
)
CLAUDE_CMD         = os.getenv("CLAUDE_CMD", "claude")
MAX_ITERATIONS     = int(os.getenv("RALPH_MAX_ITERATIONS", "10"))
CLAUDE_TIMEOUT     = int(os.getenv("RALPH_CLAUDE_TIMEOUT", "180"))  # seconds per call
COMPLETION_PROMISE = "TASK_COMPLETE"
DRY_RUN            = os.getenv("DRY_RUN", "false").lower() == "true"

# Vault directories
NEEDS_ACTION      = VAULT_PATH / "Needs_Action"
CROSS_DOMAIN_DIR  = VAULT_PATH / "Cross_Domain"
CROSS_NEEDS_ACTION = CROSS_DOMAIN_DIR / "Needs_Action"  # output of CrossDomainIntegrator
CROSS_DONE        = CROSS_DOMAIN_DIR / "Done"
PLANS             = VAULT_PATH / "Plans"
DONE              = VAULT_PATH / "Done"
LOGS              = VAULT_PATH / "Logs"
SKILLS            = VAULT_PATH / "Skills"
PENDING_APPROVAL  = VAULT_PATH / "Pending_Approval"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [RalphLoop] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("RalphLoop")


# ── Per-Source-Type Iteration Limits ─────────────────────────────────────────
#
# Each source type has a complexity profile that determines how many Claude
# invocations are needed to reliably complete that class of task:
#
#   "promise" completions rarely need more than 3–4 iters for simple replies
#   because Claude can read the file, draft, and declare done in one shot.
#
#   Cross-domain tasks need more iterations because they require:
#     (a) reading the original file  (b) reading the correlated item(s)
#     (c) drafting a coherent cross-channel response
#     (d) invoking the MCP server   (e) moving the file to /Done/
#
# These are per-task caps. The CLI --max-iterations flag acts as a hard
# ceiling across ALL tasks and overrides these values when it is lower.

MAX_ITERATIONS_BY_SOURCE: dict[str, int] = {
    "email":     5,    # draft + send; single MCP call
    "whatsapp":  4,    # short reply; often requires approval first
    "linkedin":  6,    # draft post + publish via MCP
    "twitter":   4,    # short tweet; MCP publish is one call
    "facebook":  4,    # social post; MCP publish is one call
    "instagram": 4,    # social post; MCP publish is one call
    "youtube":   4,    # comment reply; MCP publish is one call
    "file":      8,    # file analysis can require multiple sub-steps
    "odoo":      6,    # accounting tasks involve several API calls
    "cross":     10,   # cross-domain: MCP + correlation + coordination
    "custom":    MAX_ITERATIONS,
    "unknown":   MAX_ITERATIONS,
}

# ── Skill Router ──────────────────────────────────────────────────────────────
#
# Maps action file filename prefixes → skill .md file in /Skills/.
# The skill file defines the inputs, instructions, output format, and rules
# Claude must follow for that class of task.
#
# For CROSS_ items we also look at the source_type field in frontmatter so
# the most relevant skill (e.g., "youtube_reply.md" for a CROSS_BUSINESS_YOUTUBE_
# item) is injected rather than the generic "task_planner.md".

PREFIX_TO_SKILL: dict[str, str] = {
    "EMAIL_":     "email_triage.md",
    "WHATSAPP_":  "whatsapp_reply.md",
    "LINKEDIN_":  "linkedin_post.md",
    "TWITTER_":   "social_media_post.md",
    "FACEBOOK_":  "social_media_post.md",
    "INSTAGRAM_": "social_media_post.md",
    "TW_TWEET_":  "social_media_post.md",
    "FB_POST_":   "social_media_post.md",
    "IG_POST_":   "social_media_post.md",
    "YOUTUBE_":   "youtube_reply.md",
    "FILE_":      "task_planner.md",
    "CROSS_":     "task_planner.md",   # default; overridden by source_type below
    "APPROVAL_":  "task_planner.md",
    "ODOO_":      "accounting_audit.md",
    "SYSTEM_":    "task_planner.md",
}

# Finer-grained mapping used for cross-domain items where the source_type field
# in frontmatter tells us the actual channel (e.g., "youtube" for CROSS_*_YOUTUBE_*).
SOURCE_TYPE_TO_SKILL: dict[str, str] = {
    "email":     "email_triage.md",
    "whatsapp":  "whatsapp_reply.md",
    "linkedin":  "linkedin_post.md",
    "twitter":   "social_media_post.md",
    "facebook":  "social_media_post.md",
    "instagram": "social_media_post.md",
    "youtube":   "youtube_reply.md",
    "file":      "task_planner.md",
    "odoo":      "accounting_audit.md",
    "cross":     "task_planner.md",
}

# Priority ordering for batch sort (lower = processed first)
_PRIORITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


# ── TaskContext ───────────────────────────────────────────────────────────────

@dataclass
class TaskContext:
    """
    Parsed metadata for a single action file, built before the loop starts.

    Having structured context available from iteration 1 means we can inject
    the right skill, MCP route, and correlation data into every prompt without
    re-parsing the frontmatter on each iteration.

    Fields
    ------
    source_type           Watcher origin: email | whatsapp | linkedin | twitter |
                          facebook | instagram | youtube | file | odoo | cross | custom
    domain                personal | business | cross | unknown
    mcp_route             MCP server name if this is a cross-domain item (else "").
                          Example: "linkedin-post", "twitter-post", "youtube"
    priority              critical | high | medium | low (from frontmatter)
    correlation_ids       SHA hashes of correlated items from the opposite domain.
                          Only set for CROSS_ files with cross-domain links.
    matched_topics        Business keywords that triggered correlation detection.
    skill_path            Absolute path to the relevant /Skills/*.md file (or "").
    max_iterations        Effective iteration cap for this task type.
    requires_approval     If True, Claude should write to /Pending_Approval instead
                          of acting directly (cross-domain items already have this
                          resolved, so it is usually False here).
    is_cross_domain       True when the file came from CrossDomainIntegrator.
    original_source_file  For CROSS_ items: the original watcher file path.
    attempt_count         Number of iterations across ALL previous sessions,
                          read from a .ralph_<stem>.json sidecar if it exists.
    """
    source_type:          str       = "unknown"
    domain:               str       = "unknown"
    mcp_route:            str       = ""
    priority:             str       = "medium"
    correlation_ids:      list[str] = field(default_factory=list)
    matched_topics:       list[str] = field(default_factory=list)
    skill_path:           str       = ""
    max_iterations:       int       = MAX_ITERATIONS
    requires_approval:    bool      = False
    is_cross_domain:      bool      = False
    original_source_file: str       = ""
    attempt_count:        int       = 0


def _parse_task_context(
    action_file: Path,
    global_max_iterations: int = MAX_ITERATIONS,
) -> TaskContext:
    """
    Build a TaskContext by reading the action file's YAML frontmatter.

    Steps
    -----
    1. Parse frontmatter key/value pairs.
    2. Determine source_type: prefer frontmatter field, then infer from prefix.
    3. Set is_cross_domain flag for CROSS_* files.
    4. Resolve the matching /Skills/*.md path (source_type → PREFIX fallback).
    5. Set the effective iteration cap: min(per_type_limit, global_ceiling).
    6. Load previous attempt count from sidecar (.ralph_<stem>.json).

    The global_max_iterations parameter is the hard ceiling set by --max-iterations.
    If the per-type limit is lower, the per-type limit wins. If the user explicitly
    passes --max-iterations 20 the global ceiling allows more room.
    """
    ctx = TaskContext()

    # ── 1. Parse frontmatter ───────────────────────────────────────────────
    fm = _parse_frontmatter(action_file)

    ctx.source_type  = (fm.get("source_type") or fm.get("source") or "unknown").strip("\"'").lower()
    ctx.domain       = (fm.get("domain") or "unknown").strip("\"'").lower()
    ctx.mcp_route    = (fm.get("mcp_route") or "").strip("\"'").lower().replace("none", "")
    ctx.priority     = (fm.get("priority") or "medium").strip("\"'").lower()
    ctx.requires_approval = str(fm.get("requires_approval", "false")).lower() == "true"
    ctx.original_source_file = fm.get("source_file", "").strip("\"'")

    # ── 2. Cross-domain flag ───────────────────────────────────────────────
    # CROSS_ prefix is written by CrossDomainIntegrator; the type field also
    # confirms it. Cross-domain items carry MCP routing + correlation data.
    ctx.is_cross_domain = (
        action_file.name.upper().startswith("CROSS_")
        or fm.get("type", "").startswith("cross_domain")
    )
    if ctx.is_cross_domain and ctx.source_type in ("unknown", "cross"):
        # CROSS_ items often have a more specific source_type (e.g., "youtube").
        # Fall back to "cross" only if no finer-grained type was found.
        ctx.source_type = ctx.source_type if ctx.source_type != "unknown" else "cross"

    # ── 3. Parse list fields (correlation IDs and matched topics) ──────────
    # These are written as [hash1, hash2, ...] inline YAML by CrossDomainIntegrator.
    raw_corr = fm.get("correlation_ids", "")
    ctx.correlation_ids = [
        c.strip() for c in raw_corr.strip("[]").split(",") if c.strip()
    ]
    raw_topics = fm.get("matched_topics", "")
    ctx.matched_topics = [
        t.strip() for t in raw_topics.strip("[]").split(",") if t.strip()
    ]

    # ── 4. Infer source_type from filename prefix if still unknown ─────────
    if ctx.source_type == "unknown":
        upper = action_file.name.upper()
        for prefix in PREFIX_TO_SKILL:
            if upper.startswith(prefix):
                # "EMAIL_" → "email", "TW_TWEET_" → "twitter" (special cases below)
                inferred = prefix.rstrip("_").lower()
                ctx.source_type = {
                    "tw_tweet": "twitter",
                    "fb_post":  "facebook",
                    "ig_post":  "instagram",
                }.get(inferred, inferred)
                break

    # ── 5. Resolve skill path ──────────────────────────────────────────────
    # Priority: source_type lookup (most specific for cross-domain)
    #           → prefix lookup (fallback)
    #           → task_planner.md (universal fallback)
    skill_filename = (
        SOURCE_TYPE_TO_SKILL.get(ctx.source_type)
        or _skill_from_prefix(action_file.name)
        or "task_planner.md"
    )
    candidate = SKILLS / skill_filename
    ctx.skill_path = str(candidate) if candidate.exists() else ""

    # ── 6. Effective iteration cap ─────────────────────────────────────────
    # The per-type limit is the baseline. The global ceiling (from CLI or env)
    # is the hard cap. We take the minimum so:
    #   - Short tasks (email, 5) are never run 10x unnecessarily.
    #   - The operator can still override downward with --max-iterations 3.
    per_type = MAX_ITERATIONS_BY_SOURCE.get(ctx.source_type, MAX_ITERATIONS)
    ctx.max_iterations = min(per_type, global_max_iterations)

    # ── 7. Previous attempt count from sidecar ────────────────────────────
    # The sidecar is written by _update_attempt_sidecar() when a task stalls.
    # It accumulates across sessions so the stalled escalation file can show
    # the full history. Deleted on successful completion by _cleanup_sidecar().
    sidecar = _sidecar_path(action_file)
    if sidecar.exists():
        try:
            data = json.loads(sidecar.read_text())
            ctx.attempt_count = int(data.get("attempt_count", 0))
        except (json.JSONDecodeError, ValueError, OSError):
            ctx.attempt_count = 0

    return ctx


def _skill_from_prefix(filename: str) -> str:
    """Return the skill filename for the given action file name, or ''."""
    upper = filename.upper()
    for prefix, skill_file in PREFIX_TO_SKILL.items():
        if upper.startswith(prefix):
            return skill_file
    return ""


def _sidecar_path(action_file: Path) -> Path:
    """
    Return the path of the hidden attempt-count sidecar for action_file.

    The sidecar is a tiny JSON file named .ralph_<stem>.json in the same
    directory as the action file. It stores the cumulative attempt count so
    stalled-task escalations can show the full history across restarts.

    It is intentionally hidden (dot-prefixed) and should be in .gitignore.
    """
    return action_file.parent / f".ralph_{action_file.stem}.json"


# ── Audit Logger ──────────────────────────────────────────────────────────────

def log_action(action_type: str, details: dict):
    """
    Append a structured entry to today's JSON log.

    Uses the same schema as orchestrator.py and base_watcher.py so all log
    files can be queried uniformly. Fields: timestamp, actor, action_type,
    details (arbitrary dict).
    """
    LOGS.mkdir(parents=True, exist_ok=True)
    today    = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log_file = LOGS / f"{today}.json"

    entry = {
        "timestamp":   datetime.now(timezone.utc).isoformat(),
        "actor":       "ralph_loop",
        "action_type": action_type,
        "details":     details,
    }

    entries = []
    if log_file.exists():
        try:
            entries = json.loads(log_file.read_text())
        except (json.JSONDecodeError, ValueError):
            entries = []

    entries.append(entry)
    log_file.write_text(json.dumps(entries, indent=2))


# ── Frontmatter Parser ────────────────────────────────────────────────────────

def _parse_frontmatter(path: Path) -> dict:
    """
    Extract key: value pairs from YAML-like frontmatter between --- delimiters.

    Intentionally lightweight (no external YAML dependency). Handles the flat,
    unquoted schema written by all watchers and cross_domain_integration.py.
    Multi-line values and nested structures are not supported — all fields in
    this project are single-line scalars or inline lists.

    Returns an empty dict if the file has no frontmatter or cannot be read.
    """
    fm: dict = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return fm

    if not text.startswith("---"):
        return fm

    # Find the closing "---" on its own line
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


# ── Completion Checker ────────────────────────────────────────────────────────

class CompletionChecker:
    """
    Encapsulates all supported task-completion detection strategies.

    Modes
    -----
    "promise"    (default)
        Look for <promise>TASK_COMPLETE</promise> in Claude's output. This is
        the most explicit signal — Claude has to intentionally emit it. However,
        if Claude moves the file to /Done/ but forgets the tag, this mode misses
        the completion. Use --combine-done-file to catch that edge case.

    "done_file"  (recommended for cross-domain / MCP tasks)
        Check whether the action file has been removed from its source directory.
        Claude's task instructions always include "move the file to /Done/", so
        if the file is gone it means Claude executed that step. We additionally
        scan the /Done/ and /Cross_Domain/Done/ archives for a timestamped copy
        to confirm the file landed there (vs. being accidentally deleted).

    "file_glob"  (legacy / custom)
        Check whether any file matching the given glob pattern now exists.
        Useful for tasks that produce a known output artifact. If the pattern
        doesn't match anything the check returns False.

    combine_done_file
        When True, the done_file check is run as a secondary signal alongside
        the primary mode. Useful in production where you want promise as the
        primary signal but still want to catch the "moved but forgot tag" case.
    """

    def __init__(
        self,
        mode: str = "promise",
        glob_pattern: str = "",
        combine_done_file: bool = False,
    ):
        self.mode              = mode
        self.glob_pattern      = glob_pattern
        self.combine_done_file = combine_done_file

    def is_complete(self, result: "ClaudeResult", action_file: Path) -> bool:
        """
        Return True if the task should be considered done.

        Parameters
        ----------
        result      : Output from the latest Claude CLI invocation.
        action_file : Original action file path (needed for done_file check).
        """
        primary = self._check_primary(result, action_file)

        # Secondary done_file check: catch "moved but forgot promise" edge case.
        # Only evaluated when primary is False to avoid redundant filesystem calls.
        if not primary and self.combine_done_file and self.mode != "done_file":
            primary = _check_done_file(action_file)

        return primary

    def _check_primary(self, result: "ClaudeResult", action_file: Path) -> bool:
        if self.mode == "promise":
            # Promise tag must appear in Claude's stdout
            return result.completed

        if self.mode == "done_file":
            return _check_done_file(action_file)

        if self.mode == "file_glob":
            if not self.glob_pattern:
                logger.warning(
                    "Completion mode 'file_glob' requires --completion-check <pattern>; "
                    "treating task as incomplete."
                )
                return False
            return len(glob.glob(self.glob_pattern)) > 0

        logger.warning(f"Unknown completion mode '{self.mode}' — treating as incomplete")
        return False


def _check_done_file(action_file: Path) -> bool:
    """
    Return True if the action file has been moved out of its source directory.

    We perform two checks:
      1. Fast check: the file simply doesn't exist at its original path.
      2. Confirmation: a timestamped copy exists in /Done/ or /Cross_Domain/Done/.

    Check 2 prevents a false positive if the file was accidentally deleted rather
    than deliberately moved. If the file is absent AND no archive copy is found,
    we still return True (with a debug log) because the absence is the stronger
    signal and we don't want to block completion on a missing archive entry.
    """
    if action_file.exists():
        return False  # File still in place — definitely not done yet

    # File is absent — check for timestamped archive copy as confirmation
    stem = action_file.stem
    for done_dir in [DONE, CROSS_DONE]:
        if done_dir.exists():
            # Archive names are like: 20260225_143022_EMAIL_foo.md
            matches = list(done_dir.glob(f"*_{stem}*"))
            if matches:
                logger.debug(f"done_file confirmed in {done_dir.name}: {matches[0].name}")
                return True

    # File absent but no archive copy found — probably Claude moved it without
    # the standard timestamp prefix. Still treat as complete.
    logger.debug(f"done_file: {action_file.name} absent (no archive copy found — assuming moved)")
    return True


# ── Claude Invocation ─────────────────────────────────────────────────────────

@dataclass
class ClaudeResult:
    """Result from a single Claude CLI invocation."""
    output:    str
    exit_code: int
    completed: bool       # True if promise tag was found in output
    error:     str | None = None


def invoke_claude(prompt: str, timeout: int = CLAUDE_TIMEOUT) -> ClaudeResult:
    """
    Invoke `claude -p <prompt> --no-input` and capture the result.

    The --no-input flag tells Claude Code not to prompt the operator for
    interactive input, making the call safe inside an automated pipeline.

    Error classification
    --------------------
    FileNotFoundError  → claude binary missing; FATAL, loop should stop immediately.
    TimeoutExpired     → Claude took too long; NON-FATAL, loop continues to next iter.
    Non-zero exit code with no stdout → NON-FATAL error logged, loop continues.

    Dry-run behaviour: returns a synthetic completed=True result so the loop
    exits after one iteration without touching any real files.
    """
    if DRY_RUN:
        logger.info(f"[DRY RUN] Would invoke Claude ({len(prompt)} char prompt)")
        logger.debug(f"Prompt preview: {prompt[:200]}...")
        return ClaudeResult(output="[DRY RUN] No output", exit_code=0, completed=True)

    try:
        result = subprocess.run(
            [CLAUDE_CMD, "-p", prompt, "--no-input"],
            cwd=str(VAULT_PATH),
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        output = result.stdout or ""
        stderr = result.stderr or ""

        # Non-zero exit with empty stdout: treat stderr as the error message
        if result.returncode != 0 and not output:
            return ClaudeResult(
                output=stderr,
                exit_code=result.returncode,
                completed=False,
                error=f"Claude exited {result.returncode}: {stderr[:300]}",
            )

        # Promise detection: the tag must appear verbatim on its own
        completed = f"<promise>{COMPLETION_PROMISE}</promise>" in output

        return ClaudeResult(output=output, exit_code=result.returncode, completed=completed)

    except subprocess.TimeoutExpired:
        return ClaudeResult(
            output="",
            exit_code=-1,
            completed=False,
            error=f"Claude timed out after {timeout}s",
        )
    except FileNotFoundError:
        return ClaudeResult(
            output="",
            exit_code=-1,
            completed=False,
            error=(
                f"Claude command '{CLAUDE_CMD}' not found. "
                "Install Claude Code or set CLAUDE_CMD in .env."
            ),
        )


# ── Prompt Builders ───────────────────────────────────────────────────────────

def build_initial_prompt(action_file: Path, ctx: TaskContext) -> str:
    """
    Build the first-iteration prompt for processing an action file.

    The prompt is structured as follows:
      1. Role and priority framing
      2. Task: which file to process
      3. Rules: read Company_Handbook.md first
      4. Skill instruction (if a matching /Skills/*.md exists)
      5. [MCP section] — only for cross-domain items with mcp_route set
      6. [Correlation section] — only for CROSS_ items with correlation_ids
      7. Completion checklist (7 steps every task must follow)
      8. Completion instruction (promise tag + blocking policy)

    Sections 5 and 6 are injected only when relevant so non-cross-domain
    items get clean, focused prompts without unrelated instructions.
    """
    # Skill instruction — tell Claude which skill file to read for task type
    skill_instruction = ""
    if ctx.skill_path:
        skill_instruction = (
            f"\n\n**SKILL FILE**: Read `{ctx.skill_path}` for step-by-step instructions "
            f"specific to this task type (`{ctx.source_type}`). Follow them exactly."
        )

    # MCP routing instructions — only for cross-domain items with a known MCP server.
    # The MCP routing block embedded in the CROSS_ file contains the specific tool
    # name and parameters; this section points Claude to that block.
    mcp_section = ""
    if ctx.mcp_route:
        mcp_section = f"""

**MCP SERVER REQUIRED**: This task must be executed via the `{ctx.mcp_route}` MCP server.
After drafting the response, invoke the appropriate MCP tool to send or publish it.
The "MCP Routing Instructions" section in the action file contains the exact tool name
and parameters to use. If the MCP server is unavailable, write the draft response to
`/Pending_Approval/` with a note explaining why it couldn't be sent automatically."""

    # Cross-domain correlation context — only when correlated items were found.
    # Tells Claude to coordinate its response for consistency across channels.
    corr_section = ""
    if ctx.correlation_ids:
        topics_str = ", ".join(ctx.matched_topics) if ctx.matched_topics else "see action file"
        corr_section = f"""

**CROSS-DOMAIN CORRELATION**: This item is linked to {len(ctx.correlation_ids)} item(s)
from the opposite communication domain (shared business topics: {topics_str}).
Review `/Cross_Domain/index.json` for correlated items and coordinate your response
to ensure a consistent message across all channels (e.g., don't say different things
on LinkedIn vs. Email to the same person about the same topic)."""

    # Priority framing — surfaces urgency at the very top for high-priority items
    priority_note = ""
    if ctx.priority in ("critical", "high"):
        priority_note = (
            f"\n\n**PRIORITY: {ctx.priority.upper()}** — "
            "Handle this before any lower-priority items in the queue."
        )

    return f"""You are the AI Employee processing an action item.{priority_note}

**TASK**: Read and fully process the action file at `{action_file}`
**RULES**: Read `Company_Handbook.md` before taking any action.{skill_instruction}{mcp_section}{corr_section}

**STEPS** (complete ALL of these before declaring done):
1. Read the action file at `{action_file}`
2. Read `Company_Handbook.md` for approval thresholds and communication rules
3. {"Read the skill file at `" + ctx.skill_path + "` for detailed task-type instructions" if ctx.skill_path else "Determine the best approach for this task type"}
4. Create a `PLAN_*.md` in `/Plans/` with checkboxes for every sub-step
5. Execute each step of the plan:
   - Draft any responses, documents, or reports required
   - {"Invoke the `" + ctx.mcp_route + "` MCP server to send / publish the response" if ctx.mcp_route else "Send responses via the appropriate channel"}
   - Write any step requiring human approval to `/Pending_Approval/` with full context
6. Update `Dashboard.md` with the new activity and outcome
7. Move the original file from its current location to `/Done/` with a UTC timestamp prefix

**COMPLETION**: When ALL 7 steps above are genuinely complete, output exactly this on its own line:
<promise>{COMPLETION_PROMISE}</promise>

Do NOT output the promise until every step is done.
If something is blocking you, explain the specific blocker and do NOT output the promise."""


def build_continuation_prompt(
    action_file: Path,
    iteration: int,
    previous_output: str,
    ctx: TaskContext,
) -> str:
    """
    Build a follow-up prompt for iterations 2–N when the previous one didn't complete.

    This prompt is more targeted than the initial one: it shows Claude what was
    already done, then asks it to identify and complete only the remaining steps.

    Context truncation
    ------------------
    previous_output is capped at 3000 chars (1500 head + gap + 1500 tail) to
    avoid hitting token limits while preserving the most informative content
    from both ends of the previous response.

    Completion probe
    ----------------
    We check whether the action file still exists. If Claude already moved it
    to /Done/ (done_file completion) but forgot to emit the promise tag, we
    tell it to just output the promise so the loop can exit cleanly.

    MCP and correlation reminders are injected only when relevant so the
    continuation prompt isn't cluttered for simple single-domain tasks.
    """
    # Truncate previous output to fit within a safe context budget
    max_context = 3000
    if len(previous_output) > max_context:
        previous_output = (
            previous_output[:1500]
            + f"\n\n... [{len(previous_output) - 3000} chars truncated] ...\n\n"
            + previous_output[-1500:]
        )

    # Check if Claude already moved the file (done_file early completion)
    file_moved = not action_file.exists()
    file_status_line = (
        f"NOTE: The action file is no longer at `{action_file}` — Claude already moved it. "
        f"If all steps are complete, just output the promise tag now."
        if file_moved
        else f"The action file is still at `{action_file}` — you must move it to `/Done/` when done."
    )

    # MCP reminder — only shown if Claude hasn't moved the file yet (task incomplete)
    mcp_reminder = ""
    if ctx.mcp_route and not file_moved:
        mcp_reminder = (
            f"\n- [ ] **MCP**: Did you invoke `{ctx.mcp_route}` to send/publish the response? "
            "If not, do so now before declaring complete."
        )

    # Correlation reminder — only for cross-domain items with pending coordination
    corr_reminder = ""
    if ctx.correlation_ids and not file_moved:
        corr_reminder = (
            f"\n- [ ] **Cross-domain**: Did you coordinate with the "
            f"{len(ctx.correlation_ids)} correlated item(s) in `/Cross_Domain/index.json`?"
        )

    return f"""You are the AI Employee continuing work on an action item.

**ITERATION**: {iteration}/{ctx.max_iterations} (previous attempt did not complete)
**TASK**: Continue processing: `{action_file}`
**{file_status_line}**

**PREVIOUS OUTPUT** (what happened in the last iteration):
---
{previous_output}
---

**REMAINING CHECKLIST** — identify which steps are still incomplete and do them now:
1. [ ] `PLAN_*.md` created in `/Plans/` with checkboxes for every sub-step
2. [ ] Response / action drafted for `{ctx.source_type}`
3. [ ] All approval-required steps written to `/Pending_Approval/`{mcp_reminder}{corr_reminder}
4. [ ] `Dashboard.md` updated with latest activity
5. [ ] Action file moved to `/Done/` with UTC timestamp prefix

Complete every remaining step, then output exactly:
<promise>{COMPLETION_PROMISE}</promise>

If something is still blocking you, explain the specific blocker and do NOT output the promise."""


# ── Stalled Task Escalation ───────────────────────────────────────────────────

def _escalate_stalled_task(
    action_file: Path,
    ctx: TaskContext,
    iterations_used: int,
    last_output: str,
) -> Path:
    """
    Write a RALPH_STALLED_*.md to /Pending_Approval/ when the loop is exhausted.

    This gives the human operator visibility and a clear decision tree:
      a) Retry: move the original file back to /Needs_Action/, optionally
         increase RALPH_MAX_ITERATIONS in .env, then delete this stalled file.
      b) Manual handling: open the original file directly in Claude Code.
      c) Abandon: move this file to /Rejected/ and the original to /Done/.

    A .ralph_<stem>.json sidecar is also updated so that if the operator does
    retry, the attempt count is preserved across sessions.
    """
    PENDING_APPROVAL.mkdir(parents=True, exist_ok=True)

    now    = datetime.now(timezone.utc)
    stamp  = now.strftime("%Y%m%d_%H%M%S")
    filename = f"RALPH_STALLED_{action_file.stem}_{stamp}.md"
    output_path = PENDING_APPROVAL / filename

    # Truncate last output so the escalation file doesn't become enormous
    output_preview = (last_output[:800] + "\n…") if len(last_output) > 800 else last_output
    total_attempts = ctx.attempt_count + iterations_used

    content = f"""---
type: ralph_stalled
source_file: "{action_file}"
source_type: {ctx.source_type}
domain: {ctx.domain}
mcp_route: {ctx.mcp_route or "none"}
priority: {ctx.priority}
iterations_this_run: {iterations_used}
total_attempts_all_runs: {total_attempts}
iteration_cap_used: {ctx.max_iterations}
stalled_at: {now.isoformat()}
status: pending
---

# Ralph Loop Stalled: {action_file.name}

The AI Employee attempted this task **{iterations_used} time(s)** in this run
(total: **{total_attempts}** attempt(s) across all sessions) without completing it.

## Task Details
| Field | Value |
|-------|-------|
| **File** | `{action_file}` |
| **Source type** | {ctx.source_type} |
| **Domain** | {ctx.domain} |
| **Priority** | {ctx.priority.upper()} |
| **MCP route** | {ctx.mcp_route or "none"} |
| **Iteration cap** | {ctx.max_iterations} |
{"| **Correlated items** | " + str(len(ctx.correlation_ids)) + " |" if ctx.correlation_ids else ""}

## Last Output (truncated)
```
{output_preview}
```

## What To Do Next
| Option | Steps |
|--------|-------|
| **Retry with more iterations** | 1. Set `RALPH_MAX_ITERATIONS=20` in `.env`  2. Move original file back to `/Needs_Action/`  3. Move this file to `/Rejected/` |
| **Handle manually** | Open `{action_file.name}` in Claude Code directly and work through it interactively |
| **Abandon** | Move this file to `/Rejected/` and manually move the original to `/Done/` |

---
*Auto-generated by ralph_loop.py — {now.strftime("%Y-%m-%d %H:%M UTC")}*
"""

    try:
        output_path.write_text(content, encoding="utf-8")
        logger.warning(f"Stalled task escalated → Pending_Approval/{filename}")
    except OSError as exc:
        logger.error(f"Failed to write stalled escalation file: {exc}")
        return Path()

    # Update the sidecar so the next operator-triggered retry knows the history
    _update_attempt_sidecar(action_file, total_attempts)

    return output_path


def _update_attempt_sidecar(action_file: Path, total_attempts: int) -> None:
    """
    Write / update the hidden .ralph_<stem>.json sidecar for action_file.

    The sidecar accumulates the total attempt count across restarts and runs.
    It is only written when a task FAILS to complete; successfully completed
    tasks have their sidecar removed by _cleanup_sidecar().

    This file should be in .gitignore (pattern: .ralph_*.json).
    """
    sidecar = _sidecar_path(action_file)
    data = {
        "source_file":   str(action_file),
        "attempt_count": total_attempts,
        "last_attempt":  datetime.now(timezone.utc).isoformat(),
    }
    try:
        sidecar.write_text(json.dumps(data, indent=2))
    except OSError as exc:
        logger.debug(f"Could not write attempt sidecar: {exc}")


def _cleanup_sidecar(action_file: Path) -> None:
    """
    Remove the .ralph_<stem>.json sidecar when a task completes successfully.

    This keeps the /Needs_Action directory tidy: only stalled tasks accumulate
    sidecars, and those are cleaned up if the operator retries and succeeds.
    """
    sidecar = _sidecar_path(action_file)
    if sidecar.exists():
        try:
            sidecar.unlink()
        except OSError:
            pass


# ── Move to Done ──────────────────────────────────────────────────────────────

def _move_to_done(action_file: Path, ctx: TaskContext) -> Optional[Path]:
    """
    Archive the completed action file to the appropriate /Done/ directory.

    Routing
    -------
    Cross-domain items (CROSS_ prefix or is_cross_domain=True) go to
    /Cross_Domain/Done/ to keep the cross-domain audit trail separate.
    All other items go to the top-level /Done/.

    No-op behaviour
    ---------------
    If the action file no longer exists (Claude already moved it as part of
    the task execution), this is a no-op returning None. This is the expected
    path when completion_mode="done_file" or when Claude faithfully follows
    the step-7 instruction to move the file.

    The destination is prefixed with a UTC timestamp so /Done/ remains sorted
    chronologically and duplicate task names don't collide.
    """
    if not action_file.exists():
        # Claude already moved the file — this is the happy path for done_file mode
        logger.debug(f"_move_to_done: {action_file.name} already archived by Claude")
        return None

    done_dir = CROSS_DONE if ctx.is_cross_domain else DONE
    done_dir.mkdir(parents=True, exist_ok=True)

    ts   = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    dest = done_dir / f"{ts}_{action_file.name}"

    try:
        shutil.move(str(action_file), str(dest))
        archive_label = "Cross_Domain/Done" if ctx.is_cross_domain else "Done"
        logger.info(f"Archived → {archive_label}/{dest.name}")
    except OSError as exc:
        logger.error(f"Failed to move {action_file.name} to Done: {exc}")
        return None

    return dest


# ── Priority Sort ─────────────────────────────────────────────────────────────

def _sort_by_priority(files: list[Path]) -> list[Path]:
    """
    Sort action files by priority tier first, then oldest-first within each tier.

    Priority is read from the frontmatter `priority:` field. Files without a
    recognisable value default to "medium" (tier 2).

    Tier order: critical(0) → high(1) → medium(2) → low(3)

    Sorting oldest-first within each tier ensures FIFO processing for items at
    the same priority, preventing indefinite starvation of any single item.
    """
    def sort_key(f: Path) -> tuple:
        fm   = _parse_frontmatter(f)
        prio = fm.get("priority", "medium").strip("\"'").lower()
        tier = _PRIORITY_ORDER.get(prio, 2)
        mtime = f.stat().st_mtime if f.exists() else 0.0
        return (tier, mtime)

    return sorted(files, key=sort_key)


# ── Ralph Loop Core ───────────────────────────────────────────────────────────

@dataclass
class LoopResult:
    """Result from a full Ralph loop execution on one action file."""
    action_file:  str
    iterations:   int
    completed:    bool
    final_output: str
    error:        str | None = None


def ralph_loop(
    action_file: Path,
    max_iterations: int = MAX_ITERATIONS,
    completion_mode: str = "promise",
    completion_check: str = "",
    combine_done_file: bool = False,
) -> LoopResult:
    """
    Run the Ralph Wiggum loop on a single action file.

    This is the primary function imported by orchestrator.py. It:
      1. Parses task context from the action file's YAML frontmatter.
      2. Sets the effective iteration cap (per-type vs global ceiling).
      3. Builds and sends prompts to Claude in a retry loop.
      4. Checks for completion after each Claude invocation.
      5. On success: moves the file to /Done/ (if Claude didn't already) and
         removes the attempt-count sidecar.
      6. On max-iterations exhaustion: escalates to /Pending_Approval/ and
         updates the sidecar for cross-session attempt tracking.

    Parameters
    ----------
    action_file       : Path to the .md file to process (Needs_Action or
                        Cross_Domain/Needs_Action).
    max_iterations    : Hard ceiling on iterations. Per-type limits may be
                        lower; the effective cap is min(per_type, max_iterations).
    completion_mode   : "promise" | "done_file" | "file_glob"
    completion_check  : Glob pattern for "file_glob" mode.
    combine_done_file : Also check done_file as a secondary completion signal.

    Returns
    -------
    LoopResult. completed=True means the task finished; the orchestrator
    logs "ralph_completed". completed=False means it stalled; the orchestrator
    logs "ralph_incomplete" and leaves the file for operator review.
    """
    # ── Parse context from frontmatter ────────────────────────────────────
    ctx = _parse_task_context(action_file, global_max_iterations=max_iterations)

    # ── Set up completion detector ─────────────────────────────────────────
    checker = CompletionChecker(
        mode=completion_mode,
        glob_pattern=completion_check,
        combine_done_file=combine_done_file,
    )

    logger.info("=" * 62)
    logger.info(f"Ralph Loop START: {action_file.name}")
    logger.info(f"  Source type  : {ctx.source_type}")
    logger.info(f"  Domain       : {ctx.domain}")
    logger.info(f"  Priority     : {ctx.priority}")
    logger.info(f"  Max iters    : {ctx.max_iterations}  (global cap: {max_iterations})")
    logger.info(f"  Completion   : {completion_mode}" + (" + done_file" if combine_done_file else ""))
    logger.info(f"  MCP route    : {ctx.mcp_route or 'none'}")
    logger.info(f"  Cross-domain : {ctx.is_cross_domain}  correlations={len(ctx.correlation_ids)}")
    logger.info(f"  Prior runs   : {ctx.attempt_count} attempt(s)")
    logger.info(f"  Dry run      : {DRY_RUN}")
    logger.info("=" * 62)

    log_action("ralph_loop_started", {
        "file":            action_file.name,
        "source_type":     ctx.source_type,
        "domain":          ctx.domain,
        "priority":        ctx.priority,
        "max_iterations":  ctx.max_iterations,
        "completion_mode": completion_mode,
        "mcp_route":       ctx.mcp_route or None,
        "is_cross_domain": ctx.is_cross_domain,
        "correlation_count": len(ctx.correlation_ids),
        "prior_attempts":  ctx.attempt_count,
    })

    previous_output = ""

    for iteration in range(1, ctx.max_iterations + 1):
        logger.info(f"--- Iteration {iteration}/{ctx.max_iterations} ---")

        # ── Build prompt for this iteration ───────────────────────────────
        if iteration == 1:
            # First pass: full context with skill + MCP + correlation sections
            prompt = build_initial_prompt(action_file, ctx)
        else:
            # Follow-up passes: targeted checklist based on previous output
            prompt = build_continuation_prompt(action_file, iteration, previous_output, ctx)

        # ── Call Claude ────────────────────────────────────────────────────
        result = invoke_claude(prompt)

        # ── Fatal: binary not found — stop the entire loop immediately ─────
        # Continuing would spin on the same error; better to surface it fast.
        if result.error and "not found" in (result.error or ""):
            logger.error(f"FATAL: {result.error}")
            log_action("ralph_fatal_error", {
                "file":      action_file.name,
                "iteration": iteration,
                "error":     result.error,
            })
            return LoopResult(
                action_file=str(action_file),
                iterations=iteration,
                completed=False,
                final_output=result.output,
                error=result.error,
            )

        # ── Non-fatal error (timeout, exit code) — log and continue ───────
        # Claude may have partially executed steps even if it errored; the
        # continuation prompt will ask it to pick up where it left off.
        if result.error:
            logger.warning(f"Iteration {iteration} non-fatal error: {result.error}")
            log_action("ralph_iteration_error", {
                "file":      action_file.name,
                "iteration": iteration,
                "error":     result.error,
            })
            previous_output = result.output or result.error or ""
            continue

        previous_output = result.output
        logger.info(f"Iteration {iteration}: received {len(result.output)} chars")

        # ── Check completion ───────────────────────────────────────────────
        completed = checker.is_complete(result, action_file)

        log_action("ralph_iteration_done", {
            "file":          action_file.name,
            "iteration":     iteration,
            "completed":     completed,
            "output_length": len(result.output),
        })

        if completed:
            logger.info(f"TASK COMPLETE after {iteration} iteration(s)")

            if not DRY_RUN:
                # Move to Done/ — no-op if Claude already archived the file
                _move_to_done(action_file, ctx)
                # Clean up the sidecar (only needed for stalled tasks)
                _cleanup_sidecar(action_file)

            log_action("ralph_loop_completed", {
                "file":        action_file.name,
                "source_type": ctx.source_type,
                "iterations":  iteration,
                "mcp_route":   ctx.mcp_route or None,
            })

            return LoopResult(
                action_file=str(action_file),
                iterations=iteration,
                completed=True,
                final_output=result.output,
            )

        logger.info(f"Iteration {iteration}: not yet complete, continuing…")

    # ── All iterations exhausted without completion ────────────────────────
    logger.warning(
        f"Max iterations ({ctx.max_iterations}) exhausted without completion: "
        f"{action_file.name}"
    )

    if not DRY_RUN:
        # Escalate to /Pending_Approval/ for human review
        _escalate_stalled_task(action_file, ctx, ctx.max_iterations, previous_output)

    log_action("ralph_loop_stalled", {
        "file":          action_file.name,
        "source_type":   ctx.source_type,
        "iterations":    ctx.max_iterations,
        "total_attempts": ctx.attempt_count + ctx.max_iterations,
    })

    return LoopResult(
        action_file=str(action_file),
        iterations=ctx.max_iterations,
        completed=False,
        final_output=previous_output,
        error=f"Max iterations ({ctx.max_iterations}) reached without completion",
    )


# ── Batch Processor ───────────────────────────────────────────────────────────

def process_all_needs_action(
    max_iterations: int = MAX_ITERATIONS,
    completion_mode: str = "promise",
    completion_check: str = "",
    combine_done_file: bool = False,
    source_filter: str = "",
    include_cross_domain: bool = True,
) -> list[LoopResult]:
    """
    Process all pending action items from /Needs_Action and (optionally)
    /Cross_Domain/Needs_Action in a single pass.

    Cross-domain integration
    ------------------------
    When include_cross_domain=True (the default), this function also scans
    /Cross_Domain/Needs_Action/ for CROSS_ files written by CrossDomainIntegrator.
    These are processed with full MCP-aware prompts because _parse_task_context()
    reads their frontmatter and injects the mcp_route, correlation_ids, and
    matched_topics into the prompts automatically.

    Items from both directories are merged into a single list and sorted by
    priority tier → mtime so critical items are always processed first.

    Source filtering
    ----------------
    When source_filter is non-empty (e.g. "email", "cross", "linkedin"), only
    items whose source_type or filename prefix matches the filter are included.
    Useful for testing a specific integration or for targeted operator runs.

    Parameters
    ----------
    max_iterations       : Global iteration ceiling (per-type limits may be lower).
    completion_mode      : "promise" | "done_file" | "file_glob"
    completion_check     : Glob pattern for file_glob mode.
    combine_done_file    : Also check done_file as a secondary signal.
    source_filter        : Only process items of this source_type (or prefix).
    include_cross_domain : Scan /Cross_Domain/Needs_Action/ when True.

    Returns
    -------
    List of LoopResult, one per processed file.
    """
    # ── Gather candidate files from both directories ───────────────────────
    all_files: list[Path] = []

    if NEEDS_ACTION.exists():
        all_files += [
            f for f in NEEDS_ACTION.iterdir()
            if f.is_file() and f.suffix == ".md" and not f.name.startswith(".")
        ]

    # Cross_Domain/Needs_Action/ — enriched routing files from CrossDomainIntegrator
    if include_cross_domain and CROSS_NEEDS_ACTION.exists():
        all_files += [
            f for f in CROSS_NEEDS_ACTION.iterdir()
            if f.is_file() and f.suffix == ".md" and not f.name.startswith(".")
        ]

    if not all_files:
        logger.info("No action items found in /Needs_Action or /Cross_Domain/Needs_Action")
        return []

    # ── Apply optional source filter ───────────────────────────────────────
    if source_filter:
        filter_lower = source_filter.lower()
        filtered: list[Path] = []
        for f in all_files:
            fm = _parse_frontmatter(f)
            st = (fm.get("source_type") or fm.get("source") or "").strip("\"'").lower()
            # Also match by filename prefix so "email" matches EMAIL_* even without frontmatter
            prefix_match = f.name.upper().startswith(filter_lower.upper() + "_")
            if st == filter_lower or prefix_match:
                filtered.append(f)
        logger.info(
            f"Source filter '{source_filter}': {len(filtered)}/{len(all_files)} items match"
        )
        all_files = filtered

    if not all_files:
        logger.info(f"No items match source filter '{source_filter}'")
        return []

    # ── Sort by priority tier → oldest-first within each tier ─────────────
    all_files = _sort_by_priority(all_files)
    logger.info(f"Processing {len(all_files)} item(s) [sorted: critical → high → medium → low]")

    results: list[LoopResult] = []

    for action_file in all_files:
        result = ralph_loop(
            action_file=action_file,
            max_iterations=max_iterations,
            completion_mode=completion_mode,
            completion_check=completion_check,
            combine_done_file=combine_done_file,
        )
        results.append(result)

        status = "COMPLETED" if result.completed else "STALLED  "
        logger.info(
            f"[{status}] {action_file.name}  ({result.iterations} iter(s))"
        )

    # ── Print summary ──────────────────────────────────────────────────────
    n_done    = sum(1 for r in results if r.completed)
    n_stalled = len(results) - n_done

    logger.info("=" * 62)
    logger.info(f"BATCH COMPLETE: {n_done}/{len(results)} done, {n_stalled} stalled")
    if n_stalled:
        logger.info("  Stalled items escalated → /Pending_Approval/ for operator review")
    logger.info("=" * 62)

    return results


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(
        description="Ralph Wiggum Loop — Drive Claude to completion on multi-step tasks",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Completion modes:
  promise       (default) Claude outputs <promise>TASK_COMPLETE</promise>
  done_file               Action file moved out of /Needs_Action by Claude
  file_glob               File matching --completion-check glob now exists

Examples:
  uv run python ralph_loop.py
  uv run python ralph_loop.py --file /path/to/Needs_Action/EMAIL_xxx.md
  uv run python ralph_loop.py --completion-mode done_file --combine-done-file
  uv run python ralph_loop.py --source-filter cross
  uv run python ralph_loop.py --no-cross-domain --max-iterations 5
  uv run python ralph_loop.py --dry-run
""",
    )
    parser.add_argument(
        "--file", type=str,
        help="Process a single action file (full path)",
    )
    parser.add_argument(
        "--prompt", type=str,
        help="Custom prompt wrapped in a temp CUSTOM_*.md action file",
    )
    parser.add_argument(
        "--max-iterations", type=int, default=MAX_ITERATIONS,
        help=(
            f"Hard ceiling on iterations per task (default: {MAX_ITERATIONS}). "
            "Per-source-type limits may be lower — see MAX_ITERATIONS_BY_SOURCE."
        ),
    )
    parser.add_argument(
        "--completion-mode",
        choices=["promise", "done_file", "file_glob"],
        default="promise",
        help="Primary completion detection strategy (default: promise)",
    )
    parser.add_argument(
        "--completion-check", type=str, default="",
        help="Glob pattern for file_glob mode (e.g. '/Plans/PLAN_*.md')",
    )
    parser.add_argument(
        "--combine-done-file", action="store_true",
        help="Also check done_file as a secondary signal alongside the primary mode",
    )
    parser.add_argument(
        "--source-filter", type=str, default="",
        help="Only process items of this source_type (e.g. email, cross, linkedin)",
    )
    parser.add_argument(
        "--no-cross-domain", action="store_true",
        help="Skip /Cross_Domain/Needs_Action — process /Needs_Action only",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Build prompts but don't invoke Claude or move any files",
    )
    return parser.parse_args()


def main():
    global DRY_RUN

    args = parse_args()
    if args.dry_run:
        DRY_RUN = True

    # Ensure all required directories exist before any file operations
    for d in [NEEDS_ACTION, PLANS, DONE, LOGS, PENDING_APPROVAL]:
        d.mkdir(parents=True, exist_ok=True)

    if args.prompt:
        # ── Custom prompt mode ─────────────────────────────────────────────
        # Wrap in a temp CUSTOM_*.md so the loop treats it like a real action file
        # and the skill/context/sidecar machinery works consistently.
        logger.info("Custom prompt mode")
        now = datetime.now(timezone.utc)
        temp_file = NEEDS_ACTION / f"CUSTOM_{now.strftime('%Y%m%d_%H%M%S')}.md"
        temp_file.write_text(
            f"---\ntype: custom_task\nsource_type: custom\n"
            f"priority: medium\ncreated: {now.isoformat()}\n---\n\n{args.prompt}\n"
        )
        result = ralph_loop(
            action_file=temp_file,
            max_iterations=args.max_iterations,
            completion_mode=args.completion_mode,
            completion_check=args.completion_check,
            combine_done_file=args.combine_done_file,
        )
        sys.exit(0 if result.completed else 1)

    elif args.file:
        # ── Single file mode ───────────────────────────────────────────────
        action_file = Path(args.file)
        if not action_file.exists():
            logger.error(f"File not found: {action_file}")
            sys.exit(1)
        result = ralph_loop(
            action_file=action_file,
            max_iterations=args.max_iterations,
            completion_mode=args.completion_mode,
            completion_check=args.completion_check,
            combine_done_file=args.combine_done_file,
        )
        sys.exit(0 if result.completed else 1)

    else:
        # ── Batch mode: process all pending items ──────────────────────────
        results = process_all_needs_action(
            max_iterations=args.max_iterations,
            completion_mode=args.completion_mode,
            completion_check=args.completion_check,
            combine_done_file=args.combine_done_file,
            source_filter=args.source_filter,
            include_cross_domain=not args.no_cross_domain,
        )
        n_completed = sum(1 for r in results if r.completed)
        sys.exit(0 if n_completed == len(results) else 1)


if __name__ == "__main__":
    main()
