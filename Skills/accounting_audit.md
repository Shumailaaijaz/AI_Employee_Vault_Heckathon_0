# Skill: Accounting Audit & Invoice Management

## Purpose
Handle all Odoo accounting tasks: review invoices, record payments, generate AR summaries,
and produce the weekly accounting section of CEO briefings.

## When to Use
- `ODOO_INVOICE_*.md` arrives in /Needs_Action (invoice to create)
- `ODOO_PAYMENT_*.md` arrives in /Needs_Action (payment to record)
- Scheduled weekly accounting audit (Monday morning CEO briefing)
- On-demand request: "audit accounts", "check AR", "invoice <customer>"

## MCP Tools Available
All tools are exposed by the `odoo-accounting` MCP server.

| Tool                     | Description                                              |
|--------------------------|----------------------------------------------------------|
| `odoo_get_customer`      | Look up a customer by name → returns their Odoo ID       |
| `odoo_create_invoice`    | Draft a customer invoice (HITL in DRY_RUN mode)          |
| `odoo_post_payment`      | Record inbound payment against a posted invoice          |
| `odoo_get_balance`       | Get total outstanding AR balance for a customer          |
| `odoo_list_invoices`     | List invoices with filters (state, customer, date range) |
| `odoo_accounting_summary`| Full AR snapshot — use for briefings and audits          |

## Instructions

### A. Creating an Invoice

1. Read the action file to extract: customer, amount, description, due date.
2. Run `odoo_get_customer` to resolve the customer name to an Odoo ID.
   - If no customer found, write to /Pending_Approval asking human to create the contact.
3. Check Company_Handbook.md for the payment threshold (default: amounts > $500 require approval).
4. If amount ≤ threshold AND DRY_RUN=false:
   - Call `odoo_create_invoice` with `confirm=true`.
   - Write result to /Accounting/.
5. If amount > threshold OR DRY_RUN=true:
   - Call `odoo_create_invoice` (goes to /Pending_Approval automatically).
   - Note the approval file path in Dashboard.md.
6. Update Dashboard.md with the invoice status.
7. Move the action file to /Done.

### B. Recording a Payment

1. Read the action file for: invoice_id, amount, payment_date, memo.
2. Run `odoo_list_invoices` to verify the invoice is in POSTED state.
3. Check `odoo_get_balance` to confirm the payment doesn't exceed the outstanding balance.
4. Any payment requires approval (financial action):
   - If action file already came from /Approved → call `odoo_post_payment`.
   - Otherwise → call `odoo_post_payment` in DRY_RUN mode (auto-queues to /Pending_Approval).
5. Update Dashboard.md. Move action file to /Done.

### C. Weekly Accounting Audit

Run every Monday as part of the CEO briefing.

1. Call `odoo_accounting_summary` with `days_back=7`.
2. Call `odoo_list_invoices` with `state=posted` and `days_back=7`.
3. Flag any invoices where `invoice_date_due < today` and `payment_state != paid` as **OVERDUE**.
4. Write a section to the current CEO briefing file under `## Accounting Summary`.
5. If total overdue > $1000: create a /Needs_Action item `ODOO_OVERDUE_FOLLOWUP_<date>.md`.
6. Log the audit to /Logs.

## Output Format (Invoice Action File)

```markdown
---
type: odoo_invoice_result
customer: "<name>"
odoo_invoice_id: <id>
reference: "<INVXXX>"
amount: <float>
currency: USD
status: posted|draft
action_taken: created|queued_for_approval
created: <timestamp>
---

# Invoice: <reference>

- Customer: <name>
- Amount: $<amount>
- Status: <status>
- Action: <what was done>
```

## Output Format (Accounting Audit Section for CEO Briefing)

```markdown
## Accounting Summary (Last 7 Days)

| Metric               | Value        |
|----------------------|-------------|
| Total Invoiced       | $X,XXX.XX   |
| Total Collected      | $X,XXX.XX   |
| Outstanding AR       | $X,XXX.XX   |
| Overdue (>0 days)    | $X,XXX.XX   |
| Invoice Count        | N            |

### Overdue Invoices
- INV/XXXX — Customer Name — $XXX.XX — Due: YYYY-MM-DD

### Action Items
- [ ] Follow up with <customer> on overdue invoice INV/XXXX ($XXX.XX)
```

## Rules
- **NEVER** create or post invoices/payments without checking Company_Handbook.md thresholds first.
- **ALWAYS** log financial actions to /Logs AND write a record to /Accounting.
- **ALWAYS** verify invoice state before recording a payment (must be POSTED).
- **NEVER** record a payment that exceeds the invoice's outstanding balance.
- Payments > $500 ALWAYS require human approval (move to /Pending_Approval).
- New customers (not found in Odoo) → write to /Pending_Approval for human to create the contact.
- If Odoo is unreachable → log the error, write a /Needs_Action retry item, do NOT fail silently.
