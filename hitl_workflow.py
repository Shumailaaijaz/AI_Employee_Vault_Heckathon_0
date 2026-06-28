"""HITL (Human-in-the-Loop) Workflow Module

This module provides:
1. ApprovalRequest - Create approval files in /Pending_Approval
2. ApprovalWatcher - Watch /Approved folder using watchdog
3. MCPExecutor - Execute MCP actions when approvals are detected

Usage:
    # Creating an approval request (called by Claude when sensitive action detected)
    from hitl_workflow import ApprovalRequest

    req = ApprovalRequest(
        action_type="email_send",
        title="Send email to client",
        details={"to": "client@example.com", "subject": "Invoice"},
        mcp_server="gmail-send",
        mcp_tool="send_email",
        mcp_args={"to": "client@example.com", "subject": "Invoice", "body": "..."}
    )
    req.save()  # Creates file in /Pending_Approval

    # Running the approval watcher (run as service)
    python hitl_workflow.py
"""

import os
import sys
import json
import subprocess
import shutil
import logging
import re
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import dataclass, field, asdict
from typing import Any
import time

from watchdog.observers.polling import PollingObserver
from watchdog.events import FileSystemEventHandler

# ── Config ────────────────────────────────────────────────────────────────────

VAULT_PATH = Path(os.getenv("VAULT_PATH", "/mnt/d/AI_Employee_Vault/AI_Employee_Vault"))
PENDING_APPROVAL = VAULT_PATH / "Pending_Approval"
APPROVED = VAULT_PATH / "Approved"
REJECTED = VAULT_PATH / "Rejected"
DONE = VAULT_PATH / "Done"
LOGS = VAULT_PATH / "Logs"

DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
MCP_CONFIG_PATH = VAULT_PATH / ".mcp.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("HITL")

# Ensure directories exist
for d in [PENDING_APPROVAL, APPROVED, REJECTED, DONE, LOGS]:
    d.mkdir(parents=True, exist_ok=True)


# ── Audit Logger ──────────────────────────────────────────────────────────────

def log_action(action_type: str, details: dict):
    """Append an entry to today's JSON log file."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log_file = LOGS / f"{today}.json"

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "actor": "hitl_workflow",
        "action_type": action_type,
        "details": details,
    }

    entries = []
    if log_file.exists():
        try:
            entries = json.loads(log_file.read_text())
        except (json.JSONDecodeError, ValueError):
            entries = []

    entries.append(entry)
    log_file.write_text(json.dumps(entries, indent=2))


# ── ApprovalRequest ───────────────────────────────────────────────────────────

@dataclass
class ApprovalRequest:
    """Represents a sensitive action requiring human approval.

    Attributes:
        action_type: Type of action (email_send, payment, social_post, etc.)
        title: Human-readable title for the approval
        details: Dict with action-specific details to display
        mcp_server: Name of MCP server to call (e.g., "gmail-send")
        mcp_tool: Name of MCP tool to invoke (e.g., "send_email")
        mcp_args: Arguments to pass to the MCP tool
        priority: "normal", "high", or "urgent"
        requester: Who/what requested this action
    """
    action_type: str
    title: str
    details: dict = field(default_factory=dict)
    mcp_server: str = ""
    mcp_tool: str = ""
    mcp_args: dict = field(default_factory=dict)
    priority: str = "normal"
    requester: str = "claude"

    def __post_init__(self):
        self.created_at = datetime.now(timezone.utc)
        self.id = self.created_at.strftime("%Y%m%d_%H%M%S")

    def to_markdown(self) -> str:
        """Generate markdown content for the approval file."""
        priority_badge = ""
        if self.priority == "urgent":
            priority_badge = "🔴 URGENT"
        elif self.priority == "high":
            priority_badge = "🟡 HIGH PRIORITY"

        details_md = "\n".join(f"- **{k}**: {v}" for k, v in self.details.items())

        mcp_section = ""
        if self.mcp_server and self.mcp_tool:
            mcp_section = f"""
## MCP Execution
```json
{{
  "server": "{self.mcp_server}",
  "tool": "{self.mcp_tool}",
  "args": {json.dumps(self.mcp_args, indent=4)}
}}
```
"""

        return f"""---
type: approval_request
action_type: {self.action_type}
mcp_server: {self.mcp_server}
mcp_tool: {self.mcp_tool}
priority: {self.priority}
requester: {self.requester}
created: {self.created_at.isoformat()}
status: pending
---

# {priority_badge} {self.title}

## Action Details
{details_md}
{mcp_section}
## Instructions
- **To Approve**: Move this file to `/Approved/`
- **To Reject**: Move this file to `/Rejected/`

## MCP Arguments
```json
{json.dumps(self.mcp_args, indent=2)}
```
"""

    def save(self) -> Path:
        """Save the approval request to /Pending_Approval."""
        safe_title = re.sub(r'[^a-zA-Z0-9_-]', '_', self.title)[:40]
        filename = f"APPROVAL_{self.action_type}_{safe_title}_{self.id}.md"
        filepath = PENDING_APPROVAL / filename

        filepath.write_text(self.to_markdown())

        log_action("approval_requested", {
            "file": filename,
            "action_type": self.action_type,
            "mcp_server": self.mcp_server,
            "mcp_tool": self.mcp_tool,
            "priority": self.priority,
        })

        logger.info(f"Approval request created: {filename}")
        return filepath


# ── ApprovalParser ────────────────────────────────────────────────────────────

def parse_approval_file(filepath: Path) -> dict | None:
    """Parse an approval file and extract MCP execution details."""
    try:
        content = filepath.read_text()

        # Extract YAML frontmatter
        data = {}
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                frontmatter = parts[1].strip()
                for line in frontmatter.split("\n"):
                    if ":" in line:
                        key, value = line.split(":", 1)
                        data[key.strip()] = value.strip()

        # Extract MCP args from JSON block
        mcp_args_match = re.search(r'## MCP Arguments\s*```json\s*(\{.*?\})\s*```', content, re.DOTALL)
        if mcp_args_match:
            try:
                data["mcp_args"] = json.loads(mcp_args_match.group(1))
            except json.JSONDecodeError:
                data["mcp_args"] = {}

        return data
    except Exception as e:
        logger.error(f"Failed to parse approval file {filepath}: {e}")
        return None


# ── MCP Executor ──────────────────────────────────────────────────────────────

class MCPExecutor:
    """Execute MCP tools via Claude Code CLI or direct subprocess."""

    def __init__(self):
        self.mcp_config = self._load_mcp_config()

    def _load_mcp_config(self) -> dict:
        """Load MCP server configuration."""
        if MCP_CONFIG_PATH.exists():
            try:
                return json.loads(MCP_CONFIG_PATH.read_text())
            except json.JSONDecodeError:
                logger.error("Invalid .mcp.json")
        return {"mcpServers": {}}

    def execute(self, server: str, tool: str, args: dict) -> dict:
        """Execute an MCP tool.

        Returns:
            {"success": bool, "result": str, "error": str}
        """
        if DRY_RUN:
            logger.info(f"[DRY RUN] Would execute MCP: {server}.{tool}({args})")
            log_action("mcp_dry_run", {"server": server, "tool": tool, "args": args})
            return {"success": True, "result": "[DRY RUN] Action simulated", "error": ""}

        # Check if server is configured
        servers = self.mcp_config.get("mcpServers", {})
        if server not in servers:
            error = f"MCP server '{server}' not found in .mcp.json"
            logger.error(error)
            return {"success": False, "result": "", "error": error}

        server_config = servers[server]

        # Build command to run MCP server with tool call
        # This uses the MCP server's stdin/stdout protocol
        try:
            # For now, we'll invoke Claude Code to execute the MCP tool
            # This ensures proper MCP protocol handling
            prompt = f"""Execute the MCP tool '{tool}' from server '{server}' with these arguments:
{json.dumps(args, indent=2)}

Use the mcp_{tool} tool to execute this action. Report the result."""

            result = subprocess.run(
                ["claude", "-p", prompt, "--no-input", "--max-turns", "1"],
                capture_output=True,
                text=True,
                timeout=60,
                cwd=str(VAULT_PATH),
            )

            if result.returncode == 0:
                log_action("mcp_executed", {
                    "server": server,
                    "tool": tool,
                    "args": args,
                    "stdout": result.stdout[:500],
                })
                return {"success": True, "result": result.stdout, "error": ""}
            else:
                log_action("mcp_error", {
                    "server": server,
                    "tool": tool,
                    "error": result.stderr[:500],
                })
                return {"success": False, "result": "", "error": result.stderr}

        except subprocess.TimeoutExpired:
            error = "MCP execution timed out"
            logger.error(error)
            return {"success": False, "result": "", "error": error}
        except FileNotFoundError:
            error = "Claude CLI not found. Install claude-code or set CLAUDE_CMD."
            logger.error(error)
            return {"success": False, "result": "", "error": error}
        except Exception as e:
            error = str(e)
            logger.error(f"MCP execution failed: {error}")
            return {"success": False, "result": "", "error": error}


# ── Approval Watcher ──────────────────────────────────────────────────────────

class ApprovalHandler(FileSystemEventHandler):
    """Handle files appearing in /Approved folder."""

    def __init__(self, executor: MCPExecutor):
        self.executor = executor
        self.processed: set[str] = set()

    def on_created(self, event):
        if event.is_directory:
            return
        self._handle_file(Path(event.src_path))

    def on_moved(self, event):
        """Handle files moved into /Approved."""
        if event.is_directory:
            return
        dest = Path(event.dest_path)
        if dest.parent == APPROVED:
            self._handle_file(dest)

    def _handle_file(self, filepath: Path):
        """Process an approved file."""
        if not filepath.suffix == ".md":
            return
        if str(filepath) in self.processed:
            return

        self.processed.add(str(filepath))
        logger.info(f"Approved file detected: {filepath.name}")

        # Parse the approval file
        data = parse_approval_file(filepath)
        if not data:
            logger.error(f"Could not parse approval file: {filepath.name}")
            return

        mcp_server = data.get("mcp_server", "")
        mcp_tool = data.get("mcp_tool", "")
        mcp_args = data.get("mcp_args", {})

        if not mcp_server or not mcp_tool:
            logger.warning(f"No MCP action defined in {filepath.name}")
            self._move_to_done(filepath, "no_action")
            return

        # Execute MCP action
        logger.info(f"Executing MCP: {mcp_server}.{mcp_tool}")
        result = self.executor.execute(mcp_server, mcp_tool, mcp_args)

        if result["success"]:
            logger.info(f"MCP action succeeded: {filepath.name}")
            self._move_to_done(filepath, "success")
        else:
            logger.error(f"MCP action failed: {result['error']}")
            self._move_to_done(filepath, "failed", result["error"])

    def _move_to_done(self, filepath: Path, status: str, error: str = ""):
        """Move processed file to /Done with timestamp."""
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        done_name = f"{ts}_{status}_{filepath.name}"
        done_path = DONE / done_name

        try:
            shutil.move(str(filepath), str(done_path))
            log_action("approval_processed", {
                "file": filepath.name,
                "status": status,
                "moved_to": done_name,
                "error": error,
            })
            logger.info(f"Moved to Done: {done_name}")
        except Exception as e:
            logger.error(f"Failed to move file to Done: {e}")


class ApprovalWatcher:
    """Watch /Approved folder for new approvals."""

    def __init__(self):
        self.executor = MCPExecutor()
        self.handler = ApprovalHandler(self.executor)
        # Use PollingObserver for WSL2 compatibility
        self.observer = PollingObserver(timeout=3)

    def start(self):
        """Start watching the /Approved folder."""
        logger.info(f"Starting ApprovalWatcher on {APPROVED}")
        logger.info(f"DRY_RUN: {DRY_RUN}")

        # Process any existing files first
        self._process_existing()

        # Start watching
        self.observer.schedule(self.handler, str(APPROVED), recursive=False)
        self.observer.start()

        log_action("approval_watcher_started", {"path": str(APPROVED)})

        try:
            while True:
                # Also poll for files (backup for watchdog)
                self._process_existing()
                time.sleep(5)
        except KeyboardInterrupt:
            logger.info("Shutting down ApprovalWatcher...")
            self.observer.stop()

        self.observer.join()
        log_action("approval_watcher_stopped", {})

    def _process_existing(self):
        """Process any existing files in /Approved."""
        for f in APPROVED.iterdir():
            if f.is_file() and f.suffix == ".md":
                self.handler._handle_file(f)


# ── CLI ───────────────────────────────────────────────────────────────────────

def create_sample_approval():
    """Create a sample approval request for testing."""
    req = ApprovalRequest(
        action_type="email_send",
        title="Test Email to Client",
        details={
            "to": "test@example.com",
            "subject": "Test Subject",
            "body_preview": "This is a test email...",
        },
        mcp_server="gmail-send",
        mcp_tool="send_email",
        mcp_args={
            "to": "test@example.com",
            "subject": "Test Subject",
            "body": "This is a test email body.",
        },
        priority="normal",
    )
    filepath = req.save()
    print(f"Sample approval created: {filepath}")
    print(f"Move it to /Approved to trigger execution.")


def main():
    import argparse

    parser = argparse.ArgumentParser(description="HITL Workflow Manager")
    parser.add_argument("command", nargs="?", default="watch",
                       choices=["watch", "sample", "process"],
                       help="Command to run (default: watch)")
    parser.add_argument("--dry-run", action="store_true",
                       help="Enable dry-run mode")

    args = parser.parse_args()

    global DRY_RUN
    if args.dry_run:
        DRY_RUN = True

    if args.command == "watch":
        print("=" * 50)
        print("  HITL Approval Watcher")
        print(f"  Watching: {APPROVED}")
        print(f"  DRY_RUN: {DRY_RUN}")
        print("=" * 50)
        watcher = ApprovalWatcher()
        watcher.start()

    elif args.command == "sample":
        create_sample_approval()

    elif args.command == "process":
        # One-shot: process all approved files and exit
        watcher = ApprovalWatcher()
        watcher._process_existing()
        print("Processed all approved files.")


if __name__ == "__main__":
    main()
