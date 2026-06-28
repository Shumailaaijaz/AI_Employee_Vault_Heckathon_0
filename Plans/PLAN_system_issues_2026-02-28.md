---
type: plan
source: email_triage + audit_review
created: 2026-02-28T00:00:00+00:00
priority: high
---

# Plan: System Issues — Weekly Audit Findings (2026-02-25)

## Outstanding Issues

### 1. Odoo Unreachable — ERROR
- **Source**: `weekly_audit.py` run on 2026-02-25
- **Impact**: Financial data missing from weekly report. Odoo MCP server shows HEALTHY in pm2, but the JSON-RPC endpoint may be down.
- **Action Required**: Human must verify Odoo instance is running and `.env` credentials (`ODOO_URL`, `ODOO_DB`, `ODOO_USER`, `ODOO_PASS`) are correct.

### 2. No Bank CSV Found — WARNING
- **Source**: `weekly_audit.py` run on 2026-02-25
- **Impact**: Bank reconciliation and subscription audit cannot run without CSV data.
- **Action Required**: Upload latest bank statement CSV to `Accounting/bank_statements/`.

### 3. Stale Approval (13+ days) — WARNING
- **File**: `Pending_Approval/APPROVAL_email_send_Test_Email_to_Client_20260214_195649.md`
- **Details**: Test email to `test@example.com` created 2026-02-14, never actioned.
- **Action Required**: Move to `/Approved` to execute, or `/Rejected` to cancel. This is a test email — recommend **Reject** if not needed.

## Action Steps
- [ ] Human: Check Odoo instance at `ODOO_URL` — is the server running?
- [ ] Human: Verify `.env` has correct `ODOO_URL`, `ODOO_DB`, `ODOO_USER`, `ODOO_PASS`
- [ ] Human: Upload bank statement CSV to `Accounting/bank_statements/`
- [ ] Human: Resolve stale approval in `Pending_Approval/` (Approve or Reject)
- [ ] Claude: Re-run `weekly_audit.py` once Odoo + bank CSV are in place
- [ ] Claude: Archive this plan to /Done once all items are checked off
