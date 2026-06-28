#!/usr/bin/env python3
"""
weekly_audit.py — Weekly Audit & CEO Briefing Generator
========================================================

Data pipeline:
  1. Odoo JSON-RPC  →  AR summary, invoices, overdue items
  2. Vault scan     →  task counts, backlog ages, log action totals
  3. Bank CSVs      →  deposits/withdrawals, recurring charges, anomalies
  4. Synthesis      →  revenue table, bottlenecks, suggestions
  5. Output         →  /Briefings/YYYY-MM-DD_Weekly_Audit.md
                       /Needs_Action/AUDIT_ALERT_*.md  (on anomalies)
                       /Logs/YYYY-MM-DD.json            (audit log entry)

Scheduling:
  Cron (every Monday 7 AM):
    0 7 * * 1  cd /mnt/d/AI_Employee_Vault/AI_Employee_Vault && \
               uv run python weekly_audit.py >> Logs/cron.log 2>&1

  Orchestrator (auto, via SCHEDULED_TASKS in orchestrator.py):
    Calls generate_ceo_briefing() which runs this script as a subprocess.

Usage:
  uv run python weekly_audit.py              # full run
  uv run python weekly_audit.py --dry-run    # validate + print, don't write
  uv run python weekly_audit.py --days-back 14
  uv run python weekly_audit.py --no-odoo --no-bank   # vault-only audit
  uv run python weekly_audit.py --output Briefings/custom.md
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import sys
import textwrap
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

# ── Bootstrap ─────────────────────────────────────────────────────────────────

VAULT_PATH = Path(os.getenv("VAULT_PATH", Path(__file__).parent))
load_dotenv(VAULT_PATH / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [WeeklyAudit] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("weekly_audit")

DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"

# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class Invoice:
    id: int
    name: str
    partner_name: str
    amount_total: float
    amount_residual: float      # outstanding balance
    invoice_date: str
    invoice_date_due: str
    state: str                  # posted | draft | cancel
    payment_state: str          # paid | partial | not_paid | in_payment

    @property
    def is_overdue(self) -> bool:
        if self.payment_state in ("paid", "in_payment"):
            return False
        try:
            due = date.fromisoformat(self.invoice_date_due)
            return due < date.today()
        except (ValueError, TypeError):
            return False

    @property
    def days_overdue(self) -> int:
        if not self.is_overdue:
            return 0
        try:
            due = date.fromisoformat(self.invoice_date_due)
            return (date.today() - due).days
        except (ValueError, TypeError):
            return 0


@dataclass
class BankTransaction:
    txn_date: date
    description: str
    amount: float               # positive = credit, negative = debit
    balance: float | None
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def is_credit(self) -> bool:
        return self.amount > 0


@dataclass
class LogSummary:
    total_actions: int
    error_count: int
    action_counts: dict[str, int]
    files_processed: list[str]
    error_messages: list[str]

    @property
    def error_rate(self) -> float:
        if self.total_actions == 0:
            return 0.0
        return self.error_count / self.total_actions


@dataclass
class AuditFinancials:
    total_invoiced: float
    total_collected: float
    outstanding_ar: float
    overdue_ar: float
    invoice_count: int
    overdue_invoices: list[Invoice]
    source: str = "odoo"        # "odoo" | "unavailable"
    error: str = ""


@dataclass
class BankSummary:
    total_inflow: float
    total_outflow: float
    net_cash_flow: float
    transaction_count: int
    transactions: list[BankTransaction]
    oldest_csv_date: date | None
    csv_files: list[str]
    recurring_charges: list[dict]   # {description, amount, count, total}
    source: str = "csv"             # "csv" | "unavailable"
    error: str = ""


@dataclass
class Alert:
    level: str          # ERROR | WARNING | INFO
    source: str         # odoo | bank | vault | system
    message: str
    action: str = ""

    def emoji(self) -> str:
        return {"ERROR": "🔴", "WARNING": "🟡", "INFO": "🔵"}.get(self.level, "⚪")


@dataclass
class Suggestion:
    priority: str       # HIGH | MEDIUM | LOW
    title: str
    detail: str
    action: str

    def emoji(self) -> str:
        return {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🟢"}.get(self.priority, "⚪")


# ── OdooClient ────────────────────────────────────────────────────────────────

class OdooClient:
    """Thin JSON-RPC wrapper for Odoo Community 19+.  Read-only use in this script."""

    TIMEOUT = 15  # seconds

    def __init__(self):
        self.url      = os.getenv("ODOO_URL", "http://localhost:8069").rstrip("/")
        self.db       = os.getenv("ODOO_DB", "mycompany")
        self.user     = os.getenv("ODOO_USER", "admin")
        self.password = os.getenv("ODOO_PASS", "admin")
        self._session = requests.Session()
        self._uid: int | None = None
        self.available = False

    # ── Internal ──────────────────────────────────────────────────────────────

    def _post(self, endpoint: str, payload: dict) -> Any:
        resp = self._session.post(
            f"{self.url}{endpoint}",
            json=payload,
            timeout=self.TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(data["error"].get("message", "Odoo JSON-RPC error"))
        return data.get("result")

    def authenticate(self) -> bool:
        try:
            result = self._post("/web/session/authenticate", {
                "jsonrpc": "2.0", "method": "call", "id": 1,
                "params": {"db": self.db, "login": self.user, "password": self.password},
            })
            if result and result.get("uid"):
                self._uid = result["uid"]
                self.available = True
                log.info("Odoo authenticated (uid=%s)", self._uid)
                return True
            log.warning("Odoo auth returned no uid")
            return False
        except Exception as exc:
            log.warning("Odoo unreachable: %s", exc)
            return False

    def call(self, model: str, method: str, args: list = None, kwargs: dict = None) -> Any:
        if not self.available or self._uid is None:
            raise RuntimeError("Not authenticated")
        return self._post("/web/dataset/call_kw", {
            "jsonrpc": "2.0", "method": "call", "id": 2,
            "params": {
                "model": model, "method": method,
                "args": args or [], "kwargs": kwargs or {},
            },
        })

    # ── Public read methods ───────────────────────────────────────────────────

    def get_invoices(self, days_back: int = 7) -> list[Invoice]:
        cutoff = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
        domain = [
            ("move_type", "=", "out_invoice"),
            ("invoice_date", ">=", cutoff),
            ("state", "!=", "cancel"),
        ]
        fields = [
            "name", "partner_id", "amount_total", "amount_residual",
            "invoice_date", "invoice_date_due", "state", "payment_state",
        ]
        rows = self.call("account.move", "search_read", [domain], {"fields": fields, "limit": 200})
        return [
            Invoice(
                id=r["id"],
                name=r.get("name", ""),
                partner_name=(r.get("partner_id") or [None, "Unknown"])[1],
                amount_total=float(r.get("amount_total") or 0),
                amount_residual=float(r.get("amount_residual") or 0),
                invoice_date=r.get("invoice_date") or "",
                invoice_date_due=r.get("invoice_date_due") or "",
                state=r.get("state", ""),
                payment_state=r.get("payment_state", ""),
            )
            for r in (rows or [])
        ]

    def get_all_overdue(self) -> list[Invoice]:
        """Fetch all posted invoices with outstanding balance past due date."""
        domain = [
            ("move_type", "=", "out_invoice"),
            ("state", "=", "posted"),
            ("payment_state", "in", ["not_paid", "partial"]),
            ("invoice_date_due", "<", date.today().isoformat()),
        ]
        fields = [
            "name", "partner_id", "amount_total", "amount_residual",
            "invoice_date", "invoice_date_due", "state", "payment_state",
        ]
        rows = self.call("account.move", "search_read", [domain], {"fields": fields, "limit": 100})
        return [
            Invoice(
                id=r["id"],
                name=r.get("name", ""),
                partner_name=(r.get("partner_id") or [None, "Unknown"])[1],
                amount_total=float(r.get("amount_total") or 0),
                amount_residual=float(r.get("amount_residual") or 0),
                invoice_date=r.get("invoice_date") or "",
                invoice_date_due=r.get("invoice_date_due") or "",
                state=r.get("state", ""),
                payment_state=r.get("payment_state", ""),
            )
            for r in (rows or [])
        ]

    def build_financials(self, days_back: int = 7) -> AuditFinancials:
        invoices = self.get_invoices(days_back)
        overdue  = self.get_all_overdue()
        posted   = [i for i in invoices if i.state == "posted"]
        return AuditFinancials(
            total_invoiced  = sum(i.amount_total    for i in posted),
            total_collected = sum(i.amount_total - i.amount_residual for i in posted),
            outstanding_ar  = sum(i.amount_residual for i in posted),
            overdue_ar      = sum(i.amount_residual for i in overdue),
            invoice_count   = len(posted),
            overdue_invoices= overdue,
            source          = "odoo",
        )


# ── VaultScanner ──────────────────────────────────────────────────────────────

class VaultScanner:
    """Reads the Obsidian vault to gather task counts, ages, and log stats."""

    PREFIX_LABELS = {
        "EMAIL_": "Email",
        "WHATSAPP_": "WhatsApp",
        "LINKEDIN_": "LinkedIn",
        "TWITTER_": "Twitter",
        "FACEBOOK_": "Facebook",
        "INSTAGRAM_": "Instagram",
        "YOUTUBE_": "YouTube",
        "ODOO_": "Odoo/Accounting",
        "FILE_": "File",
        "CROSS_": "Cross-Domain",
        "APPROVAL_": "Approval",
        "AUDIT_": "Audit",
        "PLAN_": "Plan",
    }

    def __init__(self, vault: Path):
        self.vault = vault

    def _iter_md(self, folder: str) -> list[Path]:
        d = self.vault / folder
        if not d.exists():
            return []
        return sorted(d.glob("*.md"))

    def _file_age_days(self, p: Path) -> float:
        try:
            mtime = p.stat().st_mtime
            return (datetime.now().timestamp() - mtime) / 86400
        except OSError:
            return 0.0

    def _label(self, filename: str) -> str:
        for prefix, label in self.PREFIX_LABELS.items():
            if filename.upper().startswith(prefix):
                return label
        return "Other"

    # ── Counts & ages ─────────────────────────────────────────────────────────

    def count_by_prefix(self, folder: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for p in self._iter_md(folder):
            label = self._label(p.name)
            counts[label] = counts.get(label, 0) + 1
        return counts

    def backlog_items(self) -> list[dict]:
        """Items in Needs_Action with their age in days."""
        items = []
        for p in self._iter_md("Needs_Action"):
            items.append({
                "file":  p.name,
                "label": self._label(p.name),
                "age":   round(self._file_age_days(p), 1),
            })
        return sorted(items, key=lambda x: x["age"], reverse=True)

    def done_this_week(self, days_back: int = 7) -> dict[str, int]:
        cutoff = datetime.now() - timedelta(days=days_back)
        counts: dict[str, int] = {}
        for p in self._iter_md("Done"):
            try:
                mtime = datetime.fromtimestamp(p.stat().st_mtime)
                if mtime >= cutoff:
                    label = self._label(p.name)
                    counts[label] = counts.get(label, 0) + 1
            except OSError:
                pass
        return counts

    def active_plans(self) -> list[dict]:
        plans = []
        for p in self._iter_md("Plans"):
            text = p.read_text(errors="ignore")
            total = text.count("- [ ]") + text.count("- [x]")
            done  = text.count("- [x]")
            plans.append({
                "file":     p.name,
                "progress": f"{done}/{total}" if total else "0/0",
                "age":      round(self._file_age_days(p), 1),
            })
        for p in self._iter_md("Active_Project"):
            text = p.read_text(errors="ignore")
            total = text.count("- [ ]") + text.count("- [x]")
            done  = text.count("- [x]")
            plans.append({
                "file":     p.name,
                "progress": f"{done}/{total}" if total else "0/0",
                "age":      round(self._file_age_days(p), 1),
            })
        return plans

    def pending_approvals(self) -> list[dict]:
        items = []
        for p in self._iter_md("Pending_Approval"):
            items.append({"file": p.name, "age": round(self._file_age_days(p), 1)})
        return sorted(items, key=lambda x: x["age"], reverse=True)

    def read_business_goals(self) -> str:
        p = self.vault / "Business_Goals.md"
        if p.exists():
            return p.read_text(errors="ignore")[:2000]
        return ""

    # ── Log parsing ───────────────────────────────────────────────────────────

    def parse_logs(self, days_back: int = 7) -> LogSummary:
        action_counts: dict[str, int] = {}
        total = error_count = 0
        files_processed: list[str] = []
        error_messages: list[str] = []

        for i in range(days_back + 1):
            log_date = (date.today() - timedelta(days=i)).isoformat()
            log_file = self.vault / "Logs" / f"{log_date}.json"
            if not log_file.exists():
                continue
            files_processed.append(log_file.name)
            try:
                entries = json.loads(log_file.read_text())
                if not isinstance(entries, list):
                    entries = [entries]
                for entry in entries:
                    atype = entry.get("action_type", entry.get("type", "unknown"))
                    action_counts[atype] = action_counts.get(atype, 0) + 1
                    total += 1
                    if "error" in atype.lower() or entry.get("level", "") == "ERROR":
                        error_count += 1
                        msg = entry.get("details", {})
                        if isinstance(msg, dict):
                            msg = msg.get("error", str(msg))
                        error_messages.append(str(msg)[:120])
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("Could not parse log %s: %s", log_file.name, exc)

        return LogSummary(
            total_actions  = total,
            error_count    = error_count,
            action_counts  = action_counts,
            files_processed= files_processed,
            error_messages = error_messages[:5],  # keep top 5
        )


# ── BankCSVParser ─────────────────────────────────────────────────────────────

class BankCSVParser:
    """
    Auto-detecting bank CSV parser.  Supports common bank export formats:
      - Standard:     Date, Description, Amount, Balance
      - Chase:        Transaction Date, Description, Amount
      - Bank of America: Posted Date, Payee, Amount
      - Any CSV with one date column and one numeric amount column
    """

    DATE_ALIASES    = ["date", "transaction date", "posted date", "post date", "value date"]
    AMOUNT_ALIASES  = ["amount", "transaction amount", "debit/credit", "credit", "net amount"]
    DEBIT_ALIASES   = ["debit", "withdrawal", "withdrawals"]
    CREDIT_ALIASES  = ["credit", "deposit", "deposits"]
    DESC_ALIASES    = ["description", "payee", "merchant", "memo", "narrative", "details"]
    BALANCE_ALIASES = ["balance", "running balance", "closing balance"]

    def __init__(self, vault: Path, csv_dir: str = "Accounting/bank_statements"):
        self.vault   = vault
        self.csv_dir = vault / csv_dir
        # Also check root Accounting/ for any loose CSVs
        self.fallback_dir = vault / "Accounting"

    def find_csvs(self) -> list[Path]:
        files: list[Path] = []
        for d in [self.csv_dir, self.fallback_dir]:
            if d.exists():
                files.extend(d.glob("*.csv"))
                files.extend(d.glob("*.CSV"))
        return sorted(set(files), key=lambda p: p.stat().st_mtime, reverse=True)

    def _detect_columns(self, header: list[str]) -> dict[str, int | None]:
        """Map logical column names to header indices (case-insensitive)."""
        norm = {h.strip().lower(): i for i, h in enumerate(header)}

        def first(aliases: list[str]) -> int | None:
            for alias in aliases:
                if alias in norm:
                    return norm[alias]
            return None

        return {
            "date":    first(self.DATE_ALIASES),
            "amount":  first(self.AMOUNT_ALIASES),
            "debit":   first(self.DEBIT_ALIASES),
            "credit":  first(self.CREDIT_ALIASES),
            "desc":    first(self.DESC_ALIASES),
            "balance": first(self.BALANCE_ALIASES),
        }

    def _parse_amount(self, raw: str) -> float | None:
        """Strip currency symbols, commas, parentheses → float."""
        raw = raw.strip().replace(",", "").replace("$", "").replace("£", "").replace("€", "")
        if not raw:
            return None
        negative = raw.startswith("(") and raw.endswith(")")
        raw = raw.strip("()")
        try:
            val = float(raw)
            return -abs(val) if negative else val
        except ValueError:
            return None

    def _parse_date(self, raw: str) -> date | None:
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%d-%m-%Y", "%m-%d-%Y",
                    "%d %b %Y", "%d-%b-%Y", "%Y%m%d"):
            try:
                return datetime.strptime(raw.strip(), fmt).date()
            except (ValueError, AttributeError):
                pass
        return None

    def parse_csv(self, path: Path) -> list[BankTransaction]:
        transactions: list[BankTransaction] = []
        try:
            with path.open(newline="", encoding="utf-8-sig") as fh:
                reader  = csv.reader(fh)
                headers = next(reader, None)
                if headers is None:
                    return []
                cols = self._detect_columns(headers)
                if cols["date"] is None:
                    log.warning("No date column found in %s", path.name)
                    return []

                for row in reader:
                    if not any(row):
                        continue
                    raw_date = row[cols["date"]].strip() if cols["date"] < len(row) else ""
                    txn_date = self._parse_date(raw_date)
                    if txn_date is None:
                        continue

                    # Amount: unified column OR separate debit/credit columns
                    amount: float | None = None
                    if cols["amount"] is not None and cols["amount"] < len(row):
                        amount = self._parse_amount(row[cols["amount"]])
                    elif cols["credit"] is not None or cols["debit"] is not None:
                        credit = debit = 0.0
                        if cols["credit"] is not None and cols["credit"] < len(row):
                            v = self._parse_amount(row[cols["credit"]])
                            credit = v or 0.0
                        if cols["debit"] is not None and cols["debit"] < len(row):
                            v = self._parse_amount(row[cols["debit"]])
                            debit = abs(v) if v else 0.0
                        amount = credit - debit if (credit or debit) else None

                    if amount is None:
                        continue

                    desc = ""
                    if cols["desc"] is not None and cols["desc"] < len(row):
                        desc = row[cols["desc"]].strip()

                    balance: float | None = None
                    if cols["balance"] is not None and cols["balance"] < len(row):
                        balance = self._parse_amount(row[cols["balance"]])

                    transactions.append(BankTransaction(
                        txn_date    = txn_date,
                        description = desc,
                        amount      = amount,
                        balance     = balance,
                        raw         = dict(zip(headers, row)),
                    ))
        except (OSError, csv.Error) as exc:
            log.warning("Could not read CSV %s: %s", path.name, exc)
        return transactions

    def detect_recurring(self, transactions: list[BankTransaction]) -> list[dict]:
        """Find charges that appear 2+ times with similar amounts (subscriptions)."""
        from collections import defaultdict
        groups: dict[str, list[float]] = defaultdict(list)
        for t in transactions:
            if t.amount < 0:   # outflows only
                # Normalize description: remove dates, reference numbers
                norm = re.sub(r'\d{4,}', '', t.description.upper()).strip()
                norm = re.sub(r'\s+', ' ', norm)
                groups[norm].append(abs(t.amount))

        recurring = []
        for desc, amounts in groups.items():
            if len(amounts) < 2:
                continue
            avg = sum(amounts) / len(amounts)
            variance = max(abs(a - avg) for a in amounts)
            if variance / avg < 0.10:   # within 10% = probably same charge
                recurring.append({
                    "description": desc,
                    "count":       len(amounts),
                    "avg_amount":  round(avg, 2),
                    "total":       round(sum(amounts), 2),
                })
        return sorted(recurring, key=lambda x: x["total"], reverse=True)

    def build_summary(self, days_back: int = 7) -> BankSummary:
        csv_files = self.find_csvs()
        if not csv_files:
            return BankSummary(
                total_inflow=0, total_outflow=0, net_cash_flow=0,
                transaction_count=0, transactions=[], oldest_csv_date=None,
                csv_files=[], recurring_charges=[], source="unavailable",
                error="No bank CSV files found in Accounting/bank_statements/",
            )

        cutoff = date.today() - timedelta(days=days_back)
        all_txns: list[BankTransaction] = []
        for p in csv_files:
            all_txns.extend(self.parse_csv(p))

        # Filter to the audit window
        period_txns = [t for t in all_txns if t.txn_date >= cutoff]

        inflow  = sum(t.amount for t in period_txns if t.amount > 0)
        outflow = sum(t.amount for t in period_txns if t.amount < 0)
        oldest  = min((t.txn_date for t in all_txns), default=None) if all_txns else None

        return BankSummary(
            total_inflow      = round(inflow, 2),
            total_outflow     = round(outflow, 2),
            net_cash_flow     = round(inflow + outflow, 2),
            transaction_count = len(period_txns),
            transactions      = period_txns,
            oldest_csv_date   = oldest,
            csv_files         = [p.name for p in csv_files],
            recurring_charges = self.detect_recurring(period_txns),
            source            = "csv",
        )

    def validate_freshness(self, days_back: int = 7) -> tuple[bool, str]:
        """Returns (is_fresh, message).  Stale if newest CSV is older than days_back."""
        csvs = self.find_csvs()
        if not csvs:
            return False, "No CSV files found"
        newest_mtime = max(p.stat().st_mtime for p in csvs)
        newest_date  = datetime.fromtimestamp(newest_mtime).date()
        cutoff = date.today() - timedelta(days=days_back)
        if newest_date < cutoff:
            return False, f"Newest CSV last modified {newest_date} (>{days_back} days ago)"
        return True, f"CSV data current as of {newest_date}"


# ── DataValidator ─────────────────────────────────────────────────────────────

class DataValidator:
    """Runs validation checks on all data sources and produces alerts."""

    OVERDUE_ALERT_THRESHOLD   = 500.0   # USD — alert if total overdue > this
    BACKLOG_ALERT_THRESHOLD   = 15      # items — alert if Needs_Action > this
    ERROR_RATE_ALERT_THRESHOLD = 0.10   # 10% — alert if log error rate > this
    APPROVAL_STALE_DAYS       = 3       # alert if Pending_Approval item older

    def __init__(
        self,
        odoo:    OdooClient,
        bank:    BankCSVParser,
        scanner: VaultScanner,
        days_back: int = 7,
    ):
        self.odoo      = odoo
        self.bank      = bank
        self.scanner   = scanner
        self.days_back = days_back

    def validate_odoo(self) -> list[Alert]:
        alerts: list[Alert] = []
        if not self.odoo.available:
            alerts.append(Alert(
                level="ERROR", source="odoo",
                message="Odoo is unreachable. Financial data missing from this report.",
                action="Check ODOO_URL / credentials in .env and ensure Odoo is running.",
            ))
        return alerts

    def validate_bank(self) -> list[Alert]:
        alerts: list[Alert] = []
        fresh, msg = self.bank.validate_freshness(self.days_back)
        if not fresh:
            alerts.append(Alert(
                level="WARNING", source="bank",
                message=f"Bank CSV may be stale: {msg}",
                action="Upload latest bank statement to Accounting/bank_statements/",
            ))
        if not self.bank.find_csvs():
            alerts.append(Alert(
                level="INFO", source="bank",
                message="No bank CSV files found. Bank reconciliation section will be empty.",
                action="Export a CSV from your bank and place in Accounting/bank_statements/",
            ))
        return alerts

    def validate_vault(self) -> list[Alert]:
        alerts: list[Alert] = []

        backlog = self.scanner.backlog_items()
        if len(backlog) > self.BACKLOG_ALERT_THRESHOLD:
            alerts.append(Alert(
                level="WARNING", source="vault",
                message=f"Needs_Action backlog has {len(backlog)} items (threshold: {self.BACKLOG_ALERT_THRESHOLD}).",
                action="Schedule a triage session to process the backlog.",
            ))

        stale = [b for b in backlog if b["age"] > 7]
        if stale:
            alerts.append(Alert(
                level="WARNING", source="vault",
                message=f"{len(stale)} item(s) in Needs_Action older than 7 days.",
                action=f"Oldest: {stale[0]['file']} ({stale[0]['age']:.0f} days old). Review immediately.",
            ))

        approvals = self.scanner.pending_approvals()
        stale_approvals = [a for a in approvals if a["age"] > self.APPROVAL_STALE_DAYS]
        if stale_approvals:
            alerts.append(Alert(
                level="WARNING", source="vault",
                message=f"{len(stale_approvals)} approval(s) waiting >3 days.",
                action=f"Check Pending_Approval/ in Obsidian. Oldest: {stale_approvals[0]['file']}",
            ))

        return alerts

    def validate_financials(self, fin: AuditFinancials) -> list[Alert]:
        alerts: list[Alert] = []
        if fin.source == "unavailable":
            return alerts  # already alerted in validate_odoo
        if fin.overdue_ar > self.OVERDUE_ALERT_THRESHOLD:
            alerts.append(Alert(
                level="WARNING", source="odoo",
                message=(
                    f"Total overdue AR is ${fin.overdue_ar:,.2f} "
                    f"({len(fin.overdue_invoices)} invoice(s))."
                ),
                action="Follow up with customers on overdue invoices immediately.",
            ))
        return alerts

    def validate_logs(self, logs: LogSummary) -> list[Alert]:
        alerts: list[Alert] = []
        if logs.total_actions > 0 and logs.error_rate > self.ERROR_RATE_ALERT_THRESHOLD:
            alerts.append(Alert(
                level="WARNING", source="system",
                message=(
                    f"System error rate is {logs.error_rate:.0%} "
                    f"({logs.error_count}/{logs.total_actions} actions failed)."
                ),
                action="Check pm2 logs and /Logs/ for recurring errors.",
            ))
        return alerts

    def run_all(
        self,
        fin:  AuditFinancials,
        logs: LogSummary,
    ) -> list[Alert]:
        all_alerts: list[Alert] = []
        all_alerts.extend(self.validate_odoo())
        all_alerts.extend(self.validate_bank())
        all_alerts.extend(self.validate_vault())
        all_alerts.extend(self.validate_financials(fin))
        all_alerts.extend(self.validate_logs(logs))
        return all_alerts


# ── Suggestion Engine ─────────────────────────────────────────────────────────

def generate_suggestions(
    fin:      AuditFinancials,
    bank:     BankSummary,
    backlog:  list[dict],
    done:     dict[str, int],
    logs:     LogSummary,
    approvals: list[dict],
) -> list[Suggestion]:
    suggestions: list[Suggestion] = []

    # 1. Overdue invoice follow-up
    if fin.overdue_invoices:
        top = sorted(fin.overdue_invoices, key=lambda x: x.amount_residual, reverse=True)[:3]
        names = ", ".join(f"{i.partner_name} (${i.amount_residual:,.0f})" for i in top)
        suggestions.append(Suggestion(
            priority="HIGH",
            title="Chase Overdue Payments",
            detail=f"{len(fin.overdue_invoices)} overdue invoice(s) totalling ${fin.overdue_ar:,.2f}. Top: {names}.",
            action="Send payment reminders via the email_triage skill or call directly.",
        ))

    # 2. Stale approvals
    stale_approvals = [a for a in approvals if a["age"] > 3]
    if stale_approvals:
        suggestions.append(Suggestion(
            priority="HIGH",
            title="Clear Pending Approvals",
            detail=f"{len(stale_approvals)} approval(s) have been waiting >3 days in /Pending_Approval.",
            action="Open Obsidian, review each file, and move to /Approved or /Rejected.",
        ))

    # 3. Large backlog
    if len(backlog) > 10:
        oldest = backlog[0] if backlog else None
        details = f"Needs_Action has {len(backlog)} items."
        if oldest:
            details += f" Oldest: '{oldest['file']}' ({oldest['age']:.0f} days)."
        suggestions.append(Suggestion(
            priority="MEDIUM",
            title="Schedule Backlog Triage",
            detail=details,
            action="Block 30 minutes to batch-process the Needs_Action folder.",
        ))

    # 4. Low productivity
    total_done = sum(done.values())
    if total_done < 5:
        suggestions.append(Suggestion(
            priority="MEDIUM",
            title="Low Throughput This Week",
            detail=f"Only {total_done} task(s) completed in the last 7 days.",
            action="Review blockers — check if watchers are running and DRY_RUN is still enabled.",
        ))

    # 5. Recurring charges (flagged subscriptions)
    if bank.source == "csv":
        unknown_subs = [r for r in bank.recurring_charges if r["avg_amount"] > 20]
        if unknown_subs:
            top = unknown_subs[:3]
            names = "; ".join(f"{r['description']} (${r['avg_amount']}/occurrence)" for r in top)
            suggestions.append(Suggestion(
                priority="MEDIUM",
                title="Review Recurring Charges",
                detail=f"Found {len(unknown_subs)} recurring charge(s): {names}.",
                action="Verify each is authorised. Cancel unused subscriptions to reduce burn.",
            ))

    # 6. Bank vs Odoo reconciliation gap
    if bank.source == "csv" and fin.source == "odoo":
        bank_inflow = bank.total_inflow
        odoo_collected = fin.total_collected
        if odoo_collected > 0 and bank_inflow > 0:
            gap = abs(bank_inflow - odoo_collected)
            pct = gap / max(bank_inflow, odoo_collected)
            if pct > 0.20:
                suggestions.append(Suggestion(
                    priority="LOW",
                    title="Reconciliation Gap Detected",
                    detail=(
                        f"Bank inflow (${bank_inflow:,.2f}) vs Odoo collected "
                        f"(${odoo_collected:,.2f}) differ by ${gap:,.2f} ({pct:.0%})."
                    ),
                    action="Verify bank deposits are recorded in Odoo. Check for missing payments.",
                ))

    # 7. High error rate
    if logs.total_actions > 0 and logs.error_rate > 0.05:
        suggestions.append(Suggestion(
            priority="LOW",
            title="System Health Check",
            detail=f"Error rate is {logs.error_rate:.0%} over the past 7 days.",
            action="Run `pm2 logs` for each watcher to identify the root cause.",
        ))

    # Deduplicate & sort
    seen: set[str] = set()
    unique: list[Suggestion] = []
    for s in suggestions:
        if s.title not in seen:
            unique.append(s)
            seen.add(s.title)
    order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    return sorted(unique, key=lambda s: order.get(s.priority, 9))


# ── AuditReporter ─────────────────────────────────────────────────────────────

class AuditReporter:
    """Assembles all data into a CEO-ready Markdown briefing."""

    def __init__(
        self,
        fin:         AuditFinancials,
        bank:        BankSummary,
        backlog:     list[dict],
        done:        dict[str, int],
        plans:       list[dict],
        approvals:   list[dict],
        logs:        LogSummary,
        alerts:      list[Alert],
        suggestions: list[Suggestion],
        days_back:   int,
    ):
        self.fin         = fin
        self.bank        = bank
        self.backlog     = backlog
        self.done        = done
        self.plans       = plans
        self.approvals   = approvals
        self.logs        = logs
        self.alerts      = alerts
        self.suggestions = suggestions
        self.days_back   = days_back
        self.now         = datetime.now(timezone.utc)
        self.week_start  = (date.today() - timedelta(days=days_back)).isoformat()
        self.week_end    = date.today().isoformat()

    # ── Section builders ──────────────────────────────────────────────────────

    def _section_summary(self) -> str:
        total_done   = sum(self.done.values())
        backlog_size = len(self.backlog)
        high_alerts  = sum(1 for a in self.alerts if a.level == "ERROR")
        warn_alerts  = sum(1 for a in self.alerts if a.level == "WARNING")

        fin_line = (
            f"Revenue invoiced: **${self.fin.total_invoiced:,.2f}**, "
            f"collected: **${self.fin.total_collected:,.2f}**, "
            f"AR outstanding: **${self.fin.outstanding_ar:,.2f}**."
            if self.fin.source == "odoo"
            else "_Financial data unavailable — Odoo unreachable._"
        )

        return textwrap.dedent(f"""\
            ## Executive Summary

            Period: **{self.week_start}** → **{self.week_end}** ({self.days_back} days)

            {fin_line}
            Tasks completed this week: **{total_done}** | Backlog: **{backlog_size} items**.
            {f"⚠️ **{high_alerts} error(s) and {warn_alerts} warning(s)** require attention." if high_alerts or warn_alerts else "✅ No critical alerts."}
        """)

    def _section_financials(self) -> str:
        fin = self.fin
        if fin.source == "unavailable":
            return "## Revenue & Financials\n\n> ⚠️ Odoo unreachable — financial data unavailable.\n\n"

        currency = os.getenv("ODOO_CURRENCY", "USD")
        rows = [
            ("Total Invoiced",   f"${fin.total_invoiced:>10,.2f} {currency}"),
            ("Total Collected",  f"${fin.total_collected:>10,.2f} {currency}"),
            ("Outstanding AR",   f"${fin.outstanding_ar:>10,.2f} {currency}"),
            ("Overdue AR",       f"${fin.overdue_ar:>10,.2f} {currency} {'⚠️' if fin.overdue_ar > 0 else ''}"),
            ("Invoice Count",    str(fin.invoice_count)),
        ]
        table  = "| Metric | Value |\n|--------|-------|\n"
        table += "\n".join(f"| {k} | {v} |" for k, v in rows)

        overdue_section = ""
        if fin.overdue_invoices:
            overdue_section = "\n### Overdue Invoices\n\n"
            overdue_section += "| Invoice | Customer | Amount | Due Date | Days Overdue |\n"
            overdue_section += "|---------|----------|--------|----------|--------------|\n"
            for inv in sorted(fin.overdue_invoices, key=lambda x: x.days_overdue, reverse=True)[:10]:
                overdue_section += (
                    f"| {inv.name} | {inv.partner_name} "
                    f"| ${inv.amount_residual:,.2f} | {inv.invoice_date_due} "
                    f"| **{inv.days_overdue}d** |\n"
                )

        return f"## Revenue & Financials\n\n{table}\n{overdue_section}\n"

    def _section_bank(self) -> str:
        bank = self.bank
        if bank.source == "unavailable":
            return (
                "## Bank Reconciliation\n\n"
                f"> ℹ️ {bank.error}\n\n"
                "> Place CSV exports in `Accounting/bank_statements/` to enable this section.\n\n"
            )

        direction_icon = "📈" if bank.net_cash_flow >= 0 else "📉"
        rows = [
            ("Total Inflow",        f"${bank.total_inflow:>10,.2f}"),
            ("Total Outflow",       f"${abs(bank.total_outflow):>10,.2f}"),
            ("Net Cash Flow",       f"${bank.net_cash_flow:>10,.2f} {direction_icon}"),
            ("Transaction Count",   str(bank.transaction_count)),
        ]
        table  = "| Metric | Value |\n|--------|-------|\n"
        table += "\n".join(f"| {k} | {v} |" for k, v in rows)

        top_credits = sorted(
            [t for t in bank.transactions if t.is_credit],
            key=lambda t: t.amount, reverse=True
        )[:5]
        top_debits  = sorted(
            [t for t in bank.transactions if not t.is_credit],
            key=lambda t: t.amount
        )[:5]

        credits_tbl = ""
        if top_credits:
            credits_tbl = "\n### Top Inflows\n\n"
            credits_tbl += "| Date | Description | Amount |\n|------|-------------|--------|\n"
            for t in top_credits:
                credits_tbl += f"| {t.txn_date} | {t.description[:50]} | **+${t.amount:,.2f}** |\n"

        debits_tbl = ""
        if top_debits:
            debits_tbl = "\n### Top Outflows\n\n"
            debits_tbl += "| Date | Description | Amount |\n|------|-------------|--------|\n"
            for t in top_debits:
                debits_tbl += f"| {t.txn_date} | {t.description[:50]} | **-${abs(t.amount):,.2f}** |\n"

        recurring_section = ""
        if bank.recurring_charges:
            recurring_section = "\n### Recurring Charges (Subscriptions)\n\n"
            recurring_section += "| Description | Count | Avg/Charge | Period Total |\n"
            recurring_section += "|-------------|-------|------------|-------------|\n"
            for r in bank.recurring_charges[:8]:
                recurring_section += (
                    f"| {r['description'][:40]} | {r['count']} "
                    f"| ${r['avg_amount']:,.2f} | ${r['total']:,.2f} |\n"
                )

        source_note = f"\n_Data from: {', '.join(bank.csv_files)}_\n"
        return (
            f"## Bank Reconciliation\n\n{table}"
            f"{credits_tbl}{debits_tbl}{recurring_section}{source_note}\n"
        )

    def _section_tasks(self) -> str:
        total_done   = sum(self.done.values())
        backlog_size = len(self.backlog)

        done_rows = ""
        if self.done:
            done_rows = "\n".join(
                f"| {cat} | {count} |"
                for cat, count in sorted(self.done.items(), key=lambda x: x[1], reverse=True)
            )
        else:
            done_rows = "| — | 0 |"

        pending_rows = ""
        if self.backlog:
            pending_rows = "\n".join(
                f"| {item['file'][:60]} | {item['label']} | {item['age']:.0f}d |"
                for item in self.backlog[:15]
            )
            if len(self.backlog) > 15:
                pending_rows += f"\n| _…and {len(self.backlog)-15} more_ | | |"
        else:
            pending_rows = "| — | — | — |"

        plans_section = ""
        if self.plans:
            plans_section = "\n### Active Plans\n\n"
            plans_section += "| Plan | Progress | Age |\n|------|----------|-----|\n"
            for p in self.plans[:8]:
                plans_section += f"| {p['file'][:55]} | {p['progress']} | {p['age']:.0f}d |\n"

        return textwrap.dedent(f"""\
            ## Work Summary

            ### Completed This Week ({total_done} tasks)

            | Category | Count |
            |----------|-------|
            {done_rows}

            ### Outstanding Backlog ({backlog_size} items)

            | File | Type | Age |
            |------|------|-----|
            {pending_rows}
            {plans_section}
        """)

    def _section_bottlenecks(self) -> str:
        # Items older than 3 days in Needs_Action are potential bottlenecks
        stale = [b for b in self.backlog if b["age"] > 3]
        stale_approvals = [a for a in self.approvals if a["age"] > 2]

        if not stale and not stale_approvals:
            return "## Bottlenecks\n\n✅ No bottlenecks detected this week.\n\n"

        rows = ""
        for b in stale[:10]:
            rows += f"| {b['file'][:60]} | Needs_Action | {b['age']:.0f} days |\n"
        for a in stale_approvals[:5]:
            rows += f"| {a['file'][:60]} | Pending_Approval | {a['age']:.0f} days |\n"

        return textwrap.dedent(f"""\
            ## Bottlenecks

            | Item | Location | Waiting |
            |------|----------|---------|
            {rows}
        """)

    def _section_alerts(self) -> str:
        if not self.alerts:
            return ""
        lines = "\n".join(
            f"- {a.emoji()} **[{a.level}]** `{a.source}`: {a.message}"
            + (f"\n  - 💡 *{a.action}*" if a.action else "")
            for a in self.alerts
        )
        return f"## Data Quality & Alerts\n\n{lines}\n\n"

    def _section_suggestions(self) -> str:
        if not self.suggestions:
            return "## Proactive Suggestions\n\n✅ No actions required.\n\n"
        items = "\n".join(
            f"{i+1}. {s.emoji()} **[{s.priority}] {s.title}**\n"
            f"   {s.detail}\n"
            f"   💡 *{s.action}*"
            for i, s in enumerate(self.suggestions)
        )
        return f"## Proactive Suggestions\n\n{items}\n\n"

    def _section_system(self) -> str:
        top_actions = sorted(
            self.logs.action_counts.items(), key=lambda x: x[1], reverse=True
        )[:8]
        action_rows = "\n".join(f"| {k} | {v} |" for k, v in top_actions) or "| — | — |"
        error_rows  = "\n".join(f"- `{e}`" for e in self.logs.error_messages) or "_None_"
        error_badge = (
            f"🔴 {self.logs.error_count} error(s) ({self.logs.error_rate:.0%})"
            if self.logs.error_count
            else "🟢 No errors"
        )
        return textwrap.dedent(f"""\
            ## System Health

            | Metric | Value |
            |--------|-------|
            | Total Actions (7d) | {self.logs.total_actions} |
            | Error Rate | {error_badge} |
            | Log Files Parsed | {len(self.logs.files_processed)} |

            ### Top Action Types

            | Action | Count |
            |--------|-------|
            {action_rows}

            ### Recent Errors

            {error_rows}
        """)

    # ── Main render ───────────────────────────────────────────────────────────

    def render(self) -> str:
        dry_banner = (
            "> ⚠️ **DRY RUN** — All data is real but no write actions were taken this week.\n\n"
            if DRY_RUN else ""
        )
        header = textwrap.dedent(f"""\
            ---
            generated: {self.now.isoformat()}
            period_start: {self.week_start}
            period_end: {self.week_end}
            days_back: {self.days_back}
            odoo_source: {self.fin.source}
            bank_source: {self.bank.source}
            type: weekly_audit
            ---

            # Weekly CEO Briefing — {self.week_end}

            {dry_banner}
        """)
        footer = textwrap.dedent(f"""\
            ---
            *Generated by AI Employee · `weekly_audit.py` · {self.now.strftime("%Y-%m-%d %H:%M UTC")}*
        """)
        return (
            header
            + self._section_summary()
            + "\n"
            + self._section_financials()
            + self._section_bank()
            + self._section_tasks()
            + self._section_bottlenecks()
            + self._section_suggestions()
            + self._section_alerts()
            + self._section_system()
            + footer
        )

    def write(self, output: Path) -> None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(self.render(), encoding="utf-8")
        log.info("Briefing written → %s", output)


# ── Alert writer ──────────────────────────────────────────────────────────────

def write_alert_files(alerts: list[Alert], vault: Path, dry_run: bool = True) -> list[Path]:
    """Write ERROR/WARNING alerts to /Needs_Action so the orchestrator picks them up."""
    written: list[Path] = []
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y-%m-%d_%H%M")

    for alert in alerts:
        if alert.level == "INFO":
            continue                # INFO alerts only go into the briefing
        safe_src = re.sub(r"[^a-z0-9]", "_", alert.source.lower())
        filename = f"AUDIT_ALERT_{safe_src.upper()}_{stamp}.md"
        path     = vault / "Needs_Action" / filename

        content = textwrap.dedent(f"""\
            ---
            type: audit_alert
            level: {alert.level}
            source: {alert.source}
            generated: {now.isoformat()}
            ---

            # Audit Alert: {alert.level} from {alert.source}

            {alert.message}

            ## Recommended Action

            {alert.action or '_No specific action defined._'}

            ---
            *Auto-generated by weekly_audit.py*
        """)

        if dry_run:
            log.info("[DRY RUN] Would write alert → %s", path.name)
        else:
            path.write_text(content, encoding="utf-8")
            log.info("Alert written → %s", path.name)
            written.append(path)

    return written


# ── Log writer ────────────────────────────────────────────────────────────────

def log_audit_run(
    vault:     Path,
    briefing:  Path | None,
    alerts:    list[Alert],
    fin:       AuditFinancials,
    bank:      BankSummary,
    dry_run:   bool,
) -> None:
    today    = date.today().isoformat()
    log_file = vault / "Logs" / f"{today}.json"
    entry = {
        "timestamp":    datetime.now(timezone.utc).isoformat(),
        "actor":        "weekly_audit",
        "action_type":  "weekly_audit_run",
        "details": {
            "dry_run":       dry_run,
            "briefing_path": str(briefing) if briefing else None,
            "alert_count":   len(alerts),
            "error_alerts":  sum(1 for a in alerts if a.level == "ERROR"),
            "warn_alerts":   sum(1 for a in alerts if a.level == "WARNING"),
            "odoo_source":   fin.source,
            "bank_source":   bank.source,
            "invoiced":      fin.total_invoiced if fin.source == "odoo" else None,
            "collected":     fin.total_collected if fin.source == "odoo" else None,
            "overdue_ar":    fin.overdue_ar if fin.source == "odoo" else None,
            "bank_inflow":   bank.total_inflow if bank.source == "csv" else None,
        },
    }

    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        existing: list = []
        if log_file.exists():
            try:
                existing = json.loads(log_file.read_text())
            except (json.JSONDecodeError, OSError):
                existing = []
        existing.append(entry)
        log_file.write_text(json.dumps(existing, indent=2, default=str), encoding="utf-8")
    except OSError as exc:
        log.warning("Could not write audit log: %s", exc)


# ── Dashboard update ──────────────────────────────────────────────────────────

def update_dashboard(vault: Path, briefing_path: Path, dry_run: bool) -> None:
    dashboard = vault / "Dashboard.md"
    if not dashboard.exists():
        return
    try:
        content = dashboard.read_text(encoding="utf-8")
        now = datetime.now(timezone.utc)
        link_line = (
            f"- **Last Weekly Audit**: [{briefing_path.name}]({briefing_path.relative_to(vault)}) "
            f"— {now.strftime('%Y-%m-%d %H:%M UTC')}"
        )
        if "Last Weekly Audit" in content:
            content = re.sub(r"- \*\*Last Weekly Audit\*\*:.*", link_line, content)
        else:
            content = content.replace(
                "## Recent Activity",
                f"{link_line}\n\n## Recent Activity",
            )
        if dry_run:
            log.info("[DRY RUN] Would update Dashboard.md with briefing link")
        else:
            dashboard.write_text(content, encoding="utf-8")
            log.info("Dashboard.md updated with briefing link")
    except OSError as exc:
        log.warning("Could not update Dashboard.md: %s", exc)


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Weekly Audit & CEO Briefing Generator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Cron (every Monday 7 AM):
              0 7 * * 1  cd /mnt/d/AI_Employee_Vault/AI_Employee_Vault && \\
                         uv run python weekly_audit.py >> Logs/cron.log 2>&1

            Environment variables (from .env):
              ODOO_URL, ODOO_DB, ODOO_USER, ODOO_PASS  — Odoo connection
              DRY_RUN                                   — skip file writes
              VAULT_PATH                                — override vault root
              BANK_CSV_DIR                              — override CSV directory
        """),
    )
    p.add_argument("--days-back",  type=int, default=7, metavar="N",
                   help="Audit window in days (default: 7)")
    p.add_argument("--no-odoo",   action="store_true",
                   help="Skip Odoo — use if Odoo is not configured")
    p.add_argument("--no-bank",   action="store_true",
                   help="Skip bank CSV parsing")
    p.add_argument("--output",    type=Path, default=None, metavar="PATH",
                   help="Override output file path for the briefing")
    p.add_argument("--dry-run",   action="store_true",
                   help="Print briefing to stdout, don't write files")
    p.add_argument("--print",     action="store_true",
                   help="Also print the briefing to stdout after writing")
    return p.parse_args()


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    args     = parse_args()
    dry_run  = DRY_RUN or args.dry_run
    days_back = args.days_back

    log.info("=== Weekly Audit starting (days_back=%d, dry_run=%s) ===", days_back, dry_run)

    # ── 1. Initialise data sources ─────────────────────────────────────────────

    odoo    = OdooClient()
    scanner = VaultScanner(VAULT_PATH)
    csv_dir = os.getenv("BANK_CSV_DIR", "Accounting/bank_statements")
    bank    = BankCSVParser(VAULT_PATH, csv_dir)

    # ── 2. Authenticate Odoo ───────────────────────────────────────────────────

    if args.no_odoo:
        log.info("Skipping Odoo (--no-odoo)")
        fin = AuditFinancials(0, 0, 0, 0, 0, [], source="unavailable",
                              error="Skipped via --no-odoo flag")
    else:
        log.info("Connecting to Odoo at %s …", odoo.url)
        odoo.authenticate()
        if odoo.available:
            try:
                log.info("Fetching Odoo financials …")
                fin = odoo.build_financials(days_back)
                log.info(
                    "Odoo: invoiced=$%.2f  collected=$%.2f  overdue=$%.2f  (%d inv)",
                    fin.total_invoiced, fin.total_collected, fin.overdue_ar, fin.invoice_count,
                )
            except Exception as exc:
                log.error("Odoo query failed: %s", exc)
                fin = AuditFinancials(0, 0, 0, 0, 0, [], source="unavailable", error=str(exc))
        else:
            fin = AuditFinancials(0, 0, 0, 0, 0, [], source="unavailable",
                                  error="Authentication failed")

    # ── 3. Parse bank CSVs ────────────────────────────────────────────────────

    if args.no_bank:
        log.info("Skipping bank CSV (--no-bank)")
        bank_summary = BankSummary(0, 0, 0, 0, [], None, [], [], source="unavailable",
                                   error="Skipped via --no-bank flag")
    else:
        log.info("Parsing bank CSVs from %s …", csv_dir)
        bank_summary = bank.build_summary(days_back)
        if bank_summary.source == "csv":
            log.info(
                "Bank: inflow=$%.2f  outflow=$%.2f  net=$%.2f  (%d txns, %d recurring)",
                bank_summary.total_inflow, abs(bank_summary.total_outflow),
                bank_summary.net_cash_flow, bank_summary.transaction_count,
                len(bank_summary.recurring_charges),
            )
        else:
            log.warning("Bank: %s", bank_summary.error)

    # ── 4. Scan vault ─────────────────────────────────────────────────────────

    log.info("Scanning vault …")
    backlog   = scanner.backlog_items()
    done      = scanner.done_this_week(days_back)
    plans     = scanner.active_plans()
    approvals = scanner.pending_approvals()
    logs      = scanner.parse_logs(days_back)
    log.info(
        "Vault: backlog=%d  done=%d  plans=%d  pending_approvals=%d  log_actions=%d",
        len(backlog), sum(done.values()), len(plans), len(approvals), logs.total_actions,
    )

    # ── 5. Validate ───────────────────────────────────────────────────────────

    validator = DataValidator(odoo, bank, scanner, days_back)
    alerts    = validator.run_all(fin, logs)
    if alerts:
        log.warning("%d alert(s): %s", len(alerts),
                    ", ".join(f"[{a.level}] {a.source}" for a in alerts))

    # ── 6. Generate suggestions ───────────────────────────────────────────────

    suggestions = generate_suggestions(fin, bank_summary, backlog, done, logs, approvals)

    # ── 7. Build reporter & render ────────────────────────────────────────────

    reporter = AuditReporter(
        fin=fin, bank=bank_summary, backlog=backlog, done=done,
        plans=plans, approvals=approvals, logs=logs,
        alerts=alerts, suggestions=suggestions, days_back=days_back,
    )
    briefing_md = reporter.render()

    # ── 8. Write outputs ──────────────────────────────────────────────────────

    if dry_run:
        log.info("[DRY RUN] Briefing preview (first 80 lines):")
        for line in briefing_md.splitlines()[:80]:
            print(line)
        briefing_path = None
    else:
        today = date.today().isoformat()
        if args.output:
            briefing_path = args.output
        else:
            briefing_path = VAULT_PATH / "Briefings" / f"{today}_Weekly_Audit.md"

        reporter.write(briefing_path)

        written_alerts = write_alert_files(alerts, VAULT_PATH, dry_run=False)
        if written_alerts:
            log.info("%d alert file(s) written to Needs_Action/", len(written_alerts))

        update_dashboard(VAULT_PATH, briefing_path, dry_run=False)

    if args.print and not dry_run:
        print(briefing_md)

    # ── 9. Audit log ──────────────────────────────────────────────────────────

    log_audit_run(VAULT_PATH, briefing_path if not dry_run else None,
                  alerts, fin, bank_summary, dry_run)

    # ── 10. Summary ───────────────────────────────────────────────────────────

    log.info(
        "=== Audit complete: %d alert(s), %d suggestion(s), dry_run=%s ===",
        len(alerts), len(suggestions), dry_run,
    )
    return 1 if any(a.level == "ERROR" for a in alerts) else 0


if __name__ == "__main__":
    sys.exit(main())
