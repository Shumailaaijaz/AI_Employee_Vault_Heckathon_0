"""Base Watcher - Template for all AI Employee watchers.

All watchers inherit from this class and implement:
  - check_for_updates(): Return list of new items to process
  - create_action_file(): Create .md file in Needs_Action folder

Error recovery is provided automatically via error_recovery.ErrorRecovery:
  - check_for_updates() is called with 3-attempt exponential backoff
  - create_action_file() is retried twice; failures are queued to Logs/failed_tasks/
  - A CircuitBreaker tracks consecutive failures per watcher
  - After cb_threshold failures the watcher pauses and writes a human alert
  - Pending queued tasks are retried at the start of each cycle
"""

import time
import json
import logging
from pathlib import Path
from abc import ABC, abstractmethod
from datetime import datetime, timezone

from error_recovery import ErrorRecovery, RetryConfig


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


class BaseWatcher(ABC):
    def __init__(self, vault_path: str, check_interval: int = 60):
        self.vault_path     = Path(vault_path)
        self.needs_action   = self.vault_path / "Needs_Action"
        self.logs_dir       = self.vault_path / "Logs"
        self.check_interval = check_interval
        self.logger         = logging.getLogger(self.__class__.__name__)

        # Ensure required directories exist
        self.needs_action.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)

        # Unified error recovery (retry + circuit-breaker + queue + alerts)
        self.recovery = ErrorRecovery(
            vault_path = self.vault_path,
            component  = self.__class__.__name__,
            cb_failure_threshold = 5,
            cb_failure_window    = 300.0,   # 5 min rolling window
            cb_recovery_timeout  = 600.0,   # 10 min before probing
            alert_dedup_window   = 300.0,   # max 1 alert per 5 min per level
        )

    @abstractmethod
    def check_for_updates(self) -> list:
        """Return list of new items to process."""
        pass

    @abstractmethod
    def create_action_file(self, item) -> Path:
        """Create .md file in Needs_Action folder for a detected item."""
        pass

    def log_action(self, action_type: str, details: dict):
        """Append an action entry to today's log file."""
        today    = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        log_file = self.logs_dir / f"{today}.json"

        entry = {
            "timestamp":   datetime.now(timezone.utc).isoformat(),
            "watcher":     self.__class__.__name__,
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

    def run(self):
        """
        Main loop — check for updates and create action files.

        Error recovery per cycle:
          1. Retry pending queued tasks from previous failures.
          2. Call check_for_updates() with up to 3 retries + backoff.
             If circuit is OPEN the check is skipped (returns []).
          3. For each item, call create_action_file() with up to 2 retries.
             Persistent failures are queued to Logs/failed_tasks/.
          4. Consecutive failures accumulate in the CircuitBreaker; after
             cb_failure_threshold failures a human alert is written and the
             watcher enters degraded (fast-fail) mode for cb_recovery_timeout.
        """
        self.logger.info(
            "Starting %s (interval: %ds, vault: %s, circuit: %s, queued: %d)",
            self.__class__.__name__,
            self.check_interval,
            self.vault_path,
            self.recovery.circuit_state,
            self.recovery.queued_count,
        )

        while True:
            try:
                # ── 1. Retry previously queued failures ───────────────────────
                if self.recovery.queued_count > 0:
                    self.logger.info(
                        "Retrying %d queued task(s)…", self.recovery.queued_count
                    )
                    ok, fail = self.recovery.retry_queued(
                        lambda payload: self.create_action_file(payload.get("item", payload))
                    )
                    if ok or fail:
                        self.log_action("queued_retry", {"ok": ok, "fail": fail})

                # ── 2. Fetch new items (with retry + circuit-breaker) ──────────
                items = self.recovery.call(
                    self.check_for_updates,
                    operation   = "check_for_updates",
                    config      = RetryConfig(
                        max_attempts     = 3,
                        base_delay       = 5.0,
                        max_delay        = 30.0,
                        exponential_base = 2.0,
                    ),
                    fallback    = [],
                    queue_on_fail = False,  # polling has no payload to queue
                    alert_on_fail = True,
                )

                if items:
                    self.logger.info("Found %d new item(s)", len(items))

                # ── 3. Create action files (with retry + queue on failure) ──────
                for item in (items or []):
                    filepath = self.recovery.call(
                        self.create_action_file,
                        item,
                        operation    = "create_action_file",
                        config       = RetryConfig(
                            max_attempts     = 2,
                            base_delay       = 3.0,
                            max_delay        = 15.0,
                        ),
                        fallback     = Path(),
                        queue_on_fail = True,
                        alert_on_fail = False,   # only alert when CB opens
                        payload      = {"item": str(item)},
                    )
                    if filepath and filepath.exists():
                        self.log_action(
                            "action_file_created",
                            {"file": str(filepath), "source": str(item)},
                        )

            except KeyboardInterrupt:
                self.logger.info("Shutting down gracefully…")
                break

            except Exception as exc:
                # Unexpected error outside the recovery wrappers
                self.logger.error("Unhandled error in watch loop: %s", exc, exc_info=True)
                self.log_action("error", {"error": str(exc)})

            time.sleep(self.check_interval)
