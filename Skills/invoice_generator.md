# Skill: Invoice Generation

## Purpose
Generate professional invoices for clients based on project data and rate information, and route for approval before sending.

## When to Use
- When a WhatsApp/email message requests an invoice
- When a project milestone is completed
- On demand: "Generate invoice for Client X"

## Instructions

1. **Gather client info** from:
   - The triggering message (client name, project reference)
   - /Accounting/Rates.md (hourly rates, project fees)
   - /Active_Project/ (project details, milestones)
   - /Accounting/Clients/ (past invoices, client details)

2. **Calculate the amount**:
   - For hourly: hours worked x rate
   - For fixed-price: milestone amount from project agreement
   - Apply any discounts or taxes as configured

3. **Generate invoice content** in markdown (see template below).

4. **Write to /Pending_Approval/**:
   ```
   /Pending_Approval/INVOICE_<client>_<YYYY-MM>.md
   ```

5. **After approval**, the invoice should be:
   - Moved to /Accounting/Invoices/
   - Sent to client via email (requires email MCP)
   - Logged in /Accounting/Current_Month.md

## Output Format
```markdown
---
type: invoice
invoice_number: INV-<YYYY>-<sequential>
client: "<client name>"
client_email: "<email>"
amount: <total>
currency: USD
due_date: <date + 30 days>
status: pending_approval
created: <timestamp>
---

# Invoice INV-<number>

**From:** <Your Business Name>
**To:** <Client Name>
**Date:** <today>
**Due:** <due date>

## Line Items
| Description | Qty | Rate | Amount |
|-------------|-----|------|--------|
| <service> | <qty> | $<rate> | $<amount> |

**Subtotal:** $<subtotal>
**Tax (X%):** $<tax>
**Total Due:** $<total>

## Payment Instructions
<payment details from Company_Handbook.md>

## Notes
<any project-specific notes>
```

## Rules
- NEVER send an invoice without approval
- Verify the amount matches /Accounting/Rates.md
- Include a unique invoice number (check /Accounting/Invoices/ for last used)
- Default payment terms: Net 30
- Flag any invoice over $500 as HIGH PRIORITY in the approval request
