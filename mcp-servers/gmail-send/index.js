/**
 * Gmail Send MCP Server
 *
 * Exposes tools for Claude Code to draft and send emails via the Gmail API.
 * All sends go through the vault's HITL approval flow when DRY_RUN=true.
 *
 * Tools:
 *   send_email    — Send an email (requires approval file in /Approved)
 *   draft_email   — Draft an email and save to /Pending_Approval for human review
 *   list_emails   — List recent emails from inbox (read-only, no approval needed)
 */

import "dotenv/config";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import { google } from "googleapis";
import fs from "node:fs";
import path from "node:path";

// ── Config ──────────────────────────────────────────────────────────────────

const VAULT_PATH = process.env.VAULT_PATH || "/mnt/d/AI_Employee_Vault/AI_Employee_Vault";
const FROM_ADDRESS = process.env.GMAIL_FROM_ADDRESS || "";
const DRY_RUN = (process.env.DRY_RUN || "true").toLowerCase() === "true";

// ── Gmail API Setup ─────────────────────────────────────────────────────────

const oauth2Client = new google.auth.OAuth2(
  process.env.GMAIL_CLIENT_ID,
  process.env.GMAIL_CLIENT_SECRET,
  "http://localhost:3457/callback"
);

oauth2Client.setCredentials({
  access_token: process.env.GMAIL_ACCESS_TOKEN,
  refresh_token: process.env.GMAIL_REFRESH_TOKEN,
});

// Auto-refresh: when the access token expires, googleapis uses the refresh
// token automatically. Persist the new token if needed.
oauth2Client.on("tokens", (tokens) => {
  if (tokens.access_token) {
    // Log token refresh (don't persist to .env automatically for security)
    writeLog("token_refreshed", { expires: tokens.expiry_date });
  }
});

const gmail = google.gmail({ version: "v1", auth: oauth2Client });

// ── Helpers ─────────────────────────────────────────────────────────────────

/**
 * Build a RFC 2822 compliant email message and Base64url encode it.
 */
function buildRawEmail(to, subject, body, cc = "", bcc = "") {
  const lines = [
    `From: ${FROM_ADDRESS}`,
    `To: ${to}`,
  ];
  if (cc) lines.push(`Cc: ${cc}`);
  if (bcc) lines.push(`Bcc: ${bcc}`);
  lines.push(
    `Subject: ${subject}`,
    "MIME-Version: 1.0",
    'Content-Type: text/plain; charset="UTF-8"',
    "",
    body
  );

  const raw = lines.join("\r\n");
  return Buffer.from(raw).toString("base64url");
}

/**
 * Write a file to a vault subdirectory.
 */
function writeVaultFile(subdir, filename, content) {
  const dir = path.join(VAULT_PATH, subdir);
  fs.mkdirSync(dir, { recursive: true });
  const filepath = path.join(dir, filename);
  fs.writeFileSync(filepath, content, "utf-8");
  return filepath;
}

/**
 * Append an entry to today's log.
 */
function writeLog(actionType, details) {
  const dir = path.join(VAULT_PATH, "Logs");
  fs.mkdirSync(dir, { recursive: true });
  const today = new Date().toISOString().slice(0, 10);
  const logFile = path.join(dir, `${today}.json`);

  let entries = [];
  if (fs.existsSync(logFile)) {
    try { entries = JSON.parse(fs.readFileSync(logFile, "utf-8")); } catch { entries = []; }
  }

  entries.push({
    timestamp: new Date().toISOString(),
    watcher: "GmailMCP",
    action_type: actionType,
    details,
  });

  fs.writeFileSync(logFile, JSON.stringify(entries, null, 2), "utf-8");
}

// ── MCP Server ──────────────────────────────────────────────────────────────

const server = new McpServer({
  name: "gmail-send",
  version: "1.0.0",
});

// ── Tool: draft_email ───────────────────────────────────────────────────────

server.tool(
  "draft_email",
  `Draft an email and save it to /Pending_Approval for human review.
The email is NOT sent until approved and send_email is called.
Use this for all outgoing emails — the HITL workflow ensures nothing is sent without review.`,
  {
    to: z.string().email().describe("Recipient email address"),
    subject: z.string().min(1).max(500).describe("Email subject line"),
    body: z.string().min(1).describe("Email body text (plain text)"),
    cc: z.string().optional().describe("CC recipients (comma-separated emails)"),
    commentary: z.string().optional().describe("Internal note explaining why this email is being sent (not included in email)"),
  },
  async ({ to, subject, body, cc, commentary }) => {
    try {
      const now = new Date();
      const ts = now.toISOString().slice(0, 19).replace(/[T:]/g, "-");
      const safeTo = to.split("@")[0].replace(/[^a-zA-Z0-9]/g, "_");
      const filename = `EMAIL_SEND_${safeTo}_${ts}.md`;

      const content = `---
type: email_send
to: "${to}"
subject: "${subject}"
${cc ? `cc: "${cc}"` : ""}
from: "${FROM_ADDRESS}"
status: pending_approval
created: ${now.toISOString()}
dry_run: ${DRY_RUN}
---

# Email Draft

## To
${to}${cc ? `\n\n## CC\n${cc}` : ""}

## Subject
${subject}

## Body
${body}

${commentary ? `## Internal Note\n${commentary}\n` : ""}
## To Approve
Move this file to /Approved folder, then ask Claude to send it.

## To Reject
Move this file to /Rejected folder.
`;

      const filepath = writeVaultFile("Pending_Approval", filename, content);
      writeLog("email_drafted", { to, subject, filename });

      return {
        content: [{
          type: "text",
          text: `Draft saved to: ${filepath}\n\nTo: ${to}\nSubject: ${subject}\nBody preview: ${body.slice(0, 100)}${body.length > 100 ? "..." : ""}\n\nMove the file to /Approved and call send_email to send.`,
        }],
      };
    } catch (err) {
      return { content: [{ type: "text", text: `Error: ${err.message}` }], isError: true };
    }
  }
);

// ── Tool: send_email ────────────────────────────────────────────────────────

server.tool(
  "send_email",
  `Send an email via Gmail. Requires a matching approved file in /Approved.
In DRY_RUN mode this logs the action but does NOT actually send.
ALWAYS use draft_email first and wait for human approval before calling this.`,
  {
    to: z.string().email().describe("Recipient email address"),
    subject: z.string().min(1).max(500).describe("Email subject line"),
    body: z.string().min(1).describe("Email body text (plain text)"),
    cc: z.string().optional().describe("CC recipients (comma-separated)"),
    bcc: z.string().optional().describe("BCC recipients (comma-separated)"),
  },
  async ({ to, subject, body, cc, bcc }) => {
    // Check for approval file
    const approvedDir = path.join(VAULT_PATH, "Approved");
    let approvedFile = null;

    if (fs.existsSync(approvedDir)) {
      const files = fs.readdirSync(approvedDir).filter((f) => f.startsWith("EMAIL_SEND_"));
      for (const f of files) {
        const content = fs.readFileSync(path.join(approvedDir, f), "utf-8");
        if (content.includes(to) && content.includes(subject.slice(0, 60))) {
          approvedFile = f;
          break;
        }
      }
    }

    if (!approvedFile) {
      return {
        content: [{
          type: "text",
          text: "BLOCKED: No matching approved file found in /Approved.\nDraft the email first with draft_email, then move the file from /Pending_Approval to /Approved before sending.",
        }],
        isError: true,
      };
    }

    // DRY RUN
    if (DRY_RUN) {
      writeLog("email_send_dry_run", { to, subject, approved_file: approvedFile });

      const doneDir = path.join(VAULT_PATH, "Done");
      fs.mkdirSync(doneDir, { recursive: true });
      fs.renameSync(path.join(approvedDir, approvedFile), path.join(doneDir, approvedFile));

      return {
        content: [{
          type: "text",
          text: `[DRY RUN] Would send email:\n  To: ${to}\n  Subject: ${subject}\n  Body: ${body.slice(0, 100)}...\nApproval file moved to /Done.\nSet DRY_RUN=false in .env to send for real.`,
        }],
      };
    }

    // REAL SEND
    try {
      const raw = buildRawEmail(to, subject, body, cc || "", bcc || "");

      const result = await gmail.users.messages.send({
        userId: "me",
        requestBody: { raw },
      });

      // Move approval file to Done
      const doneDir = path.join(VAULT_PATH, "Done");
      fs.mkdirSync(doneDir, { recursive: true });
      fs.renameSync(path.join(approvedDir, approvedFile), path.join(doneDir, approvedFile));

      writeLog("email_sent", {
        to,
        subject,
        message_id: result.data.id,
        thread_id: result.data.threadId,
        approved_file: approvedFile,
      });

      return {
        content: [{
          type: "text",
          text: `Email sent!\n  To: ${to}\n  Subject: ${subject}\n  Gmail Message ID: ${result.data.id}\n  Thread ID: ${result.data.threadId}\nApproval file moved to /Done.`,
        }],
      };
    } catch (err) {
      writeLog("email_send_error", { to, subject, error: err.message });
      return { content: [{ type: "text", text: `Send failed: ${err.message}` }], isError: true };
    }
  }
);

// ── Tool: list_emails ───────────────────────────────────────────────────────

server.tool(
  "list_emails",
  "List recent emails from Gmail inbox. Read-only, no approval needed. Useful for checking what emails have arrived.",
  {
    query: z
      .string()
      .default("is:inbox")
      .describe('Gmail search query (e.g., "is:unread", "from:client@example.com", "subject:invoice")'),
    max_results: z
      .number()
      .int()
      .min(1)
      .max(20)
      .default(10)
      .describe("Maximum number of emails to return (1-20)"),
  },
  async ({ query, max_results }) => {
    try {
      const res = await gmail.users.messages.list({
        userId: "me",
        q: query,
        maxResults: max_results,
      });

      const messages = res.data.messages || [];

      if (messages.length === 0) {
        return {
          content: [{ type: "text", text: `No emails found for query: "${query}"` }],
        };
      }

      // Fetch details for each message
      const details = [];
      for (const msg of messages.slice(0, max_results)) {
        const detail = await gmail.users.messages.get({
          userId: "me",
          id: msg.id,
          format: "metadata",
          metadataHeaders: ["From", "To", "Subject", "Date"],
        });

        const headers = {};
        for (const h of detail.data.payload.headers) {
          headers[h.name] = h.value;
        }

        details.push({
          id: msg.id,
          from: headers.From || "Unknown",
          to: headers.To || "Unknown",
          subject: headers.Subject || "(No Subject)",
          date: headers.Date || "Unknown",
          snippet: detail.data.snippet || "",
        });
      }

      const output = details
        .map(
          (d, i) =>
            `${i + 1}. **${d.subject}**\n   From: ${d.from}\n   Date: ${d.date}\n   Preview: ${d.snippet.slice(0, 120)}...\n   ID: ${d.id}`
        )
        .join("\n\n");

      writeLog("emails_listed", { query, count: details.length });

      return {
        content: [{
          type: "text",
          text: `Found ${details.length} email(s) for "${query}":\n\n${output}`,
        }],
      };
    } catch (err) {
      return { content: [{ type: "text", text: `Error listing emails: ${err.message}` }], isError: true };
    }
  }
);

// ── Start ───────────────────────────────────────────────────────────────────

async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
}

main().catch((err) => {
  console.error("Gmail MCP server failed:", err);
  process.exit(1);
});
