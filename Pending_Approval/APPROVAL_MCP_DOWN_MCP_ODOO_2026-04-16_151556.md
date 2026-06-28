---
type: mcp_server_down
server_name: "Odoo-Accounting MCP"
pm2_app: "mcp-odoo"
consecutive_failures: 4
tools_affected: ['odoo_get_customer', 'odoo_create_invoice', 'odoo_post_payment', 'odoo_get_balance', 'odoo_list_invoices', 'odoo_accounting_summary']
fallback_mode: queue
detected: 2026-04-16T15:15:56.107113+00:00
status: pending_approval
---

# MCP Server Down: Odoo-Accounting MCP

The `mcp-odoo` MCP server has been unresponsive for
4 consecutive health checks.

## Affected Tools
- `odoo_get_customer`
- `odoo_create_invoice`
- `odoo_post_payment`
- `odoo_get_balance`
- `odoo_list_invoices`
- `odoo_accounting_summary`

## What to Do
1. Check logs: `pm2 logs mcp-odoo`
2. Restart: `pm2 restart mcp-odoo`
3. If credentials expired, update `.env` and restart.
4. Once fixed, move this file to `/Done`.

## Queued Actions
Any actions requiring these tools have been queued here in
/Pending_Approval until the server is restored.
