/**
 * Odoo Community 19+ MCP Server
 *
 * Exposes accounting tools to Claude Code via the Odoo JSON-RPC external API.
 * All write operations (create_invoice, post_payment) require human approval
 * through the vault's HITL flow when DRY_RUN=true.
 *
 * Auth setup (one-time):
 *   1. Ensure Odoo is running and accessible at ODOO_URL
 *   2. Create a dedicated API user with Invoicing / Accounting access
 *   3. Set ODOO_URL, ODOO_DB, ODOO_USER, ODOO_PASS in .env
 *   4. Set DRY_RUN=false when ready for real operations
 *
 * Tools:
 *   odoo_get_customer        — Look up a customer by name, return their ID
 *   odoo_create_invoice      — Draft a customer invoice (goes to /Pending_Approval)
 *   odoo_post_payment        — Record an inbound payment against an invoice
 *   odoo_get_balance         — Get a customer's total outstanding (AR) balance
 *   odoo_list_invoices       — List invoices with optional filters
 *   odoo_accounting_summary  — Overall AR snapshot (for CEO briefings/audits)
 *
 * Odoo JSON-RPC endpoints used:
 *   POST /web/session/authenticate   → obtain session_id
 *   POST /web/dataset/call_kw        → all model CRUD operations
 *
 * Environment variables:
 *   ODOO_URL          Odoo base URL (e.g. http://localhost:8069)
 *   ODOO_DB           Database name
 *   ODOO_USER         Login username
 *   ODOO_PASS         Password
 *   VAULT_PATH        Path to Obsidian vault
 *   DRY_RUN           "true" = log only, no real writes (default: true)
 *   ODOO_CURRENCY     ISO currency code for new invoices (default: USD)
 *   ODOO_SALES_JOURNAL  Sales journal name override (optional)
 */

import "dotenv/config";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import fs from "node:fs";
import path from "node:path";

// ── Config ───────────────────────────────────────────────────────────────────

const ODOO_URL    = (process.env.ODOO_URL || "http://localhost:8069").replace(/\/$/, "");
const ODOO_DB     = process.env.ODOO_DB   || "mycompany";
const ODOO_USER   = process.env.ODOO_USER || "admin";
const ODOO_PASS   = process.env.ODOO_PASS || "admin";
const VAULT_PATH  = process.env.VAULT_PATH || "/mnt/d/AI_Employee_Vault/AI_Employee_Vault";
const DRY_RUN     = (process.env.DRY_RUN || "true").toLowerCase() === "true";
const CURRENCY    = process.env.ODOO_CURRENCY || "USD";
const SALES_JOURNAL = process.env.ODOO_SALES_JOURNAL || "";

// ── Session State ────────────────────────────────────────────────────────────

/** In-process session cache — re-authenticated lazily on expiry. */
const session = {
  id: null,      // session_id cookie value
  uid: null,     // authenticated user ID
  expiresAt: 0,  // epoch ms — re-auth after 8 h to be safe
};

const SESSION_TTL_MS = 8 * 60 * 60 * 1000; // 8 hours

// ── JSON-RPC helpers ─────────────────────────────────────────────────────────

let _rpcId = 1;

/**
 * Low-level JSON-RPC 2.0 call to Odoo.
 * Attaches session cookie when available.
 */
async function rpcCall(endpoint, params) {
  const body = JSON.stringify({
    jsonrpc: "2.0",
    method: "call",
    id: _rpcId++,
    params,
  });

  const headers = {
    "Content-Type": "application/json",
    Accept: "application/json",
  };
  if (session.id) {
    headers["Cookie"] = `session_id=${session.id}`;
  }

  const res = await fetch(`${ODOO_URL}${endpoint}`, {
    method: "POST",
    headers,
    body,
  });

  if (!res.ok) {
    throw new Error(`HTTP ${res.status} from Odoo at ${endpoint}: ${await res.text()}`);
  }

  const json = await res.json();

  // JSON-RPC error
  if (json.error) {
    const msg = json.error.data?.message || json.error.message || JSON.stringify(json.error);
    throw new Error(`Odoo RPC error: ${msg}`);
  }

  // Persist session cookie from Set-Cookie header (auth endpoint)
  const setCookie = res.headers.get("set-cookie");
  if (setCookie) {
    const match = setCookie.match(/session_id=([^;]+)/);
    if (match) {
      session.id = match[1];
    }
  }

  return json.result;
}

// ── Authentication ───────────────────────────────────────────────────────────

/**
 * Authenticate (or re-authenticate) with Odoo.
 * Caches session_id + uid for SESSION_TTL_MS.
 */
async function ensureSession() {
  if (session.uid && Date.now() < session.expiresAt) {
    return; // still valid
  }

  const result = await rpcCall("/web/session/authenticate", {
    db: ODOO_DB,
    login: ODOO_USER,
    password: ODOO_PASS,
  });

  if (!result || !result.uid) {
    throw new Error(
      "Odoo authentication failed — check ODOO_DB, ODOO_USER, ODOO_PASS in .env"
    );
  }

  session.uid      = result.uid;
  session.expiresAt = Date.now() + SESSION_TTL_MS;
}

/**
 * Wrapper around rpcCall that ensures a valid session first,
 * and retries once on session-expired errors.
 */
async function odooCall(model, method, args = [], kwargs = {}) {
  await ensureSession();

  const callOnce = () =>
    rpcCall("/web/dataset/call_kw", {
      model,
      method,
      args,
      kwargs,
    });

  try {
    return await callOnce();
  } catch (err) {
    // Session expired mid-operation → re-auth and retry once
    if (/session|auth|login/i.test(err.message)) {
      session.uid = null;
      await ensureSession();
      return await callOnce();
    }
    throw err;
  }
}

// ── Vault helpers ────────────────────────────────────────────────────────────

function writeVaultFile(subdir, filename, content) {
  const dir = path.join(VAULT_PATH, subdir);
  fs.mkdirSync(dir, { recursive: true });
  const filepath = path.join(dir, filename);
  fs.writeFileSync(filepath, content, "utf-8");
  return filepath;
}

function logAction(actionType, details) {
  const dir = path.join(VAULT_PATH, "Logs");
  fs.mkdirSync(dir, { recursive: true });
  const today = new Date().toISOString().slice(0, 10);
  const logFile = path.join(dir, `${today}.json`);

  let entries = [];
  if (fs.existsSync(logFile)) {
    try { entries = JSON.parse(fs.readFileSync(logFile, "utf-8")); }
    catch { entries = []; }
  }

  entries.push({
    timestamp: new Date().toISOString(),
    watcher: "OdooMCP",
    action_type: actionType,
    details,
  });

  fs.writeFileSync(logFile, JSON.stringify(entries, null, 2), "utf-8");
}

// ── Domain helpers ───────────────────────────────────────────────────────────

/**
 * Find a res.partner ID by name (case-insensitive substring search).
 * Returns the first match or throws if none found.
 */
async function findCustomerId(nameOrId) {
  // If it's already a number, use it directly
  if (/^\d+$/.test(String(nameOrId))) {
    return parseInt(nameOrId, 10);
  }

  const partners = await odooCall(
    "res.partner",
    "search_read",
    [[["name", "ilike", nameOrId], ["customer_rank", ">", 0]]],
    { fields: ["id", "name"], limit: 5 }
  );

  if (!partners || partners.length === 0) {
    throw new Error(
      `No customer found matching "${nameOrId}". Use odoo_get_customer to search first.`
    );
  }

  // Prefer exact match, fall back to first result
  const exact = partners.find(
    (p) => p.name.toLowerCase() === nameOrId.toLowerCase()
  );
  return (exact || partners[0]).id;
}

/**
 * Find a journal ID by name (for payment recording).
 */
async function findJournalId(type = "bank") {
  if (SALES_JOURNAL) {
    const journals = await odooCall(
      "account.journal",
      "search_read",
      [[["name", "ilike", SALES_JOURNAL]]],
      { fields: ["id", "name"], limit: 1 }
    );
    if (journals?.length) return journals[0].id;
  }

  // Default: first bank/cash journal
  const journals = await odooCall(
    "account.journal",
    "search_read",
    [[["type", "in", [type, "cash"]]]],
    { fields: ["id", "name", "type"], limit: 5 }
  );
  if (!journals?.length) {
    throw new Error(`No ${type} journal found in Odoo. Create one in Accounting > Configuration.`);
  }
  // Prefer bank over cash
  return (journals.find((j) => j.type === "bank") || journals[0]).id;
}

// ── MCP Server ───────────────────────────────────────────────────────────────

const server = new McpServer({
  name: "odoo-accounting",
  version: "1.0.0",
});

// ── Tool: odoo_get_customer ──────────────────────────────────────────────────

server.tool(
  "odoo_get_customer",
  "Search for a customer in Odoo by name. Returns their ID, full name, email, and phone. Use this to resolve a customer name to an ID before creating invoices.",
  {
    name: z.string().min(1).describe("Customer name to search (partial match supported)"),
  },
  async ({ name }) => {
    try {
      await ensureSession();
      const partners = await odooCall(
        "res.partner",
        "search_read",
        [[["name", "ilike", name], ["customer_rank", ">", 0]]],
        { fields: ["id", "name", "email", "phone", "city", "country_id"], limit: 10 }
      );

      if (!partners?.length) {
        return {
          content: [{ type: "text", text: `No customers found matching "${name}".` }],
        };
      }

      const rows = partners.map(
        (p) =>
          `ID: ${p.id}  Name: ${p.name}  Email: ${p.email || "-"}  Phone: ${p.phone || "-"}  City: ${p.city || "-"}`
      );

      return {
        content: [
          {
            type: "text",
            text: `Found ${partners.length} customer(s) matching "${name}":\n\n${rows.join("\n")}`,
          },
        ],
      };
    } catch (err) {
      return { content: [{ type: "text", text: `Error: ${err.message}` }], isError: true };
    }
  }
);

// ── Tool: odoo_create_invoice ────────────────────────────────────────────────

server.tool(
  "odoo_create_invoice",
  `Create a customer invoice in Odoo. In DRY_RUN mode the invoice spec is saved to /Pending_Approval for human review.
Once approved (file moved to /Approved), call this tool again with dry_run_override=false to execute.
The invoice is created in DRAFT state. It must be confirmed (action_post) before payment can be recorded.`,
  {
    customer: z
      .string()
      .describe("Customer name (will be looked up by name) or numeric Odoo partner ID"),
    amount: z
      .number()
      .positive()
      .describe("Invoice total amount (before tax, in the account's default currency)"),
    description: z
      .string()
      .min(1)
      .describe("Line item description (what is being invoiced)"),
    invoice_date: z
      .string()
      .optional()
      .describe("Invoice date in YYYY-MM-DD format (defaults to today)"),
    due_date: z
      .string()
      .optional()
      .describe("Payment due date in YYYY-MM-DD format (defaults to invoice_date + 30 days)"),
    currency: z
      .string()
      .default(CURRENCY)
      .describe(`ISO currency code (default: ${CURRENCY})`),
    confirm: z
      .boolean()
      .default(false)
      .describe("If true, also confirm (post) the invoice immediately after creation"),
  },
  async ({ customer, amount, description, invoice_date, due_date, currency, confirm }) => {
    const now       = new Date();
    const today     = now.toISOString().slice(0, 10);
    const invDate   = invoice_date || today;
    const dueDate   = due_date || (() => {
      const d = new Date(invDate);
      d.setDate(d.getDate() + 30);
      return d.toISOString().slice(0, 10);
    })();

    if (DRY_RUN) {
      const ts       = now.toISOString().slice(0, 19).replace(/[T:]/g, "-");
      const safeName = String(customer).slice(0, 40).replace(/[^a-zA-Z0-9 ]/g, "").trim().replace(/ /g, "_");
      const filename = `ODOO_INVOICE_${safeName}_${ts}.md`;

      const content = `---
type: odoo_invoice
customer: "${customer}"
amount: ${amount}
currency: ${currency}
description: "${description}"
invoice_date: "${invDate}"
due_date: "${dueDate}"
confirm_on_create: ${confirm}
status: pending_approval
created: ${now.toISOString()}
dry_run: true
---

# Odoo Invoice Draft — Pending Approval

## Invoice Details
- **Customer**: ${customer}
- **Amount**: ${currency} ${amount.toFixed(2)}
- **Description**: ${description}
- **Invoice Date**: ${invDate}
- **Due Date**: ${dueDate}
- **Auto-Confirm**: ${confirm ? "Yes (will be posted immediately)" : "No (stays in Draft)"}

## Action Required
Move this file to /Approved, then call Claude to execute the invoice creation.

## To Approve
Move this file to \`/Approved\` folder and ask Claude to execute the Odoo invoice.

## To Reject
Move this file to \`/Rejected\` folder.
`;

      const filepath = writeVaultFile("Pending_Approval", filename, content);
      logAction("odoo_invoice_queued", { customer, amount, currency, description, filepath });

      return {
        content: [
          {
            type: "text",
            text: `[DRY RUN] Invoice draft saved to:\n${filepath}\n\nDetails:\n  Customer: ${customer}\n  Amount: ${currency} ${amount.toFixed(2)}\n  Description: ${description}\n  Invoice Date: ${invDate}\n  Due Date: ${dueDate}\n\nMove the file to /Approved and ask Claude to execute.`,
          },
        ],
      };
    }

    // Real execution
    try {
      const partnerId = await findCustomerId(customer);

      // Look up currency ID
      const currencies = await odooCall(
        "res.currency",
        "search_read",
        [[["name", "=", currency.toUpperCase()]]],
        { fields: ["id", "name"], limit: 1 }
      );
      const currencyId = currencies?.[0]?.id;

      const invoiceVals = {
        move_type: "out_invoice",
        partner_id: partnerId,
        invoice_date: invDate,
        invoice_date_due: dueDate,
        invoice_line_ids: [
          [0, 0, {
            name: description,
            quantity: 1.0,
            price_unit: amount,
          }],
        ],
      };
      if (currencyId) {
        invoiceVals.currency_id = currencyId;
      }

      const invoiceId = await odooCall("account.move", "create", [invoiceVals]);

      let status = "draft";
      if (confirm) {
        await odooCall("account.move", "action_post", [[invoiceId]]);
        status = "posted";
      }

      // Fetch the created invoice's reference number
      const [invoice] = await odooCall(
        "account.move",
        "read",
        [[invoiceId]],
        { fields: ["name", "amount_total", "state"] }
      );

      logAction("odoo_invoice_created", {
        invoice_id: invoiceId,
        name: invoice.name,
        partner_id: partnerId,
        amount,
        currency,
        status,
      });

      writeVaultFile("Accounting", `INVOICE_${invoice.name.replace(/\//g, "-")}_${invDate}.md`,
`---
type: odoo_invoice_created
odoo_invoice_id: ${invoiceId}
reference: "${invoice.name}"
customer: "${customer}"
partner_id: ${partnerId}
amount: ${amount}
currency: ${currency}
state: "${status}"
invoice_date: "${invDate}"
due_date: "${dueDate}"
created: ${now.toISOString()}
---

# Invoice Created: ${invoice.name}

- **Odoo ID**: ${invoiceId}
- **Customer**: ${customer} (ID: ${partnerId})
- **Amount**: ${currency} ${amount.toFixed(2)}
- **Description**: ${description}
- **Status**: ${status.toUpperCase()}
- **Invoice Date**: ${invDate}
- **Due Date**: ${dueDate}
`);

      return {
        content: [
          {
            type: "text",
            text: `Invoice created in Odoo!\n\n  Reference: ${invoice.name}\n  Odoo ID:   ${invoiceId}\n  Customer:  ${customer} (ID: ${partnerId})\n  Amount:    ${currency} ${amount.toFixed(2)}\n  Status:    ${status.toUpperCase()}\n  Due:       ${dueDate}\n\nRecord saved to /Accounting.`,
          },
        ],
      };
    } catch (err) {
      return { content: [{ type: "text", text: `Invoice creation failed: ${err.message}` }], isError: true };
    }
  }
);

// ── Tool: odoo_post_payment ──────────────────────────────────────────────────

server.tool(
  "odoo_post_payment",
  `Record an inbound payment from a customer against a specific invoice in Odoo.
In DRY_RUN mode the payment spec is saved to /Pending_Approval for human review.
The invoice must be in POSTED (confirmed) state before payment can be applied.`,
  {
    invoice_id: z
      .number()
      .int()
      .positive()
      .describe("Odoo invoice ID (numeric, from odoo_create_invoice or odoo_list_invoices)"),
    amount: z
      .number()
      .positive()
      .describe("Payment amount (must be <= invoice outstanding balance)"),
    payment_date: z
      .string()
      .optional()
      .describe("Payment date in YYYY-MM-DD format (defaults to today)"),
    memo: z
      .string()
      .optional()
      .describe("Payment reference/memo (e.g. bank transfer ref, cheque number)"),
  },
  async ({ invoice_id, amount, payment_date, memo }) => {
    const now      = new Date();
    const today    = now.toISOString().slice(0, 10);
    const payDate  = payment_date || today;

    if (DRY_RUN) {
      const ts       = now.toISOString().slice(0, 19).replace(/[T:]/g, "-");
      const filename = `ODOO_PAYMENT_INV${invoice_id}_${ts}.md`;

      const content = `---
type: odoo_payment
invoice_id: ${invoice_id}
amount: ${amount}
payment_date: "${payDate}"
memo: "${memo || ""}"
status: pending_approval
created: ${now.toISOString()}
dry_run: true
---

# Odoo Payment — Pending Approval

## Payment Details
- **Invoice ID**: ${invoice_id}
- **Amount**: ${amount.toFixed(2)}
- **Payment Date**: ${payDate}
- **Memo/Reference**: ${memo || "(none)"}

## Action Required
Move this file to /Approved, then ask Claude to execute the payment recording.

## Warning
The invoice must be in POSTED state before payment can be recorded.
Use odoo_list_invoices to verify the invoice state first.
`;

      const filepath = writeVaultFile("Pending_Approval", filename, content);
      logAction("odoo_payment_queued", { invoice_id, amount, payment_date: payDate, filepath });

      return {
        content: [
          {
            type: "text",
            text: `[DRY RUN] Payment draft saved to:\n${filepath}\n\n  Invoice ID: ${invoice_id}\n  Amount:     ${amount.toFixed(2)}\n  Date:       ${payDate}\n  Memo:       ${memo || "(none)"}\n\nMove the file to /Approved and ask Claude to execute.`,
          },
        ],
      };
    }

    // Real execution
    try {
      // Fetch invoice details first
      const [invoice] = await odooCall(
        "account.move",
        "read",
        [[invoice_id]],
        { fields: ["name", "state", "amount_residual", "partner_id", "currency_id"] }
      );

      if (!invoice) {
        throw new Error(`Invoice ID ${invoice_id} not found in Odoo.`);
      }
      if (invoice.state !== "posted") {
        throw new Error(
          `Invoice ${invoice.name} is in state "${invoice.state}". ` +
          "Only POSTED invoices can receive payments. Confirm the invoice first."
        );
      }
      if (amount > invoice.amount_residual + 0.01) {
        throw new Error(
          `Payment amount ${amount} exceeds invoice outstanding balance ` +
          `${invoice.amount_residual} for invoice ${invoice.name}.`
        );
      }

      const partnerId  = Array.isArray(invoice.partner_id) ? invoice.partner_id[0] : invoice.partner_id;
      const currencyId = Array.isArray(invoice.currency_id) ? invoice.currency_id[0] : invoice.currency_id;
      const journalId  = await findJournalId("bank");

      // Create and post the payment
      const paymentVals = {
        payment_type: "inbound",
        partner_type: "customer",
        partner_id: partnerId,
        amount,
        date: payDate,
        journal_id: journalId,
        currency_id: currencyId,
        ref: memo || `Payment for ${invoice.name}`,
      };

      const paymentId = await odooCall("account.payment", "create", [paymentVals]);
      // Confirm the payment (posts it to the journal)
      await odooCall("account.payment", "action_post", [[paymentId]]);

      // Reconcile payment with the invoice
      // Get the payment's journal items (move lines)
      const paymentMoves = await odooCall(
        "account.payment",
        "read",
        [[paymentId]],
        { fields: ["move_id", "name"] }
      );
      const paymentName = paymentMoves?.[0]?.name || `payment:${paymentId}`;

      logAction("odoo_payment_posted", {
        payment_id: paymentId,
        invoice_id,
        invoice_name: invoice.name,
        amount,
        payment_date: payDate,
        partner_id: partnerId,
      });

      writeVaultFile(
        "Accounting",
        `PAYMENT_${paymentName.replace(/\//g, "-")}_${payDate}.md`,
`---
type: odoo_payment_posted
odoo_payment_id: ${paymentId}
invoice_id: ${invoice_id}
invoice_reference: "${invoice.name}"
partner_id: ${partnerId}
amount: ${amount}
payment_date: "${payDate}"
memo: "${memo || ""}"
created: ${now.toISOString()}
---

# Payment Posted: ${paymentName}

- **Odoo Payment ID**: ${paymentId}
- **Invoice**: ${invoice.name} (ID: ${invoice_id})
- **Amount Paid**: ${amount.toFixed(2)}
- **Payment Date**: ${payDate}
- **Reference**: ${memo || `Payment for ${invoice.name}`}
`
      );

      return {
        content: [
          {
            type: "text",
            text: `Payment posted in Odoo!\n\n  Payment ID: ${paymentId}\n  Invoice:    ${invoice.name} (ID: ${invoice_id})\n  Amount:     ${amount.toFixed(2)}\n  Date:       ${payDate}\n  Memo:       ${memo || "(none)"}\n\nRecord saved to /Accounting.`,
          },
        ],
      };
    } catch (err) {
      return { content: [{ type: "text", text: `Payment failed: ${err.message}` }], isError: true };
    }
  }
);

// ── Tool: odoo_get_balance ───────────────────────────────────────────────────

server.tool(
  "odoo_get_balance",
  "Get the total outstanding (unpaid) accounts-receivable balance for a customer. Returns the sum of all posted, unpaid invoice residuals.",
  {
    customer: z
      .string()
      .describe("Customer name (partial match) or numeric Odoo partner ID"),
  },
  async ({ customer }) => {
    try {
      const partnerId = await findCustomerId(customer);

      // Fetch the partner name for display
      const [partner] = await odooCall(
        "res.partner",
        "read",
        [[partnerId]],
        { fields: ["name", "email"] }
      );

      // All posted (confirmed) customer invoices with outstanding balance
      const invoices = await odooCall(
        "account.move",
        "search_read",
        [[
          ["partner_id", "=", partnerId],
          ["move_type", "=", "out_invoice"],
          ["state",     "=", "posted"],
          ["payment_state", "not in", ["paid", "reversed"]],
        ]],
        { fields: ["name", "amount_total", "amount_residual", "invoice_date_due", "payment_state"] }
      );

      const totalBalance = invoices.reduce((sum, inv) => sum + (inv.amount_residual || 0), 0);

      if (!invoices.length) {
        return {
          content: [
            {
              type: "text",
              text: `Customer: ${partner.name} (ID: ${partnerId})\n\nNo outstanding invoices. Balance: ${CURRENCY} 0.00`,
            },
          ],
        };
      }

      const lines = invoices.map(
        (inv) =>
          `  ${inv.name.padEnd(16)} Due: ${inv.invoice_date_due || "n/a"}  ` +
          `Outstanding: ${inv.amount_residual.toFixed(2)}  ` +
          `(${inv.payment_state})`
      );

      return {
        content: [
          {
            type: "text",
            text: [
              `Customer: ${partner.name} (ID: ${partnerId})  Email: ${partner.email || "-"}`,
              ``,
              `Outstanding Invoices (${invoices.length}):`,
              ...lines,
              ``,
              `Total Outstanding Balance: ${CURRENCY} ${totalBalance.toFixed(2)}`,
            ].join("\n"),
          },
        ],
      };
    } catch (err) {
      return { content: [{ type: "text", text: `Error: ${err.message}` }], isError: true };
    }
  }
);

// ── Tool: odoo_list_invoices ─────────────────────────────────────────────────

server.tool(
  "odoo_list_invoices",
  "List customer invoices from Odoo with optional filters. Useful for audits, briefings, and finding invoice IDs for payments.",
  {
    customer: z
      .string()
      .optional()
      .describe("Filter by customer name or ID (optional)"),
    state: z
      .enum(["draft", "posted", "cancel", "all"])
      .default("all")
      .describe("Invoice state filter: draft, posted (confirmed), cancel, or all"),
    limit: z
      .number()
      .int()
      .min(1)
      .max(100)
      .default(20)
      .describe("Maximum number of invoices to return (default: 20)"),
    days_back: z
      .number()
      .int()
      .min(1)
      .max(365)
      .optional()
      .describe("Only return invoices from the last N days (optional)"),
  },
  async ({ customer, state, limit, days_back }) => {
    try {
      const domain = [["move_type", "=", "out_invoice"]];

      if (customer) {
        const pid = await findCustomerId(customer);
        domain.push(["partner_id", "=", pid]);
      }

      if (state !== "all") {
        domain.push(["state", "=", state]);
      }

      if (days_back) {
        const since = new Date();
        since.setDate(since.getDate() - days_back);
        domain.push(["invoice_date", ">=", since.toISOString().slice(0, 10)]);
      }

      const invoices = await odooCall(
        "account.move",
        "search_read",
        [domain],
        {
          fields: [
            "name", "partner_id", "invoice_date", "invoice_date_due",
            "amount_total", "amount_residual", "state", "payment_state",
            "currency_id",
          ],
          limit,
          order: "invoice_date desc, id desc",
        }
      );

      if (!invoices?.length) {
        return {
          content: [{ type: "text", text: "No invoices found matching the given filters." }],
        };
      }

      const rows = invoices.map((inv) => {
        const partner = Array.isArray(inv.partner_id) ? inv.partner_id[1] : "-";
        const currency = Array.isArray(inv.currency_id) ? inv.currency_id[1] : CURRENCY;
        return (
          `ID:${inv.id}  ${inv.name.padEnd(16)} ` +
          `${partner.slice(0, 20).padEnd(20)} ` +
          `${inv.invoice_date || "-"}  ` +
          `Total:${currency} ${(inv.amount_total || 0).toFixed(2).padStart(10)}  ` +
          `Due:${currency} ${(inv.amount_residual || 0).toFixed(2).padStart(10)}  ` +
          `[${inv.state}/${inv.payment_state}]`
        );
      });

      return {
        content: [
          {
            type: "text",
            text: `${invoices.length} invoice(s) found:\n\n${rows.join("\n")}`,
          },
        ],
      };
    } catch (err) {
      return { content: [{ type: "text", text: `Error: ${err.message}` }], isError: true };
    }
  }
);

// ── Tool: odoo_accounting_summary ────────────────────────────────────────────

server.tool(
  "odoo_accounting_summary",
  `Generate a snapshot of the company's accounts-receivable position.
Returns: total invoiced, total collected, total outstanding, overdue amount, and top debtors.
Ideal for weekly CEO briefings and accounting audits.`,
  {
    days_back: z
      .number()
      .int()
      .min(1)
      .max(365)
      .default(30)
      .describe("Summarise invoices from the last N days (default: 30)"),
  },
  async ({ days_back }) => {
    try {
      const since = new Date();
      since.setDate(since.getDate() - days_back);
      const sinceStr = since.toISOString().slice(0, 10);
      const today    = new Date().toISOString().slice(0, 10);

      // All posted invoices in the period
      const invoices = await odooCall(
        "account.move",
        "search_read",
        [[
          ["move_type",    "=",  "out_invoice"],
          ["state",        "=",  "posted"],
          ["invoice_date", ">=", sinceStr],
        ]],
        {
          fields: [
            "name", "partner_id", "invoice_date", "invoice_date_due",
            "amount_total", "amount_residual", "payment_state",
          ],
          limit: 500,
        }
      );

      const totalInvoiced  = invoices.reduce((s, i) => s + (i.amount_total    || 0), 0);
      const totalOutstanding = invoices.reduce((s, i) => s + (i.amount_residual || 0), 0);
      const totalCollected = totalInvoiced - totalOutstanding;

      // Overdue = outstanding invoices where due date < today
      const overdueInvoices = invoices.filter(
        (i) => i.amount_residual > 0 && i.invoice_date_due && i.invoice_date_due < today
      );
      const totalOverdue = overdueInvoices.reduce((s, i) => s + (i.amount_residual || 0), 0);

      // Top debtors by outstanding balance
      const debtorMap = new Map();
      for (const inv of invoices) {
        if (inv.amount_residual <= 0) continue;
        const name = Array.isArray(inv.partner_id) ? inv.partner_id[1] : `ID:${inv.partner_id}`;
        debtorMap.set(name, (debtorMap.get(name) || 0) + inv.amount_residual);
      }
      const topDebtors = [...debtorMap.entries()]
        .sort((a, b) => b[1] - a[1])
        .slice(0, 5);

      const topDebtorLines = topDebtors.length
        ? topDebtors.map(([name, bal]) => `    ${name.slice(0, 30).padEnd(30)} ${CURRENCY} ${bal.toFixed(2)}`).join("\n")
        : "    (none)";

      const summary = [
        `Odoo Accounting Summary — Last ${days_back} Days (${sinceStr} → ${today})`,
        `${"─".repeat(60)}`,
        `  Total Invoiced:    ${CURRENCY} ${totalInvoiced.toFixed(2)}`,
        `  Total Collected:   ${CURRENCY} ${totalCollected.toFixed(2)}`,
        `  Outstanding (AR):  ${CURRENCY} ${totalOutstanding.toFixed(2)}`,
        `  Overdue:           ${CURRENCY} ${totalOverdue.toFixed(2)}  (${overdueInvoices.length} invoice(s))`,
        `  Invoice Count:     ${invoices.length}`,
        ``,
        `Top Debtors:`,
        topDebtorLines,
      ].join("\n");

      logAction("odoo_accounting_summary", {
        period_days: days_back,
        total_invoiced: totalInvoiced,
        total_outstanding: totalOutstanding,
        total_overdue: totalOverdue,
      });

      return { content: [{ type: "text", text: summary }] };
    } catch (err) {
      return { content: [{ type: "text", text: `Error: ${err.message}` }], isError: true };
    }
  }
);

// ── Start server ─────────────────────────────────────────────────────────────

async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
}

main().catch((err) => {
  console.error("Odoo MCP server failed to start:", err);
  process.exit(1);
});
