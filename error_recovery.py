"""error_recovery.py — Unified Error Recovery for AI Employee Vault
====================================================================

Provides a single import point for all retry, circuit-breaker, task queuing,
and human alerting logic used across watchers and MCP calls.

Exports
-------
RetryConfig         — configure retry behaviour (attempts, delays, jitter)
MaxRetriesExceeded  — raised when all retry attempts are exhausted
with_retry()        — standalone function: call fn with exponential backoff
CircuitBreaker      — per-component CLOSED/OPEN/HALF_OPEN state machine
TaskQueue           — persist failed tasks to Logs/failed_tasks/ for later retry
HumanAlertWriter    — write alert/approval files to Needs_Action/Pending_Approval
ErrorRecovery       — top-level façade combining all of the above

Usage (typical watcher)
-----------------------
    from error_recovery import ErrorRecovery, RetryConfig

    class MyWatcher(BaseWatcher):
        def __init__(self, vault_path):
            super().__init__(vault_path)
            # self.recovery is provided by BaseWatcher after this module is integrated

        def check_for_updates(self):
            return self.recovery.retry(
                self._do_api_call,
                config=RetryConfig(max_attempts=3, base_delay=5),
                label="MyAPI",
                fallback=[],
            )

Usage (standalone, e.g. orchestrator)
--------------------------------------
    from error_recovery import with_retry, RetryConfig

    result = with_retry(
        subprocess.run, cmd,
        config=RetryConfig(max_attempts=2, base_delay=3),
        label="claude-subprocess",
    )

Cron / pm2 note
---------------
All state files (circuit-breaker JSON, failed-task JSON) live inside the vault,
so they survive process restarts and pm2 auto-restarts.
"""

from __future__ import annotations

import json
import logging
import math
import os
import random
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("error_recovery")

# ── Sentinel for "no fallback supplied" ───────────────────────────────────────

_UNSET = object()


# ── RetryConfig ───────────────────────────────────────────────────────────────

@dataclass
class RetryConfig:
    """
    Parameters that control retry behaviour for a single call site.

    Delay formula (attempt is 0-indexed):
        delay = min(base_delay * exponential_base**attempt, max_delay)
                + random.uniform(-jitter, jitter)
    """
    max_attempts:     int   = 3
    base_delay:       float = 2.0    # seconds before first retry
    max_delay:        float = 60.0   # cap on any single wait
    exponential_base: float = 2.0    # multiplier per attempt
    jitter:           float = 0.5    # random ±jitter added to each delay

    # Tuples of exception types to retry (empty = retry any Exception)
    retryable_excs:     tuple = ()
    # Tuples of exception types that are NEVER retried (raise immediately)
    not_retryable_excs: tuple = ()

    def calc_delay(self, attempt: int) -> float:
        """Return wait time (seconds) for the given 0-indexed attempt number."""
        raw = self.base_delay * (self.exponential_base ** attempt)
        capped = min(raw, self.max_delay)
        noise = random.uniform(-self.jitter, self.jitter)
        return max(0.0, capped + noise)


# ── Exceptions ────────────────────────────────────────────────────────────────

class MaxRetriesExceeded(Exception):
    """Raised when all retry attempts for a call are exhausted."""
    def __init__(self, message: str, last_exc: BaseException | None = None):
        super().__init__(message)
        self.last_exc = last_exc


class CircuitOpenError(Exception):
    """Raised when a call is blocked by an open circuit breaker."""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_retry_after(exc: BaseException) -> float | None:
    """
    Try to extract a Retry-After value (seconds) from an exception.
    Handles: requests.HTTPError, tweepy.TooManyRequests, googleapiclient HttpError,
    and any exception whose .response has a 'retry-after' / 'Retry-After' header.
    """
    response = getattr(exc, "response", None)
    if response is None:
        return None

    # requests / tweepy: response.headers is a CaseInsensitiveDict
    headers = getattr(response, "headers", {}) or {}
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if raw is None:
        # googleapiclient HttpError uses resp attribute
        resp = getattr(response, "resp", None) or {}
        raw = (resp.get("retry-after") or resp.get("Retry-After")) if isinstance(resp, dict) else None
    if raw is None:
        return None
    try:
        return float(raw)
    except (ValueError, TypeError):
        return None


def _is_retryable(exc: BaseException, config: RetryConfig) -> bool:
    """Return True if this exception should trigger a retry per config."""
    if config.not_retryable_excs and isinstance(exc, config.not_retryable_excs):
        return False
    if config.retryable_excs:
        return isinstance(exc, config.retryable_excs)
    return True   # default: retry any exception


# ── with_retry ────────────────────────────────────────────────────────────────

def with_retry(
    fn: Callable,
    *args,
    config: RetryConfig | None = None,
    label: str = "",
    fallback: Any = _UNSET,
    **kwargs,
) -> Any:
    """
    Call ``fn(*args, **kwargs)`` with exponential backoff retry.

    Parameters
    ----------
    fn       : callable to invoke
    config   : RetryConfig (defaults to RetryConfig())
    label    : human-readable name for log messages
    fallback : value to return on final failure instead of raising.
               If not supplied, MaxRetriesExceeded is raised.

    Returns
    -------
    fn's return value, or ``fallback`` if all retries are exhausted.

    Raises
    ------
    MaxRetriesExceeded  if no fallback and all attempts fail.
    """
    cfg = config or RetryConfig()
    last_exc: BaseException | None = None
    name = label or getattr(fn, "__name__", str(fn))

    for attempt in range(cfg.max_attempts):
        try:
            return fn(*args, **kwargs)

        except BaseException as exc:
            # KeyboardInterrupt / SystemExit — never retry
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise

            if not _is_retryable(exc, cfg):
                log.debug("[%s] non-retryable %s — raising immediately", name, type(exc).__name__)
                raise

            last_exc = exc
            remaining = cfg.max_attempts - attempt - 1

            if remaining == 0:
                break   # no more attempts; fall through to failure handling

            # Honour server's Retry-After if present
            retry_after = _get_retry_after(exc)
            delay = cfg.calc_delay(attempt)
            if retry_after is not None:
                delay = max(delay, min(float(retry_after), cfg.max_delay))

            log.warning(
                "[%s] attempt %d/%d failed (%s: %s) — retrying in %.1fs",
                name, attempt + 1, cfg.max_attempts,
                type(exc).__name__, str(exc)[:120], delay,
            )
            time.sleep(delay)

    # All attempts exhausted
    msg = f"[{name}] failed after {cfg.max_attempts} attempt(s): {last_exc}"
    log.error(msg)
    if fallback is not _UNSET:
        return fallback
    raise MaxRetriesExceeded(msg, last_exc=last_exc)


# ── CircuitBreaker ────────────────────────────────────────────────────────────

class CircuitBreaker:
    """
    Per-component state machine: CLOSED → OPEN → HALF_OPEN → CLOSED.

    CLOSED    Normal operation.  Failures are counted.
    OPEN      All calls are fast-failed (returns fallback / raises CircuitOpenError).
              After ``recovery_timeout`` seconds the breaker moves to HALF_OPEN.
    HALF_OPEN One probe call is allowed.
              Success → CLOSED.  Failure → OPEN (reset timer).

    State is persisted to ``{vault}/.circuit_breakers.json`` so it survives
    pm2 restarts.

    Parameters
    ----------
    component          : unique name (e.g. "GmailWatcher")
    vault_path         : Path to the Obsidian vault root
    failure_threshold  : failures within failure_window seconds → OPEN
    failure_window     : rolling window for counting failures (seconds)
    recovery_timeout   : seconds in OPEN state before trying HALF_OPEN
    """

    CLOSED    = "CLOSED"
    OPEN      = "OPEN"
    HALF_OPEN = "HALF_OPEN"

    _STATE_FILE_NAME = ".circuit_breakers.json"

    def __init__(
        self,
        component: str,
        vault_path: Path,
        failure_threshold: int   = 5,
        failure_window:    float = 300.0,   # 5 minutes
        recovery_timeout:  float = 600.0,   # 10 minutes
    ):
        self.component         = component
        self.vault_path        = Path(vault_path)
        self.failure_threshold = failure_threshold
        self.failure_window    = failure_window
        self.recovery_timeout  = recovery_timeout

        self._state_file = self.vault_path / self._STATE_FILE_NAME
        self._state:        str             = self.CLOSED
        self._failure_times: list[float]    = []   # timestamps
        self._opened_at:    float | None    = None
        self._probe_in_flight: bool         = False

        self._load_state()

    # ── State persistence ─────────────────────────────────────────────────────

    def _load_state(self) -> None:
        try:
            if not self._state_file.exists():
                return
            all_states: dict = json.loads(self._state_file.read_text())
            s = all_states.get(self.component, {})
            self._state         = s.get("state", self.CLOSED)
            self._failure_times = s.get("failure_times", [])
            self._opened_at     = s.get("opened_at")
            self._probe_in_flight = False  # always reset on restart
        except (json.JSONDecodeError, OSError):
            pass

    def _save_state(self) -> None:
        try:
            all_states: dict = {}
            if self._state_file.exists():
                try:
                    all_states = json.loads(self._state_file.read_text())
                except (json.JSONDecodeError, OSError):
                    all_states = {}
            all_states[self.component] = {
                "state":         self._state,
                "failure_times": self._failure_times[-50:],  # keep last 50
                "opened_at":     self._opened_at,
            }
            self._state_file.write_text(json.dumps(all_states, indent=2))
        except OSError:
            pass

    # ── Public interface ──────────────────────────────────────────────────────

    @property
    def state(self) -> str:
        self._maybe_transition()
        return self._state

    def record_success(self) -> None:
        if self._state in (self.HALF_OPEN, self.OPEN):
            log.info("[CircuitBreaker:%s] probe succeeded → CLOSED", self.component)
        self._state           = self.CLOSED
        self._failure_times   = []
        self._opened_at       = None
        self._probe_in_flight = False
        self._save_state()

    def record_failure(self, exc: BaseException | None = None) -> None:
        now = time.monotonic()
        self._failure_times.append(now)
        # Evict old failures outside the rolling window
        cutoff = now - self.failure_window
        self._failure_times = [t for t in self._failure_times if t >= cutoff]

        if self._state == self.HALF_OPEN:
            log.warning(
                "[CircuitBreaker:%s] probe failed → reopening circuit (%s)",
                self.component, type(exc).__name__ if exc else "unknown",
            )
            self._state          = self.OPEN
            self._opened_at      = now
            self._probe_in_flight = False
            self._save_state()
            return

        if len(self._failure_times) >= self.failure_threshold and self._state == self.CLOSED:
            log.error(
                "[CircuitBreaker:%s] %d failures in %.0fs — OPENING circuit",
                self.component, len(self._failure_times), self.failure_window,
            )
            self._state     = self.OPEN
            self._opened_at = now
        self._save_state()

    def is_open(self) -> bool:
        return self.state == self.OPEN

    def allows_call(self) -> bool:
        """True if the circuit allows a call right now."""
        s = self.state
        if s == self.CLOSED:
            return True
        if s == self.HALF_OPEN and not self._probe_in_flight:
            self._probe_in_flight = True
            return True
        return False

    def call(
        self,
        fn: Callable,
        *args,
        fallback: Any = _UNSET,
        **kwargs,
    ) -> Any:
        """
        Execute fn through the circuit breaker.
        If OPEN returns fallback (or raises CircuitOpenError).
        Wraps success/failure recording automatically.
        """
        if not self.allows_call():
            msg = f"[CircuitBreaker:{self.component}] circuit OPEN — fast-failing"
            log.warning(msg)
            if fallback is not _UNSET:
                return fallback
            raise CircuitOpenError(msg)

        try:
            result = fn(*args, **kwargs)
            self.record_success()
            return result
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:
            self.record_failure(exc)
            raise

    # ── Internal ──────────────────────────────────────────────────────────────

    def _maybe_transition(self) -> None:
        """Check if enough time has passed to move from OPEN → HALF_OPEN."""
        if self._state == self.OPEN and self._opened_at is not None:
            elapsed = time.monotonic() - self._opened_at
            if elapsed >= self.recovery_timeout:
                log.info(
                    "[CircuitBreaker:%s] %.0fs elapsed → HALF_OPEN (probing)",
                    self.component, elapsed,
                )
                self._state           = self.HALF_OPEN
                self._probe_in_flight = False
                self._save_state()


# ── TaskQueue ─────────────────────────────────────────────────────────────────

class TaskQueue:
    """
    Persists failed tasks as JSON files in ``Logs/failed_tasks/`` so they
    can be retried on the next cycle or by a recovery job.

    Each task file is named ``{timestamp}_{component}_{operation}_{uuid}.json``.

    Status lifecycle:  pending → retrying → done | permanent_failure
    """

    _DIR = "Logs/failed_tasks"
    _STATUS_PENDING   = "pending"
    _STATUS_RETRYING  = "retrying"
    _STATUS_DONE      = "done"
    _STATUS_PERMANENT = "permanent_failure"

    def __init__(self, vault_path: Path):
        self.queue_dir = Path(vault_path) / self._DIR
        self.queue_dir.mkdir(parents=True, exist_ok=True)

    # ── Write ─────────────────────────────────────────────────────────────────

    def queue_failed(
        self,
        component: str,
        operation: str,
        payload: dict,
        error: str,
        attempt_count: int = 1,
    ) -> str:
        """Persist a failed task; return its task_id."""
        task_id = uuid.uuid4().hex[:12]
        now     = datetime.now(timezone.utc)
        stamp   = now.strftime("%Y%m%dT%H%M%S")
        safe_op = re.sub(r"[^a-z0-9_]", "_", operation.lower())[:30]
        filename = f"{stamp}_{component}_{safe_op}_{task_id}.json"

        record = {
            "task_id":      task_id,
            "component":    component,
            "operation":    operation,
            "payload":      payload,
            "error":        error[:500],
            "timestamp":    now.isoformat(),
            "attempt_count": attempt_count,
            "status":       self._STATUS_PENDING,
        }
        path = self.queue_dir / filename
        path.write_text(json.dumps(record, indent=2, default=str))
        log.info("[TaskQueue] queued failed task %s → %s", task_id, filename)
        return task_id

    # ── Read ──────────────────────────────────────────────────────────────────

    def get_pending(self, component: str | None = None) -> list[dict]:
        """Return all pending tasks, optionally filtered by component."""
        tasks = []
        for p in sorted(self.queue_dir.glob("*.json")):
            try:
                record = json.loads(p.read_text())
                if record.get("status") not in (self._STATUS_PENDING, self._STATUS_RETRYING):
                    continue
                if component and record.get("component") != component:
                    continue
                record["_path"] = str(p)
                tasks.append(record)
            except (json.JSONDecodeError, OSError):
                continue
        return tasks

    def count_pending(self, component: str | None = None) -> int:
        return len(self.get_pending(component))

    # ── Update ────────────────────────────────────────────────────────────────

    def mark_done(self, task_id: str) -> None:
        self._update_status(task_id, self._STATUS_DONE)

    def mark_permanent_failure(self, task_id: str) -> None:
        self._update_status(task_id, self._STATUS_PERMANENT)

    def mark_retrying(self, task_id: str) -> None:
        self._update_status(task_id, self._STATUS_RETRYING)

    def _update_status(self, task_id: str, new_status: str) -> None:
        for p in self.queue_dir.glob(f"*{task_id}*.json"):
            try:
                record = json.loads(p.read_text())
                record["status"] = new_status
                record["updated"] = datetime.now(timezone.utc).isoformat()
                p.write_text(json.dumps(record, indent=2, default=str))
                return
            except (json.JSONDecodeError, OSError):
                continue

    # ── Retry ─────────────────────────────────────────────────────────────────

    def retry_pending(
        self,
        fn: Callable[[dict], Any],
        component: str | None = None,
        max_retries_per_task: int = 3,
    ) -> tuple[int, int]:
        """
        Attempt to retry all pending tasks by calling ``fn(payload)``.

        Returns (success_count, permanent_failure_count).
        """
        tasks = self.get_pending(component)
        if not tasks:
            return 0, 0
        log.info("[TaskQueue] retrying %d queued task(s)%s",
                 len(tasks), f" for {component}" if component else "")
        successes = failures = 0
        for task in tasks:
            task_id     = task["task_id"]
            attempt_cnt = task.get("attempt_count", 1)
            if attempt_cnt >= max_retries_per_task:
                log.warning("[TaskQueue] task %s reached max retries — marking permanent", task_id)
                self.mark_permanent_failure(task_id)
                failures += 1
                continue
            self.mark_retrying(task_id)
            try:
                fn(task["payload"])
                self.mark_done(task_id)
                log.info("[TaskQueue] task %s succeeded on retry %d", task_id, attempt_cnt + 1)
                successes += 1
            except Exception as exc:
                log.warning("[TaskQueue] retry %d failed for task %s: %s",
                            attempt_cnt + 1, task_id, exc)
                self._update_status(task_id, self._STATUS_PENDING)
                # bump attempt count
                for p in self.queue_dir.glob(f"*{task_id}*.json"):
                    try:
                        record = json.loads(p.read_text())
                        record["attempt_count"] = attempt_cnt + 1
                        record["last_error"] = str(exc)[:300]
                        p.write_text(json.dumps(record, indent=2, default=str))
                    except (json.JSONDecodeError, OSError):
                        pass
                failures += 1
        return successes, failures


# ── HumanAlertWriter ──────────────────────────────────────────────────────────

class HumanAlertWriter:
    """
    Writes structured alert and approval-request files into the vault so the
    orchestrator (and the human reviewing Obsidian) sees failures quickly.

    Alert files  → ``Needs_Action/SYSTEM_ALERT_{component}_{stamp}.md``
    Approval files → ``Pending_Approval/APPROVAL_RETRY_{component}_{stamp}.md``

    Deduplication prevents flooding: the same (component, level) pair is only
    written once per ``dedup_window`` seconds.
    """

    def __init__(self, vault_path: Path, dedup_window: float = 300.0):
        self.vault_path       = Path(vault_path)
        self.dedup_window     = dedup_window
        self._written:        dict[str, float] = {}   # key → monotonic timestamp
        self.needs_action     = self.vault_path / "Needs_Action"
        self.pending_approval = self.vault_path / "Pending_Approval"
        self.needs_action.mkdir(parents=True, exist_ok=True)
        self.pending_approval.mkdir(parents=True, exist_ok=True)

    def _dedup_key(self, prefix: str, component: str, level: str) -> str:
        return f"{prefix}:{component}:{level}"

    def _should_write(self, key: str) -> bool:
        last = self._written.get(key)
        now  = time.monotonic()
        if last is not None and (now - last) < self.dedup_window:
            return False
        self._written[key] = now
        return True

    # ── Alert file ────────────────────────────────────────────────────────────

    def write_alert(
        self,
        component: str,
        level: str,                    # ERROR | WARNING | INFO
        message: str,
        error: str = "",
        action: str = "",
        operation: str = "",
    ) -> Path | None:
        """
        Write a SYSTEM_ALERT_*.md to Needs_Action/.
        Returns the written path or None if deduped / INFO level.
        """
        if level == "INFO":
            return None
        key = self._dedup_key("alert", component, level)
        if not self._should_write(key):
            log.debug("[HumanAlert] deduped alert for %s/%s", component, level)
            return None

        now     = datetime.now(timezone.utc)
        stamp   = now.strftime("%Y%m%dT%H%M%S")
        filename = f"SYSTEM_ALERT_{component}_{stamp}.md"
        path     = self.needs_action / filename

        emoji = {"ERROR": "🔴", "WARNING": "🟡"}.get(level, "⚪")
        content = (
            f"---\n"
            f"type: system_alert\n"
            f"level: {level}\n"
            f"component: {component}\n"
            f"operation: {operation or 'unknown'}\n"
            f"generated: {now.isoformat()}\n"
            f"---\n\n"
            f"# {emoji} System Alert: {level} — {component}\n\n"
            f"**Message**: {message}\n\n"
            + (f"**Error**: `{error[:400]}`\n\n" if error else "")
            + (f"**Operation**: `{operation}`\n\n" if operation else "")
            + (f"## Recommended Action\n\n{action}\n\n" if action else "")
            + f"---\n*Auto-generated by error_recovery.py · {now.strftime('%Y-%m-%d %H:%M UTC')}*\n"
        )
        try:
            path.write_text(content, encoding="utf-8")
            log.warning("[HumanAlert] wrote %s alert → %s", level, filename)
        except OSError as exc:
            log.error("[HumanAlert] could not write alert file: %s", exc)
            return None
        return path

    # ── Approval request file ─────────────────────────────────────────────────

    def write_approval_request(
        self,
        component: str,
        operation: str,
        payload: dict,
        error: str,
        action: str = "",
    ) -> Path | None:
        """
        Write an APPROVAL_RETRY_*.md to Pending_Approval/.
        Used when a failed task should be reviewed by the human before retrying.
        Returns the written path or None if deduped.
        """
        key = self._dedup_key("approval", component, operation)
        if not self._should_write(key):
            return None

        now      = datetime.now(timezone.utc)
        stamp    = now.strftime("%Y%m%dT%H%M%S")
        safe_op  = re.sub(r"[^a-z0-9_]", "_", operation.lower())[:30]
        filename = f"APPROVAL_RETRY_{component}_{safe_op}_{stamp}.md"
        path     = self.pending_approval / filename

        payload_str = json.dumps(payload, indent=2, default=str)[:800]
        content = (
            f"---\n"
            f"type: approval_retry\n"
            f"component: {component}\n"
            f"operation: {operation}\n"
            f"generated: {now.isoformat()}\n"
            f"---\n\n"
            f"# Approval Required: Retry `{operation}` in `{component}`\n\n"
            f"An operation failed and requires human review before retry.\n\n"
            f"**Component**: `{component}`\n"
            f"**Operation**: `{operation}`\n"
            f"**Error**: `{error[:400]}`\n\n"
            f"## Payload\n\n```json\n{payload_str}\n```\n\n"
            + (f"## Suggested Action\n\n{action}\n\n" if action else "")
            + f"## How to Proceed\n\n"
              f"- Move this file to `/Approved` to retry the operation automatically.\n"
              f"- Move to `/Rejected` to abandon it permanently.\n\n"
            f"---\n*Auto-generated by error_recovery.py · {now.strftime('%Y-%m-%d %H:%M UTC')}*\n"
        )
        try:
            path.write_text(content, encoding="utf-8")
            log.info("[HumanAlert] wrote approval request → %s", filename)
        except OSError as exc:
            log.error("[HumanAlert] could not write approval file: %s", exc)
            return None
        return path

    # ── Circuit open notification ─────────────────────────────────────────────

    def write_circuit_open_alert(self, component: str, failure_count: int) -> Path | None:
        return self.write_alert(
            component=component,
            level="ERROR",
            message=(
                f"`{component}` circuit breaker OPENED after {failure_count} consecutive "
                "failures. All calls to this component are now fast-failing."
            ),
            action=(
                f"1. Check `pm2 logs {component.lower().replace('watcher', 'watcher-')}` "
                "for the root cause.\n"
                f"2. Fix the underlying issue (credentials, network, API quota).\n"
                f"3. Run `pm2 restart {component.lower()}` to reset the circuit breaker.\n"
                f"4. Or wait for the automatic recovery probe (~10 minutes)."
            ),
            operation="circuit_breaker",
        )


# ── ErrorRecovery ─────────────────────────────────────────────────────────────

class ErrorRecovery:
    """
    Top-level façade that wires together with_retry, CircuitBreaker,
    TaskQueue, and HumanAlertWriter into a single easy-to-use object.

    Instantiate once per component (watcher, MCP server, etc.).

        recovery = ErrorRecovery(vault_path=VAULT_PATH, component="GmailWatcher")

    Then use:

        # Retry with fallback — no alert, no queue
        result = recovery.retry(fn, arg1, config=RetryConfig(3, 5), fallback=[])

        # Full recovery: retry + queue on failure + write alert
        result = recovery.call(
            fn, arg1,
            operation="fetch_emails",
            config=RetryConfig(3, 5),
            fallback=[],
            queue_on_fail=True,
            alert_on_fail=True,
            payload={"arg": arg1},
        )

        # Retry queued tasks from a previous failed run
        recovery.retry_queued(fn)
    """

    def __init__(
        self,
        vault_path: str | Path,
        component:  str,
        cb_failure_threshold: int   = 5,
        cb_failure_window:    float = 300.0,
        cb_recovery_timeout:  float = 600.0,
        alert_dedup_window:   float = 300.0,
    ):
        self.component   = component
        self.vault_path  = Path(vault_path)
        self.circuit     = CircuitBreaker(
            component, self.vault_path,
            failure_threshold = cb_failure_threshold,
            failure_window    = cb_failure_window,
            recovery_timeout  = cb_recovery_timeout,
        )
        self.queue   = TaskQueue(self.vault_path)
        self.alerter = HumanAlertWriter(self.vault_path, dedup_window=alert_dedup_window)

    # ── Core call method ──────────────────────────────────────────────────────

    def call(
        self,
        fn: Callable,
        *args,
        operation:    str               = "",
        config:       RetryConfig | None = None,
        fallback:     Any               = _UNSET,
        queue_on_fail: bool             = False,
        alert_on_fail: bool             = True,
        payload:      dict | None       = None,
        **kwargs,
    ) -> Any:
        """
        Execute fn through the full error recovery pipeline:

          1. Circuit-breaker check (if OPEN → return fallback / raise)
          2. Retry with exponential backoff
          3. On final failure:
             a. Queue task if queue_on_fail=True
             b. Write human alert if alert_on_fail=True
             c. Return fallback if provided, else re-raise

        Parameters
        ----------
        fn           : function to call
        operation    : human-readable name for alerts and queue records
        config       : RetryConfig (defaults to RetryConfig())
        fallback     : value to return on exhausted retries (instead of raising)
        queue_on_fail: persist the failed call to TaskQueue for later retry
        alert_on_fail: write a Needs_Action alert file
        payload      : serialisable dict to store in the queue record
        """
        name = operation or getattr(fn, "__name__", str(fn))
        cfg  = config or RetryConfig()

        # 1. Circuit breaker check
        if not self.circuit.allows_call():
            msg = f"[{self.component}:{name}] circuit OPEN — fast-failing"
            log.warning(msg)
            if fallback is not _UNSET:
                return fallback
            raise CircuitOpenError(msg)

        # 2. Retry loop
        last_exc: BaseException | None = None
        for attempt in range(cfg.max_attempts):
            try:
                result = fn(*args, **kwargs)
                self.circuit.record_success()
                return result

            except (KeyboardInterrupt, SystemExit):
                raise

            except BaseException as exc:
                last_exc = exc

                if not _is_retryable(exc, cfg):
                    self.circuit.record_failure(exc)
                    break   # go to failure handling below

                remaining = cfg.max_attempts - attempt - 1
                if remaining == 0:
                    self.circuit.record_failure(exc)
                    break

                retry_after = _get_retry_after(exc)
                delay = cfg.calc_delay(attempt)
                if retry_after is not None:
                    delay = max(delay, min(float(retry_after), cfg.max_delay))

                log.warning(
                    "[%s:%s] attempt %d/%d failed (%s) — retrying in %.1fs",
                    self.component, name, attempt + 1, cfg.max_attempts,
                    type(exc).__name__, delay,
                )
                time.sleep(delay)

        # 3. All retries exhausted — handle failure
        error_str = str(last_exc)[:400] if last_exc else "unknown error"
        log.error("[%s:%s] all %d attempt(s) failed: %s",
                  self.component, name, cfg.max_attempts, error_str)

        # Write circuit-open alert if the CB just opened
        if self.circuit.state == CircuitBreaker.OPEN:
            self.alerter.write_circuit_open_alert(
                self.component,
                len(self.circuit._failure_times),
            )

        if queue_on_fail:
            self.queue.queue_failed(
                component    = self.component,
                operation    = name,
                payload      = payload or {},
                error        = error_str,
                attempt_count= cfg.max_attempts,
            )

        if alert_on_fail:
            self.alerter.write_alert(
                component = self.component,
                level     = "WARNING",
                message   = f"`{name}` failed after {cfg.max_attempts} attempt(s).",
                error     = error_str,
                operation = name,
                action    = (
                    f"Check logs for `{self.component}` and verify credentials/connectivity. "
                    f"Queued task will be retried next cycle."
                    if queue_on_fail else
                    f"Check pm2 logs for `{self.component}` to diagnose the failure."
                ),
            )

        if fallback is not _UNSET:
            return fallback
        if last_exc:
            raise MaxRetriesExceeded(
                f"[{self.component}:{name}] failed after {cfg.max_attempts} attempt(s)",
                last_exc=last_exc,
            )
        return None

    # ── Convenience methods ───────────────────────────────────────────────────

    def retry(
        self,
        fn: Callable,
        *args,
        config:   RetryConfig | None = None,
        label:    str = "",
        fallback: Any = _UNSET,
        **kwargs,
    ) -> Any:
        """
        Lightweight retry without circuit-breaker or queue/alert side effects.
        Useful for individual API calls inside check_for_updates().
        """
        return with_retry(
            fn, *args,
            config=config,
            label=label or f"{self.component}.{getattr(fn, '__name__', '?')}",
            fallback=fallback,
            **kwargs,
        )

    def retry_queued(
        self,
        fn: Callable[[dict], Any],
        max_retries_per_task: int = 3,
    ) -> tuple[int, int]:
        """Retry all pending queued tasks for this component."""
        return self.queue.retry_pending(fn, self.component, max_retries_per_task)

    def alert(
        self,
        level: str,
        message: str,
        error: str = "",
        operation: str = "",
        action: str = "",
    ) -> Path | None:
        """Write a human alert directly (bypasses retry logic)."""
        return self.alerter.write_alert(
            component=self.component,
            level=level,
            message=message,
            error=error,
            operation=operation,
            action=action,
        )

    def queue_task(self, operation: str, payload: dict, error: str) -> str:
        """Persist a failed task directly (bypasses retry logic)."""
        return self.queue.queue_failed(self.component, operation, payload, error)

    @property
    def circuit_state(self) -> str:
        return self.circuit.state

    @property
    def queued_count(self) -> int:
        return self.queue.count_pending(self.component)
