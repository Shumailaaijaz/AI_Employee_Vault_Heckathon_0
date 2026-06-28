"""Orchestrator — The master process for the Personal AI Employee.

Responsibilities:
  1. Start and monitor all watcher subprocesses
  2. Start and health-check all MCP servers (email, linkedin, facebook, twitter, odoo)
  3. Poll /Needs_Action for new items and trigger Claude Code to process them
  4. Poll /Approved for human-approved actions and execute them
  5. Run scheduled tasks (weekly CEO briefing, hourly dashboard)
  6. Implement graceful fallback when an MCP server is unavailable
  7. Integrate with pm2 when available; fall back to subprocess management

Startup sequence (phased):
  Phase 1 — Critical MCPs:   mcp-gmail, mcp-odoo  (must be up before watchers)
  Phase 2 — Social MCPs:     mcp-linkedin, mcp-twitter, mcp-social, mcp-youtube
  Phase 3 — Python watchers: file, gmail, whatsapp, linkedin, youtube, twitter, facebook
  Phase 4 — Integration:     cross_domain integrator
  Phase 5 — Self:            main polling loop starts

pm2 integration:
  When pm2 is on PATH the orchestrator uses `pm2 jlist` to query status and
  `pm2 restart <app>` to restart failed servers instead of spawning its own
  subprocesses. If pm2 is absent, direct subprocess management is used as a
  fallback for both watchers and MCP servers.

MCP health checks:
  1. pm2 status query (fast, only available if pm2 is running)
  2. stdio JSON-RPC initialize ping (deep check — spawns server, sends initialize,
     expects a valid JSON-RPC response within HEALTH_PING_TIMEOUT_S seconds)

Fallback modes per MCP server:
  "queue"  — queue action to /Pending_Approval with note about server being down
  "skip"   — log and continue; action will be retried next cycle
  "error"  — write a NEEDS_ACTION alert file, notify operator

Usage:
    uv run python orchestrator.py                  # Full start (all phases)
    uv run python orchestrator.py --no-watchers     # Orchestrator loop only (pm2 owns processes)
    uv run python orchestrator.py --no-mcp-servers  # Skip MCP server startup
    uv run python orchestrator.py --dry-run         # Log but don't execute
    uv run python orchestrator.py --watchers gmail twitter  # Start specific watchers only
"""

import os
import sys
import json
import signal
import select
import subprocess
import argparse
import shutil
import threading
from pathlib import Path
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
import time
import logging

from dotenv import load_dotenv
from error_recovery import ErrorRecovery, RetryConfig, with_retry

load_dotenv()

# ── Config ─────────────────────────────────────────────────────────────────────

VAULT_PATH   = Path(os.getenv("VAULT_PATH", "/mnt/d/AI_Employee_Vault/AI_Employee_Vault"))
DRY_RUN      = os.getenv("DRY_RUN", "true").lower() == "true"
POLL_INTERVAL    = int(os.getenv("ORCHESTRATOR_POLL_INTERVAL", "15"))
MCP_HEALTH_EVERY = int(os.getenv("MCP_HEALTH_EVERY", "5"))    # check MCPs every Nth poll cycle
MAX_RESTART_ATTEMPTS    = int(os.getenv("MAX_RESTART_ATTEMPTS", "5"))
HEALTH_PING_TIMEOUT_S   = float(os.getenv("MCP_HEALTH_PING_TIMEOUT", "4.0"))
STARTUP_PHASE_DELAY_S   = float(os.getenv("MCP_STARTUP_PHASE_DELAY", "2.0"))
CLAUDE_CMD   = os.getenv("CLAUDE_CMD", "claude")

# Vault directories
NEEDS_ACTION       = VAULT_PATH / "Needs_Action"
CROSS_NEEDS_ACTION = VAULT_PATH / "Cross_Domain" / "Needs_Action"  # output of CrossDomainIntegrator
APPROVED           = VAULT_PATH / "Approved"
PENDING_APPROVAL   = VAULT_PATH / "Pending_Approval"
REJECTED           = VAULT_PATH / "Rejected"
DONE               = VAULT_PATH / "Done"
PLANS              = VAULT_PATH / "Plans"
LOGS               = VAULT_PATH / "Logs"
BRIEFINGS          = VAULT_PATH / "Briefings"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("Orchestrator")


# ── MCP Server Registry ────────────────────────────────────────────────────────

@dataclass
class McpServerInfo:
    """Metadata and runtime state for a managed MCP server."""
    name: str                        # display name
    pm2_app: str                     # pm2 app name (e.g. "mcp-gmail")
    script: str                      # path to index.js (relative to VAULT_PATH)
    startup_priority: int            # 1=critical, 2=social, 3=optional
    provides_tools: list             # which tool names this server exposes
    fallback_mode: str               # "queue" | "skip" | "error"
    health_status: str = "unknown"   # "healthy" | "degraded" | "down" | "unknown"
    last_health_check: datetime | None = None
    consecutive_failures: int = 0
    process: subprocess.Popen | None = None  # direct-subprocess fallback


MCP_SERVER_REGISTRY: dict[str, McpServerInfo] = {
    # ── Priority 1: Critical (accounting + email) ──────────────────────────
    "gmail": McpServerInfo(
        name="Gmail-Send MCP",
        pm2_app="mcp-gmail",
        script="mcp-servers/gmail-send/index.js",
        startup_priority=1,
        provides_tools=["gmail_send_email", "gmail_draft_email"],
        fallback_mode="queue",
    ),
    "odoo": McpServerInfo(
        name="Odoo-Accounting MCP",
        pm2_app="mcp-odoo",
        script="mcp-servers/odoo-accounting/index.js",
        startup_priority=1,
        provides_tools=[
            "odoo_get_customer", "odoo_create_invoice", "odoo_post_payment",
            "odoo_get_balance", "odoo_list_invoices", "odoo_accounting_summary",
        ],
        fallback_mode="queue",
    ),
    # ── Priority 2: Social ─────────────────────────────────────────────────
    "linkedin": McpServerInfo(
        name="LinkedIn-Post MCP",
        pm2_app="mcp-linkedin",
        script="mcp-servers/linkedin-post/index.js",
        startup_priority=2,
        provides_tools=[
            "linkedin_get_profile", "linkedin_draft_post",
            "linkedin_publish_post", "linkedin_delete_post",
        ],
        fallback_mode="queue",
    ),
    "twitter": McpServerInfo(
        name="Twitter MCP",
        pm2_app="mcp-twitter",
        script="mcp-servers/twitter/index.js",
        startup_priority=2,
        provides_tools=[
            "twitter_post_tweet", "twitter_reply_tweet",
            "twitter_generate_summary", "twitter_get_thread",
            "twitter_delete_tweet", "twitter_get_mentions",
        ],
        fallback_mode="queue",
    ),
    "social": McpServerInfo(
        name="Social-Post MCP",
        pm2_app="mcp-social",
        script="mcp-servers/social-post/index.js",
        startup_priority=2,
        provides_tools=[
            "facebook_post_message", "instagram_post_message",
            "twitter_post_tweet", "social_generate_summary",
        ],
        fallback_mode="queue",
    ),
    "youtube": McpServerInfo(
        name="YouTube MCP",
        pm2_app="mcp-youtube",
        script="mcp-servers/youtube/index.js",
        startup_priority=2,
        provides_tools=["youtube_reply_comment", "youtube_get_video_stats"],
        fallback_mode="skip",
    ),
    # Dedicated Facebook/Instagram read+reply MCP.
    # Complements social-post (publishing) with the full engagement loop:
    # reading comments/messages, replying, deleting spam, page insights.
    "facebook": McpServerInfo(
        name="Facebook-Instagram MCP",
        pm2_app="mcp-facebook",
        script="mcp-servers/facebook-mcp/index.js",
        startup_priority=2,
        provides_tools=[
            "facebook_health_check",
            "facebook_get_comments",
            "facebook_reply_comment",
            "facebook_delete_comment",
            "facebook_get_conversations",
            "facebook_send_message",
            "facebook_get_page_insights",
            "instagram_get_comments",
            "instagram_reply_comment",
        ],
        fallback_mode="queue",
    ),
}


# ── Watcher Registry ───────────────────────────────────────────────────────────

@dataclass
class WatcherInfo:
    """Metadata for a managed watcher subprocess."""
    name: str
    script: str
    pm2_app: str = ""                # pm2 app name (empty = orchestrator-managed)
    process: subprocess.Popen | None = None
    restart_count: int = 0
    last_start: datetime | None = None
    enabled: bool = True


WATCHER_REGISTRY: dict[str, WatcherInfo] = {
    "file":         WatcherInfo(name="FileWatcher",              script="filesystem_watcher.py",       pm2_app="watcher-file"),
    "gmail":        WatcherInfo(name="GmailWatcher",             script="gmail_watcher.py",            pm2_app="watcher-gmail"),
    "whatsapp":     WatcherInfo(name="WhatsAppWatcher",          script="whatsapp_watcher.py",         pm2_app="watcher-whatsapp"),
    "linkedin":     WatcherInfo(name="LinkedInWatcher",          script="linkedin_watcher.py",         pm2_app="watcher-linkedin"),
    "youtube":      WatcherInfo(name="YouTubeWatcher",           script="youtube_watcher.py",          pm2_app="watcher-youtube"),
    "twitter":      WatcherInfo(name="TwitterWatcher",           script="twitter_watcher.py",          pm2_app="watcher-twitter"),
    "facebook":     WatcherInfo(name="FacebookInstagramWatcher", script="facebook_watcher.py",         pm2_app="watcher-facebook"),
    "cross_domain": WatcherInfo(name="CrossDomainIntegrator",    script="cross_domain_integration.py", pm2_app="watcher-cross-domain"),
}


# ── Audit Logger ───────────────────────────────────────────────────────────────

def log_action(action_type: str, details: dict):
    """Append an entry to today's JSON log file."""
    LOGS.mkdir(parents=True, exist_ok=True)
    today    = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log_file = LOGS / f"{today}.json"
    entry    = {
        "timestamp":   datetime.now(timezone.utc).isoformat(),
        "actor":       "orchestrator",
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


# ── MCP Health Checker ─────────────────────────────────────────────────────────

class McpHealthChecker:
    """
    Two-level health check for MCP servers:
      1. pm2 query (fast) — checks if pm2 reports the app as "online"
      2. stdio JSON-RPC ping (deep) — spawns server, sends MCP initialize, checks response
    """

    # MCP initialize request (minimal valid handshake)
    _INIT_MSG = (
        json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "health-probe", "version": "1.0"},
            },
        }) + "\n"
    ).encode()

    @staticmethod
    def is_pm2_available() -> bool:
        """Return True if pm2 is installed and accessible."""
        return shutil.which("pm2") is not None

    @staticmethod
    def pm2_status(pm2_app: str) -> str | None:
        """
        Query pm2 for a single app's status.
        Returns 'online' | 'stopped' | 'errored' | 'launching' | None (not found).
        """
        try:
            result = subprocess.run(
                ["pm2", "jlist"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode != 0:
                return None
            apps = json.loads(result.stdout)
            for app in apps:
                if app.get("name") == pm2_app:
                    return app.get("pm2_env", {}).get("status", "unknown")
        except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
            pass
        return None

    @classmethod
    def stdio_ping(cls, script_path: Path) -> bool:
        """
        Spawn the MCP server, send an initialize message, and verify the response.
        Returns True if the server responds with a valid JSON-RPC result.
        """
        if not script_path.exists():
            return False

        proc = None
        try:
            proc = subprocess.Popen(
                ["node", str(script_path)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(VAULT_PATH),
            )
            proc.stdin.write(cls._INIT_MSG)
            proc.stdin.flush()

            # Read response line with timeout using threading
            response_lines = []
            def _read():
                try:
                    line = proc.stdout.readline()
                    if line:
                        response_lines.append(line.decode(errors="replace"))
                except Exception:
                    pass

            reader = threading.Thread(target=_read, daemon=True)
            reader.start()
            reader.join(timeout=HEALTH_PING_TIMEOUT_S)

            if not response_lines:
                return False

            # Validate JSON-RPC response
            data = json.loads(response_lines[0])
            return "result" in data and "serverInfo" in data.get("result", {})

        except (json.JSONDecodeError, Exception):
            return False
        finally:
            if proc:
                try:
                    proc.terminate()
                    proc.wait(timeout=2)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass

    @classmethod
    def check(cls, server: McpServerInfo) -> str:
        """
        Run health check for a server. Returns "healthy" | "degraded" | "down".
        Tries pm2 first (fast), falls back to stdio ping.
        """
        # ── pm2 check ──────────────────────────────────────────────────────────
        if cls.is_pm2_available():
            status = cls.pm2_status(server.pm2_app)
            if status == "online":
                return "healthy"
            if status in ("stopped", "errored"):
                return "down"
            if status == "launching":
                return "degraded"
            # status is None (not registered in pm2) — fall through to stdio ping

        # ── stdio ping ─────────────────────────────────────────────────────────
        script = VAULT_PATH / server.script
        if not script.exists():
            return "down"

        return "healthy" if cls.stdio_ping(script) else "down"


# ── MCP Server Manager ─────────────────────────────────────────────────────────

class McpServerManager:
    """
    Start, stop, restart, and health-check MCP servers.
    Uses pm2 when available; falls back to direct subprocess management.
    """

    def __init__(self, registry: dict[str, McpServerInfo]):
        self.registry  = registry
        self._use_pm2  = McpHealthChecker.is_pm2_available()
        if self._use_pm2:
            logger.info("pm2 detected — using pm2 for MCP server management")
        else:
            logger.info("pm2 not found — using direct subprocess management for MCP servers")

    # ── Start ──────────────────────────────────────────────────────────────────

    def start(self, key: str) -> bool:
        """Start a single MCP server. Returns True on success."""
        server = self.registry[key]
        script = VAULT_PATH / server.script

        if not script.exists():
            logger.warning(f"MCP script not found: {script} — skipping {server.name}")
            server.health_status = "down"
            return False

        if self._use_pm2:
            return self._pm2_start(server, script)
        return self._proc_start(server, script)

    def _pm2_start(self, server: McpServerInfo, script: Path) -> bool:
        """Start or restart via pm2."""
        # Check if already managed by pm2
        status = McpHealthChecker.pm2_status(server.pm2_app)
        if status == "online":
            logger.debug(f"{server.name} already online in pm2")
            server.health_status = "healthy"
            return True

        if status in ("stopped", "errored"):
            # Restart existing pm2 app
            cmd = ["pm2", "restart", server.pm2_app]
        elif status is None:
            # Register new app with pm2
            cmd = [
                "pm2", "start", str(script),
                "--name", server.pm2_app,
                "--no-autorestart",     # orchestrator controls restart logic
                "--cwd", str(VAULT_PATH),
            ]
        else:
            # Launching or unknown — wait a moment
            time.sleep(2)
            cmd = ["pm2", "restart", server.pm2_app]

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            if result.returncode == 0:
                logger.info(f"pm2 started {server.name}")
                server.health_status = "healthy"
                log_action("mcp_started_pm2", {"name": server.name, "app": server.pm2_app})
                return True
            else:
                logger.error(f"pm2 start failed for {server.name}: {result.stderr[:200]}")
                server.health_status = "down"
                return False
        except subprocess.TimeoutExpired:
            logger.error(f"pm2 start timed out for {server.name}")
            server.health_status = "down"
            return False

    def _proc_start(self, server: McpServerInfo, script: Path) -> bool:
        """Start as a direct subprocess (pm2 fallback)."""
        if server.process and server.process.poll() is None:
            logger.debug(f"{server.name} already running (PID {server.process.pid})")
            server.health_status = "healthy"
            return True

        try:
            server.process = subprocess.Popen(
                ["node", str(script)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(VAULT_PATH),
            )
            logger.info(f"Started {server.name} (PID {server.process.pid})")
            server.health_status = "healthy"
            log_action("mcp_started_proc", {"name": server.name, "pid": server.process.pid})
            return True
        except Exception as exc:
            logger.error(f"Failed to start {server.name}: {exc}")
            server.health_status = "down"
            return False

    # ── Phased startup ─────────────────────────────────────────────────────────

    def start_all_ordered(self) -> dict[str, bool]:
        """
        Start all MCP servers in priority order.
        Phase 1 (priority 1) starts first, then Phase 2 after STARTUP_PHASE_DELAY_S.
        Returns {key: success} dict.
        """
        results: dict[str, bool] = {}
        by_priority: dict[int, list[str]] = {}
        for key, server in self.registry.items():
            by_priority.setdefault(server.startup_priority, []).append(key)

        for priority in sorted(by_priority.keys()):
            group = by_priority[priority]
            label = "Critical" if priority == 1 else "Social" if priority == 2 else "Optional"
            logger.info(f"MCP startup Phase {priority} ({label}): {[self.registry[k].pm2_app for k in group]}")

            for key in group:
                ok = self.start(key)
                results[key] = ok
                if not ok:
                    server = self.registry[key]
                    logger.warning(f"{server.name} failed to start — operating in fallback mode")
                    log_action("mcp_start_failed", {"name": server.name, "fallback": server.fallback_mode})

            if priority < max(by_priority.keys()):
                logger.debug(f"Waiting {STARTUP_PHASE_DELAY_S}s before next MCP startup phase...")
                time.sleep(STARTUP_PHASE_DELAY_S)

        return results

    # ── Restart ────────────────────────────────────────────────────────────────

    def restart(self, key: str) -> bool:
        """Restart a single MCP server."""
        server = self.registry[key]
        logger.info(f"Restarting {server.name}...")

        if self._use_pm2:
            try:
                result = subprocess.run(
                    ["pm2", "restart", server.pm2_app],
                    capture_output=True, text=True, timeout=10,
                )
                ok = result.returncode == 0
                server.health_status = "healthy" if ok else "down"
                if ok:
                    server.consecutive_failures = 0
                    log_action("mcp_restarted_pm2", {"name": server.name})
                return ok
            except subprocess.TimeoutExpired:
                return False

        # Subprocess fallback: kill existing, start new
        if server.process:
            try:
                server.process.terminate()
                server.process.wait(timeout=3)
            except Exception:
                try:
                    server.process.kill()
                except Exception:
                    pass
        return self.start(key)

    # ── Health check all ───────────────────────────────────────────────────────

    def check_health_all(self) -> dict[str, str]:
        """
        Run health checks on all registered MCP servers.
        Updates health_status on each server and triggers restart/fallback as needed.
        Returns {key: status} dict.
        """
        results: dict[str, str] = {}
        now = datetime.now(timezone.utc)

        for key, server in self.registry.items():
            prev_status = server.health_status
            status      = McpHealthChecker.check(server)

            server.health_status      = status
            server.last_health_check  = now

            if status != "healthy":
                server.consecutive_failures += 1
            else:
                server.consecutive_failures  = 0

            results[key] = status

            # Log status changes
            if status != prev_status:
                logger.warning(f"{server.name}: {prev_status} → {status}")
                log_action("mcp_health_changed", {
                    "name":     server.name,
                    "previous": prev_status,
                    "current":  status,
                    "failures": server.consecutive_failures,
                })

            # Auto-restart if down but not too many consecutive failures
            if status == "down" and 1 <= server.consecutive_failures <= 3:
                logger.info(f"Auto-restarting {server.name} (failure #{server.consecutive_failures})")
                self.restart(key)

            elif status == "down" and server.consecutive_failures > 3:
                logger.error(
                    f"{server.name} has failed {server.consecutive_failures} consecutive health checks. "
                    f"Fallback mode: {server.fallback_mode}"
                )
                self._apply_fallback(server)

        return results

    # ── Fallback routing ───────────────────────────────────────────────────────

    def _apply_fallback(self, server: McpServerInfo):
        """
        Apply the server's fallback_mode policy when it is persistently down.
        "queue"  — write a /Pending_Approval alert for the human operator
        "skip"   — log only; orchestrator will retry next health-check cycle
        "error"  — write a /Needs_Action CRITICAL alert file
        """
        now  = datetime.now(timezone.utc)
        safe = server.pm2_app.replace("-", "_").upper()

        if server.fallback_mode == "queue":
            filename = f"APPROVAL_MCP_DOWN_{safe}_{now.strftime('%Y-%m-%d_%H%M%S')}.md"
            filepath = PENDING_APPROVAL / filename
            PENDING_APPROVAL.mkdir(parents=True, exist_ok=True)
            filepath.write_text(f"""---
type: mcp_server_down
server_name: "{server.name}"
pm2_app: "{server.pm2_app}"
consecutive_failures: {server.consecutive_failures}
tools_affected: {server.provides_tools}
fallback_mode: queue
detected: {now.isoformat()}
status: pending_approval
---

# MCP Server Down: {server.name}

The `{server.pm2_app}` MCP server has been unresponsive for
{server.consecutive_failures} consecutive health checks.

## Affected Tools
{chr(10).join(f"- `{t}`" for t in server.provides_tools)}

## What to Do
1. Check logs: `pm2 logs {server.pm2_app}`
2. Restart: `pm2 restart {server.pm2_app}`
3. If credentials expired, update `.env` and restart.
4. Once fixed, move this file to `/Done`.

## Queued Actions
Any actions requiring these tools have been queued here in
/Pending_Approval until the server is restored.
""", encoding="utf-8")
            logger.warning(f"Fallback (queue): created {filename}")
            log_action("mcp_fallback_queued", {"name": server.name, "file": filename})

        elif server.fallback_mode == "error":
            filename = f"MCP_CRITICAL_{safe}_{now.strftime('%Y-%m-%d_%H%M%S')}.md"
            filepath = NEEDS_ACTION / filename
            NEEDS_ACTION.mkdir(parents=True, exist_ok=True)
            filepath.write_text(f"""---
type: mcp_server_critical
server_name: "{server.name}"
priority: critical
detected: {now.isoformat()}
status: pending
---

# CRITICAL: {server.name} is Down

Restart immediately: `pm2 restart {server.pm2_app}`
""", encoding="utf-8")
            logger.error(f"Fallback (error): created NEEDS_ACTION alert {filename}")
            log_action("mcp_fallback_error_alert", {"name": server.name, "file": filename})

        else:  # "skip"
            logger.info(f"Fallback (skip): {server.name} down — will retry next cycle")

    # ── Status summary ─────────────────────────────────────────────────────────

    def get_status(self) -> dict[str, dict]:
        """Return status dict for dashboard display."""
        result = {}
        for key, server in self.registry.items():
            last = server.last_health_check
            result[server.name] = {
                "health":   server.health_status,
                "failures": server.consecutive_failures,
                "tools":    len(server.provides_tools),
                "last_check": last.strftime("%Y-%m-%d %H:%M") if last else "never",
                "pm2_app":  server.pm2_app,
            }
        return result


# ── Process Manager (Python Watchers) ─────────────────────────────────────────

class ProcessManager:
    """Start, monitor, and restart Python watcher subprocesses."""

    def __init__(self, watchers: dict[str, WatcherInfo]):
        self.watchers = watchers
        self._use_pm2 = McpHealthChecker.is_pm2_available()

    def start_watcher(self, key: str):
        info = self.watchers[key]
        if not info.enabled:
            return

        script = VAULT_PATH / info.script
        if not script.exists():
            logger.warning(f"Script not found: {script} — skipping {info.name}")
            info.enabled = False
            return

        # If pm2 is available, delegate to pm2
        if self._use_pm2 and info.pm2_app:
            status = McpHealthChecker.pm2_status(info.pm2_app)
            if status == "online":
                logger.debug(f"{info.name} already online in pm2")
                return
            try:
                if status in ("stopped", "errored"):
                    subprocess.run(["pm2", "restart", info.pm2_app], capture_output=True, timeout=10)
                elif status is None:
                    subprocess.run(
                        ["pm2", "start", str(script), "--name", info.pm2_app,
                         "--interpreter", sys.executable, "--cwd", str(VAULT_PATH)],
                        capture_output=True, timeout=15,
                    )
                info.last_start = datetime.now(timezone.utc)
                log_action("watcher_started_pm2", {"name": info.name, "app": info.pm2_app})
                return
            except Exception as exc:
                logger.warning(f"pm2 start failed for {info.name}, falling back: {exc}")

        # Direct subprocess fallback
        if info.process and info.process.poll() is None:
            return

        logger.info(f"Starting {info.name} ({info.script})")
        try:
            info.process = subprocess.Popen(
                [sys.executable, str(script)],
                cwd=str(VAULT_PATH),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            info.last_start = datetime.now(timezone.utc)
            log_action("watcher_started", {"name": info.name, "pid": info.process.pid})
        except Exception as exc:
            logger.error(f"Failed to start {info.name}: {exc}")
            log_action("watcher_start_failed", {"name": info.name, "error": str(exc)})

    def start_all(self):
        for key in self.watchers:
            self.start_watcher(key)

    def check_health(self):
        for key, info in self.watchers.items():
            if not info.enabled:
                continue

            # pm2-managed: check via pm2 jlist
            if self._use_pm2 and info.pm2_app:
                status = McpHealthChecker.pm2_status(info.pm2_app)
                if status == "online":
                    info.restart_count = 0
                    continue
                if status in ("stopped", "errored"):
                    logger.warning(f"{info.name} is {status} in pm2 (restarts: {info.restart_count})")
                    if info.restart_count < MAX_RESTART_ATTEMPTS:
                        info.restart_count += 1
                        delay = min(2 ** info.restart_count, 30)
                        time.sleep(delay)
                        self.start_watcher(key)
                    else:
                        logger.error(f"{info.name} exceeded max restarts — disabling")
                        info.enabled = False
                continue

            # Direct subprocess: check poll()
            if info.process is None:
                continue
            retcode = info.process.poll()
            if retcode is not None:
                logger.warning(f"{info.name} exited ({retcode}), restarts: {info.restart_count}/{MAX_RESTART_ATTEMPTS}")
                log_action("watcher_died", {"name": info.name, "exit_code": retcode})
                if info.restart_count < MAX_RESTART_ATTEMPTS:
                    info.restart_count += 1
                    time.sleep(min(2 ** info.restart_count, 30))
                    self.start_watcher(key)
                else:
                    logger.error(f"{info.name} exceeded max restarts — disabling")
                    info.enabled = False
                    log_action("watcher_disabled", {"name": info.name})

    def stop_all(self):
        for key, info in self.watchers.items():
            if self._use_pm2 and info.pm2_app:
                try:
                    subprocess.run(["pm2", "stop", info.pm2_app], capture_output=True, timeout=10)
                except Exception:
                    pass
                continue
            if info.process and info.process.poll() is None:
                logger.info(f"Stopping {info.name} (PID {info.process.pid})")
                info.process.terminate()
                try:
                    info.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    info.process.kill()
                log_action("watcher_stopped", {"name": info.name})

    def get_status(self) -> dict[str, dict]:
        status = {}
        for key, info in self.watchers.items():
            if self._use_pm2 and info.pm2_app:
                pm2_s = McpHealthChecker.pm2_status(info.pm2_app) or "unknown"
                state = "Running" if pm2_s == "online" else pm2_s.capitalize()
            elif not info.enabled:
                state = "Disabled"
            elif info.process is None:
                state = "Not Started"
            elif info.process.poll() is None:
                state = "Running"
            else:
                state = f"Exited ({info.process.returncode})"

            status[info.name] = {
                "state":      state,
                "pid":        info.process.pid if info.process and info.process.poll() is None else None,
                "restarts":   info.restart_count,
                "last_start": info.last_start.isoformat() if info.last_start else None,
            }
        return status


# ── Needs_Action Processor ─────────────────────────────────────────────────────

def count_files(directory: Path) -> int:
    if not directory.exists():
        return 0
    return sum(1 for f in directory.iterdir() if f.is_file() and not f.name.startswith("."))


def process_needs_action():
    """
    Process all pending action files via the Ralph loop.

    Scans two directories:
      /Needs_Action               — items written directly by watchers
      /Cross_Domain/Needs_Action  — enriched routing files from CrossDomainIntegrator
                                    (carry mcp_route, correlation_ids, matched_topics)

    Both directories are merged and sorted by priority tier → oldest-first so
    critical items are always processed before lower-priority ones.

    combine_done_file=True means the loop also treats "file moved to /Done/ by Claude"
    as a valid completion signal, alongside the promise tag. This catches cases where
    Claude executes all steps correctly but forgets to emit the promise.
    """
    # Gather files from /Needs_Action
    all_files: list[Path] = []
    if NEEDS_ACTION.exists():
        all_files += [
            f for f in NEEDS_ACTION.iterdir()
            if f.is_file() and f.suffix == ".md" and not f.name.startswith(".")
        ]

    # Also gather CROSS_ files from /Cross_Domain/Needs_Action
    # These are written by CrossDomainIntegrator and carry MCP routing metadata
    if CROSS_NEEDS_ACTION.exists():
        all_files += [
            f for f in CROSS_NEEDS_ACTION.iterdir()
            if f.is_file() and f.suffix == ".md" and not f.name.startswith(".")
        ]

    if not all_files:
        return

    logger.info(
        f"Found {len(all_files)} item(s) to process "
        f"({sum(1 for f in all_files if 'Cross_Domain' in str(f))} cross-domain)"
    )

    from ralph_loop import ralph_loop as do_ralph_loop

    for action_file in all_files:
        logger.info(f"Processing via Ralph loop: {action_file.name}")
        if DRY_RUN:
            logger.info(f"[DRY RUN] Would Ralph-loop: {action_file.name}")
            log_action("needs_action_dry_run", {"file": action_file.name})
            continue

        result = do_ralph_loop(
            action_file=action_file,
            max_iterations=int(os.getenv("RALPH_MAX_ITERATIONS", "10")),
            completion_mode="promise",
            # Secondary check: also treat "file moved to /Done/" as completion.
            # Catches cases where Claude executed all steps but forgot the promise tag.
            combine_done_file=True,
        )
        status = "completed" if result.completed else "incomplete"
        logger.info(f"Ralph loop {status}: {action_file.name} ({result.iterations} iterations)")
        log_action(f"ralph_{status}", {"file": action_file.name, "iterations": result.iterations, "error": result.error})


# ── Approved Action Executor ───────────────────────────────────────────────────

def process_approved():
    if not APPROVED.exists():
        return
    approved_files = sorted(
        [f for f in APPROVED.iterdir() if f.is_file() and f.suffix == ".md"],
        key=lambda f: f.stat().st_mtime,
    )
    if not approved_files:
        return

    logger.info(f"Found {len(approved_files)} approved action(s)")

    for approved_file in approved_files:
        filename = approved_file.name
        if DRY_RUN:
            logger.info(f"[DRY RUN] Would execute: {filename}")
            log_action("approved_dry_run", {"file": filename})
            continue

        prompt = (
            f"The following action has been APPROVED by the human operator. "
            f"Read the approved file at {approved_file} and execute the "
            f"described action. After completion, move the file to /Done/ "
            f"with a timestamp prefix. Log the result. Update Dashboard.md."
        )

        def _run_claude():
            r = subprocess.run(
                [CLAUDE_CMD, "-p", prompt, "--no-input"],
                cwd=str(VAULT_PATH), capture_output=True, text=True, timeout=120,
            )
            if r.returncode != 0:
                raise RuntimeError(r.stderr[:300] or f"exit code {r.returncode}")
            return r

        # Retry the Claude subprocess up to 2 times before giving up
        _recovery = ErrorRecovery(vault_path=VAULT_PATH, component="Orchestrator")
        try:
            result = with_retry(
                _run_claude,
                config=RetryConfig(
                    max_attempts     = 2,
                    base_delay       = 5.0,
                    max_delay        = 30.0,
                    not_retryable_excs = (FileNotFoundError,),
                ),
                label=f"Claude/{filename[:40]}",
            )
            DONE.mkdir(parents=True, exist_ok=True)
            ts        = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            done_name = f"{ts}_{filename}"
            shutil.move(str(approved_file), str(DONE / done_name))
            logger.info(f"Completed and archived: {done_name}")
            log_action("approved_executed", {"file": filename, "moved_to": done_name})

        except FileNotFoundError:
            logger.error(f"Claude command '{CLAUDE_CMD}' not found — stopping approval processing.")
            _recovery.alert(
                level="ERROR",
                message=f"Claude command `{CLAUDE_CMD}` not found on PATH.",
                operation="process_approved",
                action=(
                    "Ensure Claude Code CLI is installed and CLAUDE_CMD in .env points to it. "
                    "Run `which claude` to check. Approvals will not be processed until fixed."
                ),
            )
            break

        except subprocess.TimeoutExpired:
            logger.error(f"Claude timed out (120s) executing: {filename}")
            log_action("approved_timeout", {"file": filename})
            _recovery.alert(
                level="WARNING",
                message=f"Approved action `{filename}` timed out after 120s.",
                operation="process_approved",
                action=(
                    f"File left in /Approved for manual retry. "
                    "Check system load and Claude responsiveness."
                ),
            )

        except Exception as exc:
            logger.error(f"Execution failed for {filename}: {exc}")
            log_action("approved_execution_error", {"file": filename, "error": str(exc)[:500]})
            # Queue the failed approval for human review
            _recovery.alerter.write_approval_request(
                component="Orchestrator",
                operation="process_approved",
                payload={"file": filename, "path": str(approved_file)},
                error=str(exc)[:400],
                action=(
                    "Move this file back to /Approved to retry, "
                    "or to /Rejected to abandon."
                ),
            )


# ── Dashboard Updater ──────────────────────────────────────────────────────────

def update_dashboard(watcher_status: dict | None = None, mcp_status: dict | None = None):
    """Rewrite Dashboard.md with current system state."""
    now = datetime.now(timezone.utc)

    needs_action_count = count_files(NEEDS_ACTION)
    pending_count      = count_files(PENDING_APPROVAL)
    approved_count     = count_files(APPROVED)
    done_today = 0
    if DONE.exists():
        today_prefix = now.strftime("%Y%m%d")
        done_today   = sum(1 for f in DONE.iterdir() if f.is_file() and f.name.startswith(today_prefix))

    # ── Watcher status table ──────────────────────────────────────────────────
    watcher_rows = ""
    if watcher_status:
        for name, info in watcher_status.items():
            state = info["state"]
            last  = (info["last_start"] or "-")[:19].replace("T", " ") if info["last_start"] else "-"
            restarts = info.get("restarts", 0)
            icon  = "🟢" if state == "Running" else "🔴"
            watcher_rows += f"| {icon} {name} | {state} | {last} | {restarts} |\n"
    else:
        watcher_rows = "| - | Unknown | - | - |\n"

    # ── MCP server status table ───────────────────────────────────────────────
    mcp_rows = ""
    if mcp_status:
        for name, info in mcp_status.items():
            health   = info["health"]
            failures = info["failures"]
            tools    = info["tools"]
            last     = info["last_check"]
            pm2_app  = info["pm2_app"]
            icon     = "🟢" if health == "healthy" else "🟡" if health == "degraded" else "🔴" if health == "down" else "⚪"
            mcp_rows += f"| {icon} {name} | {health.upper()} | {pm2_app} | {tools} tools | {failures} fails | {last} |\n"
    else:
        mcp_rows = "| ⚪ - | UNKNOWN | - | - | - | - |\n"

    # ── Recent activity ───────────────────────────────────────────────────────
    recent_rows = ""
    today_log = LOGS / f"{now.strftime('%Y-%m-%d')}.json"
    if today_log.exists():
        try:
            entries = json.loads(today_log.read_text())
            for entry in entries[-10:]:
                ts     = entry["timestamp"][:19].replace("T", " ")
                action = entry["action_type"]
                detail = str(entry.get("details", {}).get("file", ""))[:40] or "-"
                recent_rows += f"| {ts} | {action} | {detail} |\n"
        except (json.JSONDecodeError, KeyError):
            pass
    if not recent_rows:
        recent_rows = f"| {now.strftime('%Y-%m-%d %H:%M')} | orchestrator_running | - |\n"

    # ── Pending approval list ─────────────────────────────────────────────────
    approval_rows = ""
    if PENDING_APPROVAL.exists():
        for f in sorted(PENDING_APPROVAL.iterdir()):
            if f.is_file() and f.suffix == ".md":
                age = int((now.timestamp() - f.stat().st_mtime) / 60)
                approval_rows += f"| {f.name} | {age} min ago |\n"
    if not approval_rows:
        approval_rows = "| - | No pending approvals |\n"

    # ── pm2 note ──────────────────────────────────────────────────────────────
    pm2_note = (
        "pm2 active — use `pm2 status` for full process list"
        if McpHealthChecker.is_pm2_available()
        else "pm2 not detected — using direct subprocess management"
    )

    dashboard = f"""---
last_updated: {now.isoformat()}
updated_by: orchestrator
---

# AI Employee Dashboard

## System Status
| Component | Status | Last Check |
|-----------|--------|------------|
| Orchestrator | 🟢 Running | {now.strftime('%Y-%m-%d %H:%M:%S')} |
| pm2 | {'🟢 Active' if McpHealthChecker.is_pm2_available() else '⚪ Not detected'} | {now.strftime('%H:%M:%S')} |

## MCP Servers
| Server | Health | pm2 App | Tools | Failures | Last Check |
|--------|--------|---------|-------|----------|------------|
{mcp_rows}
## Watchers
| Watcher | State | Last Start | Restarts |
|---------|-------|------------|---------|
{watcher_rows}
## Queue Counts
- **Needs Action**: {needs_action_count}
- **Pending Approval**: {pending_count}
- **Approved (ready)**: {approved_count}
- **Done Today**: {done_today}

## Awaiting Approval
| File | Age |
|------|-----|
{approval_rows}
## Recent Activity (last 10)
| Timestamp | Action | Detail |
|-----------|--------|--------|
{recent_rows}
## Configuration
- **DRY_RUN**: {DRY_RUN}
- **Poll Interval**: {POLL_INTERVAL}s
- **MCP Health Check**: every {MCP_HEALTH_EVERY} cycles
- **Process manager**: {pm2_note}
"""
    (VAULT_PATH / "Dashboard.md").write_text(dashboard)


# ── CEO Briefing Generator ─────────────────────────────────────────────────────

def generate_ceo_briefing():
    """Run weekly_audit.py as a subprocess to produce the CEO briefing.

    weekly_audit.py reads Odoo balances, vault task counts, and bank CSVs directly,
    then writes a structured Briefings/YYYY-MM-DD_Weekly_Audit.md.  This is far more
    reliable than asking Claude to infer financial data from a vague prompt.
    """
    now        = datetime.now(timezone.utc)
    week_start = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    week_end   = now.strftime("%Y-%m-%d")
    period     = f"{week_start} to {week_end}"

    audit_script = VAULT_PATH / "weekly_audit.py"
    if not audit_script.exists():
        logger.error("weekly_audit.py not found at %s — skipping briefing", audit_script)
        log_action("ceo_briefing_error", {"reason": "weekly_audit.py missing", "period": period})
        return

    # Build the command — detect python interpreter
    python_bin = str(VAULT_PATH / ".venv" / "bin" / "python3")
    if not Path(python_bin).exists():
        import shutil
        python_bin = shutil.which("python3") or "python3"

    cmd = [python_bin, str(audit_script)]
    if DRY_RUN:
        cmd.append("--dry-run")

    logger.info("Running weekly_audit.py (%s) for period %s", "dry-run" if DRY_RUN else "live", period)
    try:
        result = subprocess.run(
            cmd,
            cwd=str(VAULT_PATH),
            capture_output=True,
            text=True,
            timeout=120,
            env={**os.environ, "VAULT_PATH": str(VAULT_PATH), "DRY_RUN": str(DRY_RUN).lower()},
        )
        if result.returncode == 0:
            logger.info("CEO Briefing generated successfully")
            log_action("ceo_briefing_generated", {"period": period, "dry_run": DRY_RUN})
        elif result.returncode == 1:
            # Exit code 1 = completed with ERROR-level alerts (not a crash)
            logger.warning("CEO Briefing completed with alerts — check Needs_Action/ for details")
            log_action("ceo_briefing_with_alerts", {"period": period})
        else:
            logger.error("weekly_audit.py exited %d: %s", result.returncode, result.stderr[:300])
            log_action("ceo_briefing_error", {"exit_code": result.returncode, "period": period})
        if result.stdout:
            for line in result.stdout.splitlines()[:20]:
                logger.info("[audit] %s", line)
    except subprocess.TimeoutExpired:
        logger.error("weekly_audit.py timed out after 120s")
        log_action("ceo_briefing_timeout", {"period": period})
    except Exception as exc:
        logger.error("CEO Briefing error: %s", exc)


# ── Scheduler ──────────────────────────────────────────────────────────────────

@dataclass
class ScheduledTask:
    name: str
    callback_name: str
    hour: int
    minute: int = 0
    days: list = field(default_factory=lambda: ["mon","tue","wed","thu","fri","sat","sun"])
    last_run: datetime | None = None


SCHEDULED_TASKS = [
    # Weekly full audit every Sunday at 21:00 UTC (per Company Handbook)
    ScheduledTask(name="Weekly Audit & CEO Briefing", callback_name="generate_ceo_briefing",
                  hour=21, days=["sun"]),
    # Also run Monday morning at 07:00 so the briefing is ready before work
    ScheduledTask(name="Monday Morning Briefing", callback_name="generate_ceo_briefing",
                  hour=7, days=["mon"]),
]

TASK_CALLBACKS = {"generate_ceo_briefing": generate_ceo_briefing}


def check_scheduled_tasks():
    now      = datetime.now(timezone.utc)
    day_name = now.strftime("%a").lower()
    for task in SCHEDULED_TASKS:
        if day_name not in task.days:
            continue
        if task.last_run and (now - task.last_run) < timedelta(hours=1):
            continue
        if now.hour != task.hour:
            continue
        callback = TASK_CALLBACKS.get(task.callback_name)
        if callback:
            logger.info(f"Running scheduled task: {task.name}")
            try:
                callback()
                task.last_run = now
                log_action("scheduled_task_ran", {"name": task.name})
            except Exception as exc:
                logger.error(f"Scheduled task '{task.name}' failed: {exc}")


# ── CLI + Main ─────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(description="AI Employee Orchestrator — Master process")
    parser.add_argument("--watchers", nargs="*", choices=list(WATCHER_REGISTRY.keys()),
                        default=list(WATCHER_REGISTRY.keys()),
                        help="Which Python watchers to start (default: all)")
    parser.add_argument("--no-watchers", action="store_true",
                        help="Don't start any watcher subprocesses (pm2 manages them)")
    parser.add_argument("--no-mcp-servers", action="store_true",
                        help="Don't start MCP servers (assume they're already running)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Log actions but don't invoke Claude or execute approvals")
    parser.add_argument("--poll-interval", type=int, default=POLL_INTERVAL)
    return parser.parse_args()


def main():
    global DRY_RUN, POLL_INTERVAL

    args = parse_args()
    if args.dry_run:
        DRY_RUN = True
    POLL_INTERVAL = args.poll_interval

    # Ensure vault directories exist
    for d in [NEEDS_ACTION, APPROVED, PENDING_APPROVAL, REJECTED, DONE, PLANS, LOGS, BRIEFINGS]:
        d.mkdir(parents=True, exist_ok=True)

    logger.info("=" * 64)
    logger.info("  AI Employee Orchestrator — Gold Tier")
    logger.info(f"  Vault:     {VAULT_PATH}")
    logger.info(f"  DRY_RUN:   {DRY_RUN}")
    logger.info(f"  Interval:  {POLL_INTERVAL}s")
    logger.info(f"  pm2:       {'available' if McpHealthChecker.is_pm2_available() else 'not found (subprocess mode)'}")
    logger.info("=" * 64)

    # ── Phase 1–2: Start MCP servers (unless skipped) ──────────────────────────
    mcp_manager = McpServerManager(MCP_SERVER_REGISTRY)
    if not args.no_mcp_servers:
        logger.info("Starting MCP servers (phased startup)...")
        results = mcp_manager.start_all_ordered()
        healthy = sum(1 for ok in results.values() if ok)
        logger.info(f"MCP servers: {healthy}/{len(results)} started successfully")
        log_action("mcp_servers_started", {
            "healthy": healthy,
            "total": len(results),
            "details": {k: ("ok" if v else "failed") for k, v in results.items()},
        })
    else:
        logger.info("Skipping MCP server startup (--no-mcp-servers)")
        # Run initial health check to populate status
        mcp_manager.check_health_all()

    # Wait for MCP servers to finish initialising before starting watchers
    time.sleep(STARTUP_PHASE_DELAY_S)

    # ── Phase 3–4: Start Python watchers ──────────────────────────────────────
    if args.no_watchers:
        enabled_watchers = {}
        logger.info("No watchers started (--no-watchers)")
    else:
        enabled_watchers = {k: v for k, v in WATCHER_REGISTRY.items() if k in args.watchers}
        for k, v in WATCHER_REGISTRY.items():
            if k not in args.watchers:
                v.enabled = False

    pm = ProcessManager(enabled_watchers)
    if enabled_watchers:
        pm.start_all()
        logger.info(f"Started {len(enabled_watchers)} watcher(s)")

    log_action("orchestrator_started", {
        "dry_run":    DRY_RUN,
        "watchers":   [v.name for v in enabled_watchers.values()],
        "mcp_count":  len(MCP_SERVER_REGISTRY),
        "poll_interval": POLL_INTERVAL,
        "pm2":        McpHealthChecker.is_pm2_available(),
    })

    # ── Signal handler ─────────────────────────────────────────────────────────
    shutdown = False

    def handle_signal(signum, frame):
        nonlocal shutdown
        logger.info(f"Signal {signum} received — shutting down...")
        shutdown = True

    signal.signal(signal.SIGINT,  handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # ── Main polling loop ──────────────────────────────────────────────────────
    cycle = 0
    while not shutdown:
        cycle += 1
        try:
            # 1. Watcher health
            if enabled_watchers:
                pm.check_health()

            # 2. MCP health (every MCP_HEALTH_EVERY cycles)
            if cycle % MCP_HEALTH_EVERY == 0:
                mcp_health = mcp_manager.check_health_all()
                down_servers = [k for k, s in mcp_health.items() if s == "down"]
                if down_servers:
                    logger.warning(f"MCP servers down: {down_servers}")

            # 3. Process /Needs_Action
            process_needs_action()

            # 4. Process /Approved
            process_approved()

            # 5. Scheduled tasks
            check_scheduled_tasks()

            # 6. Dashboard (every 4th cycle)
            if cycle % 4 == 0 or cycle == 1:
                watcher_status = pm.get_status() if enabled_watchers else None
                mcp_status     = mcp_manager.get_status()
                update_dashboard(watcher_status, mcp_status)

        except Exception as exc:
            logger.error(f"Orchestrator loop error: {exc}", exc_info=True)
            log_action("orchestrator_error", {"error": str(exc)})

        # Responsive sleep
        for _ in range(POLL_INTERVAL * 2):
            if shutdown:
                break
            time.sleep(0.5)

    # ── Graceful shutdown ──────────────────────────────────────────────────────
    logger.info("Shutting down orchestrator...")
    pm.stop_all()
    update_dashboard(pm.get_status() if enabled_watchers else None, mcp_manager.get_status())
    log_action("orchestrator_stopped", {})
    logger.info("Orchestrator stopped.")


if __name__ == "__main__":
    main()
