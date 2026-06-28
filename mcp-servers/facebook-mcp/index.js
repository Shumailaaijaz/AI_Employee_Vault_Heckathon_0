/**
 * Facebook/Instagram MCP Server
 *
 * Dedicated read-and-reply server for Facebook Pages and Instagram Business
 * accounts. Complements the social-post MCP (which handles publishing) by
 * covering the full engagement loop: read comments, reply, read messages,
 * send replies, and fetch page-level analytics.
 *
 * Tools exposed to Claude Code:
 *   facebook_health_check        — Verify token validity + page access
 *   facebook_get_comments        — Get recent comments on a Page post
 *   facebook_reply_comment       — Reply to a Facebook comment (HITL)
 *   facebook_delete_comment      — Delete a spam/toxic comment (HITL)
 *   facebook_get_conversations   — List Page Messenger conversations
 *   facebook_send_message        — Send a reply in a Page conversation (HITL)
 *   facebook_get_page_insights   — Page-level reach/engagement analytics
 *   instagram_get_comments       — Get recent comments on an IG media object
 *   instagram_reply_comment      — Reply to an Instagram comment (HITL)
 *
 * Auth requirements (Facebook Graph API v19.0+):
 *   FACEBOOK_PAGE_ACCESS_TOKEN — long-lived page token with these permissions:
 *     pages_read_engagement          (read comments, likes)
 *     pages_read_user_content        (read visitor posts)
 *     pages_messaging                (read + send page messages)
 *     pages_manage_engagement        (reply to / delete comments)
 *   INSTAGRAM_USER_ID          — numeric IG Business account ID (linked to Page)
 *     instagram_basic                (required for IG endpoints)
 *     instagram_manage_comments      (reply to comments)
 *
 * HITL (Human-in-the-Loop) behaviour:
 *   When DRY_RUN=true (the default), all write operations (reply, send, delete)
 *   are queued as APPROVAL_*.md files in /Pending_Approval/ instead of being
 *   executed. Move the file to /Approved/ to let the Orchestrator execute it.
 *
 * Rate-limit guards (conservative defaults, well inside Graph API limits):
 *   FB_MAX_REPLIES_PER_HOUR  — max comment replies per hour (default: 20)
 *   FB_MAX_MESSAGES_PER_HOUR — max page messages per hour (default: 10)
 *
 * Environment variables:
 *   VAULT_PATH                   — path to Obsidian vault
 *   FACEBOOK_PAGE_ACCESS_TOKEN   — long-lived page token
 *   FACEBOOK_PAGE_ID             — numeric page ID
 *   INSTAGRAM_USER_ID            — numeric IG business user ID
 *   FACEBOOK_GRAPH_VERSION       — Graph API version (default: v19.0)
 *   FB_MAX_REPLIES_PER_HOUR      — rate guard for comment replies (default: 20)
 *   FB_MAX_MESSAGES_PER_HOUR     — rate guard for page messages (default: 10)
 *   DRY_RUN                      — true/false (default: true)
 */

import "dotenv/config";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import fs from "node:fs";
import path from "node:path";

// ── Config ────────────────────────────────────────────────────────────────────

const VAULT_PATH  = process.env.VAULT_PATH  || "/mnt/d/AI_Employee_Vault/AI_Employee_Vault";
const DRY_RUN     = (process.env.DRY_RUN    || "true").toLowerCase() === "true";
const GRAPH_VER   = process.env.FACEBOOK_GRAPH_VERSION || "v19.0";
const GRAPH_BASE  = `https://graph.facebook.com/${GRAPH_VER}`;

const FB_TOKEN    = process.env.FACEBOOK_PAGE_ACCESS_TOKEN || "";
const FB_PAGE_ID  = process.env.FACEBOOK_PAGE_ID           || "";
const IG_USER_ID  = process.env.INSTAGRAM_USER_ID          || "";

const MAX_REPLIES_PER_HOUR  = parseInt(process.env.FB_MAX_REPLIES_PER_HOUR  || "20", 10);
const MAX_MESSAGES_PER_HOUR = parseInt(process.env.FB_MAX_MESSAGES_PER_HOUR || "10", 10);

// Vault directories used for HITL
const PENDING_APPROVAL = path.join(VAULT_PATH, "Pending_Approval");
const LOGS_DIR         = path.join(VAULT_PATH, "Logs");

// ── Rate-limit tracking (sliding 1-hour window) ───────────────────────────────
// Each entry is a Unix timestamp (ms). Entries older than 1 hour are evicted
// before every write so the window always reflects the last 60 minutes.

const replyTimestamps   = [];  // tracks facebook_reply_comment calls
const messageTimestamps = [];  // tracks facebook_send_message calls

function evictOld(arr) {
  const cutoff = Date.now() - 3_600_000; // 1 hour in ms
  while (arr.length > 0 && arr[0] < cutoff) arr.shift();
}

function checkRateLimit(arr, max, label) {
  evictOld(arr);
  if (arr.length >= max) {
    throw new Error(
      `Rate limit reached: ${label} — ${arr.length}/${max} in the last hour. ` +
      `Next slot opens at ${new Date(arr[0] + 3_600_000).toISOString()}.`
    );
  }
}

function recordCall(arr) {
  arr.push(Date.now());
}

// ── Helpers ───────────────────────────────────────────────────────────────────

/** Ensure a directory exists (sync, no-throw). */
function ensureDir(dir) {
  try { fs.mkdirSync(dir, { recursive: true }); } catch {}
}

/** UTC timestamp string for filenames: 20260225_143022 */
function stampNow() {
  return new Date().toISOString().replace(/[-:T]/g, "").slice(0, 15).replace(".", "_");
}

/** ISO timestamp string for log entries. */
function isoNow() { return new Date().toISOString(); }

/**
 * Write a pending-approval markdown file for HITL review.
 * Returns the written filename.
 */
function writePendingApproval(tool, params, description) {
  ensureDir(PENDING_APPROVAL);
  const stamp    = stampNow();
  const filename = `APPROVAL_FB_${tool.toUpperCase()}_${stamp}.md`;
  const filepath = path.join(PENDING_APPROVAL, filename);

  const paramsBlock = JSON.stringify(params, null, 2);
  const content = `---
type: approval_request
tool: ${tool}
created: ${isoNow()}
status: pending
---

# Approval Required: ${tool}

${description}

## Parameters
\`\`\`json
${paramsBlock}
\`\`\`

## How to proceed
- Move this file to \`/Approved/\` to execute the action
- Move to \`/Rejected/\` to abandon it

---
*Auto-generated by facebook-mcp — DRY_RUN mode*
`;

  fs.writeFileSync(filepath, content, "utf8");
  return filename;
}

/**
 * Append a structured JSON entry to today's audit log.
 * Matches the schema used by orchestrator.py and base_watcher.py.
 */
function auditLog(actionType, details) {
  ensureDir(LOGS_DIR);
  const today   = new Date().toISOString().slice(0, 10);
  const logFile = path.join(LOGS_DIR, `${today}.json`);

  let entries = [];
  try {
    if (fs.existsSync(logFile)) {
      entries = JSON.parse(fs.readFileSync(logFile, "utf8"));
    }
  } catch {}

  entries.push({
    timestamp:   isoNow(),
    actor:       "facebook-mcp",
    action_type: actionType,
    details,
  });

  try {
    fs.writeFileSync(logFile, JSON.stringify(entries, null, 2), "utf8");
  } catch {}
}

/**
 * Minimal Graph API GET wrapper.
 *
 * Sends a GET request to the Facebook Graph API and returns the parsed JSON.
 * Automatically appends access_token to the query string.
 * Throws a descriptive error on non-200 responses (includes the error.message
 * from the Graph API response body if available).
 */
async function graphGet(endpoint, params = {}) {
  const url = new URL(`${GRAPH_BASE}/${endpoint}`);
  url.searchParams.set("access_token", FB_TOKEN);
  for (const [k, v] of Object.entries(params)) {
    url.searchParams.set(k, String(v));
  }

  const resp = await fetch(url.toString());
  const body = await resp.json();

  if (!resp.ok || body.error) {
    const msg = body.error?.message || resp.statusText;
    const code = body.error?.code   || resp.status;
    throw new Error(`Graph API error ${code}: ${msg}`);
  }
  return body;
}

/**
 * Minimal Graph API POST wrapper.
 *
 * Sends a POST with a JSON body. access_token is included in the body
 * (Graph API accepts it either in query or body).
 * Throws a descriptive error on failure.
 */
async function graphPost(endpoint, payload = {}) {
  const url  = `${GRAPH_BASE}/${endpoint}`;
  const body = { ...payload, access_token: FB_TOKEN };

  const resp = await fetch(url, {
    method:  "POST",
    headers: { "Content-Type": "application/json" },
    body:    JSON.stringify(body),
  });
  const data = await resp.json();

  if (!resp.ok || data.error) {
    const msg  = data.error?.message || resp.statusText;
    const code = data.error?.code    || resp.status;
    throw new Error(`Graph API POST error ${code}: ${msg}`);
  }
  return data;
}

/** Graph API DELETE wrapper. */
async function graphDelete(endpoint) {
  const url = new URL(`${GRAPH_BASE}/${endpoint}`);
  url.searchParams.set("access_token", FB_TOKEN);

  const resp = await fetch(url.toString(), { method: "DELETE" });
  const data = await resp.json();

  if (!resp.ok || data.error) {
    const msg  = data.error?.message || resp.statusText;
    const code = data.error?.code    || resp.status;
    throw new Error(`Graph API DELETE error ${code}: ${msg}`);
  }
  return data;
}

// ── MCP Server setup ──────────────────────────────────────────────────────────

const server = new McpServer({
  name:    "facebook-mcp",
  version: "1.0.0",
});

// ── Tool: facebook_health_check ───────────────────────────────────────────────

server.tool(
  "facebook_health_check",
  "Verify that the Facebook Page Access Token is valid and retrieve the page name and ID. " +
  "Run this before any other tool to confirm credentials are working.",
  {},
  async () => {
    if (!FB_TOKEN) {
      return { content: [{ type: "text", text: "ERROR: FACEBOOK_PAGE_ACCESS_TOKEN is not set." }] };
    }

    try {
      // /me?fields=id,name,fan_count returns basic page info
      const page = await graphGet("me", { fields: "id,name,fan_count,verification_status" });
      const info = [
        `✅ Token valid`,
        `Page:   ${page.name} (ID: ${page.id})`,
        `Fans:   ${page.fan_count?.toLocaleString() ?? "n/a"}`,
        `Status: ${page.verification_status ?? "n/a"}`,
        `Graph:  ${GRAPH_VER}`,
        `DRY_RUN: ${DRY_RUN}`,
        `Rate guards: ${MAX_REPLIES_PER_HOUR} replies/hr · ${MAX_MESSAGES_PER_HOUR} messages/hr`,
      ].join("\n");

      auditLog("health_check", { page_id: page.id, page_name: page.name });
      return { content: [{ type: "text", text: info }] };
    } catch (err) {
      auditLog("health_check_failed", { error: err.message });
      return { content: [{ type: "text", text: `❌ Health check failed: ${err.message}` }] };
    }
  }
);

// ── Tool: facebook_get_comments ───────────────────────────────────────────────

server.tool(
  "facebook_get_comments",
  "Get recent comments on a Facebook Page post. " +
  "Returns author names, comment text, timestamps, and comment IDs for replying.",
  {
    post_id: z.string().describe(
      "Facebook post ID (format: pageId_postId or just the post's numeric ID)."
    ),
    limit: z.number().int().min(1).max(100).default(25).describe(
      "Number of comments to fetch (default 25, max 100)."
    ),
    filter: z.enum(["toplevel", "stream"]).default("toplevel").describe(
      "'toplevel' = top-level comments only (default). 'stream' = all including replies."
    ),
  },
  async ({ post_id, limit, filter }) => {
    if (!FB_TOKEN) return { content: [{ type: "text", text: "ERROR: FACEBOOK_PAGE_ACCESS_TOKEN not set." }] };

    try {
      const data = await graphGet(`${post_id}/comments`, {
        fields: "id,message,from,created_time,like_count,comment_count",
        limit,
        filter,
        order: "reverse_chronological",
      });

      const comments = (data.data || []).map((c, i) => {
        const author = c.from?.name || "Unknown";
        const time   = c.created_time ? new Date(c.created_time).toLocaleString() : "";
        return `${i + 1}. [${c.id}] ${author} (${time})\n   ${c.message}\n   👍 ${c.like_count ?? 0}  💬 ${c.comment_count ?? 0}`;
      });

      const total = data.data?.length ?? 0;
      const summary = total === 0
        ? "No comments found on this post."
        : `${total} comment(s) on post ${post_id}:\n\n${comments.join("\n\n")}`;

      auditLog("get_comments", { post_id, count: total });
      return { content: [{ type: "text", text: summary }] };
    } catch (err) {
      auditLog("get_comments_error", { post_id, error: err.message });
      return { content: [{ type: "text", text: `Error fetching comments: ${err.message}` }] };
    }
  }
);

// ── Tool: facebook_reply_comment ──────────────────────────────────────────────

server.tool(
  "facebook_reply_comment",
  "Reply to a Facebook comment on a Page post. " +
  "In DRY_RUN mode writes an approval file to /Pending_Approval/ instead of posting.",
  {
    comment_id: z.string().describe("ID of the comment to reply to."),
    message:    z.string().min(1).max(8000).describe("Reply text (max 8000 chars)."),
  },
  async ({ comment_id, message }) => {
    if (!FB_TOKEN) return { content: [{ type: "text", text: "ERROR: FACEBOOK_PAGE_ACCESS_TOKEN not set." }] };

    checkRateLimit(replyTimestamps, MAX_REPLIES_PER_HOUR, "facebook_reply_comment");

    if (DRY_RUN) {
      const file = writePendingApproval(
        "facebook_reply_comment",
        { comment_id, message },
        `Reply to Facebook comment ${comment_id}:\n\n> ${message.slice(0, 200)}${message.length > 200 ? "…" : ""}`
      );
      auditLog("reply_comment_queued", { comment_id, file, dry_run: true });
      return { content: [{ type: "text", text: `[DRY_RUN] Reply queued → Pending_Approval/${file}` }] };
    }

    try {
      const result = await graphPost(`${comment_id}/comments`, { message });
      recordCall(replyTimestamps);
      auditLog("reply_comment", { comment_id, reply_id: result.id });
      return { content: [{ type: "text", text: `✅ Replied to comment ${comment_id} — new comment ID: ${result.id}` }] };
    } catch (err) {
      auditLog("reply_comment_error", { comment_id, error: err.message });
      return { content: [{ type: "text", text: `Error replying to comment: ${err.message}` }] };
    }
  }
);

// ── Tool: facebook_delete_comment ─────────────────────────────────────────────

server.tool(
  "facebook_delete_comment",
  "Delete a spam or toxic comment from a Facebook Page post. " +
  "In DRY_RUN mode writes an approval file to /Pending_Approval/ instead.",
  {
    comment_id: z.string().describe("ID of the comment to delete."),
    reason:     z.string().default("spam").describe("Reason for deletion (for the audit log)."),
  },
  async ({ comment_id, reason }) => {
    if (!FB_TOKEN) return { content: [{ type: "text", text: "ERROR: FACEBOOK_PAGE_ACCESS_TOKEN not set." }] };

    if (DRY_RUN) {
      const file = writePendingApproval(
        "facebook_delete_comment",
        { comment_id, reason },
        `Delete Facebook comment ${comment_id}. Reason: ${reason}`
      );
      auditLog("delete_comment_queued", { comment_id, reason, file, dry_run: true });
      return { content: [{ type: "text", text: `[DRY_RUN] Deletion queued → Pending_Approval/${file}` }] };
    }

    try {
      await graphDelete(comment_id);
      auditLog("delete_comment", { comment_id, reason });
      return { content: [{ type: "text", text: `✅ Comment ${comment_id} deleted (reason: ${reason})` }] };
    } catch (err) {
      auditLog("delete_comment_error", { comment_id, error: err.message });
      return { content: [{ type: "text", text: `Error deleting comment: ${err.message}` }] };
    }
  }
);

// ── Tool: facebook_get_conversations ─────────────────────────────────────────

server.tool(
  "facebook_get_conversations",
  "List recent Page Messenger conversations (threads). " +
  "Returns sender name, last message snippet, timestamp, and conversation ID for replying.",
  {
    limit: z.number().int().min(1).max(50).default(10).describe(
      "Number of conversations to fetch (default 10, max 50)."
    ),
  },
  async ({ limit }) => {
    if (!FB_TOKEN) return { content: [{ type: "text", text: "ERROR: FACEBOOK_PAGE_ACCESS_TOKEN not set." }] };
    if (!FB_PAGE_ID) return { content: [{ type: "text", text: "ERROR: FACEBOOK_PAGE_ID not set." }] };

    try {
      const data = await graphGet(`${FB_PAGE_ID}/conversations`, {
        fields: "id,participants,updated_time,message_count,unread_count",
        limit,
      });

      const convs = (data.data || []).map((c, i) => {
        const participants = (c.participants?.data || [])
          .filter(p => p.id !== FB_PAGE_ID)
          .map(p => p.name || p.id)
          .join(", ") || "Unknown";
        const time    = c.updated_time ? new Date(c.updated_time).toLocaleString() : "";
        const unread  = c.unread_count > 0 ? ` 🔴 ${c.unread_count} unread` : "";
        return `${i + 1}. [${c.id}] ${participants} (${time})${unread}  — ${c.message_count} messages`;
      });

      const total   = data.data?.length ?? 0;
      const summary = total === 0
        ? "No conversations found."
        : `${total} conversation(s):\n\n${convs.join("\n")}`;

      auditLog("get_conversations", { count: total });
      return { content: [{ type: "text", text: summary }] };
    } catch (err) {
      auditLog("get_conversations_error", { error: err.message });
      return { content: [{ type: "text", text: `Error fetching conversations: ${err.message}` }] };
    }
  }
);

// ── Tool: facebook_send_message ───────────────────────────────────────────────

server.tool(
  "facebook_send_message",
  "Send a reply message in a Page Messenger conversation. " +
  "In DRY_RUN mode writes an approval file to /Pending_Approval/ instead of sending.",
  {
    recipient_id: z.string().describe(
      "PSID (Page-Scoped User ID) of the message recipient."
    ),
    message: z.string().min(1).max(2000).describe(
      "Message text to send (max 2000 chars per the Messenger Platform limit)."
    ),
  },
  async ({ recipient_id, message }) => {
    if (!FB_TOKEN) return { content: [{ type: "text", text: "ERROR: FACEBOOK_PAGE_ACCESS_TOKEN not set." }] };

    checkRateLimit(messageTimestamps, MAX_MESSAGES_PER_HOUR, "facebook_send_message");

    if (DRY_RUN) {
      const file = writePendingApproval(
        "facebook_send_message",
        { recipient_id, message },
        `Send Page message to PSID ${recipient_id}:\n\n> ${message.slice(0, 200)}${message.length > 200 ? "…" : ""}`
      );
      auditLog("send_message_queued", { recipient_id, file, dry_run: true });
      return { content: [{ type: "text", text: `[DRY_RUN] Message queued → Pending_Approval/${file}` }] };
    }

    try {
      // Messenger Platform send API endpoint
      const result = await graphPost(`${FB_PAGE_ID}/messages`, {
        recipient: { id: recipient_id },
        message:   { text: message },
        messaging_type: "RESPONSE",
      });
      recordCall(messageTimestamps);
      auditLog("send_message", { recipient_id, message_id: result.message_id });
      return { content: [{ type: "text", text: `✅ Message sent — ID: ${result.message_id}` }] };
    } catch (err) {
      auditLog("send_message_error", { recipient_id, error: err.message });
      return { content: [{ type: "text", text: `Error sending message: ${err.message}` }] };
    }
  }
);

// ── Tool: facebook_get_page_insights ──────────────────────────────────────────

server.tool(
  "facebook_get_page_insights",
  "Get page-level analytics metrics from the Facebook Insights API. " +
  "Returns reach, impressions, fan growth, and engagement for the requested period.",
  {
    metric: z.string().default("page_impressions,page_reach,page_engaged_users,page_fan_adds").describe(
      "Comma-separated metric names. Defaults to common reach/engagement metrics."
    ),
    period: z.enum(["day", "week", "days_28", "month"]).default("week").describe(
      "Aggregation period: 'day', 'week', 'days_28', or 'month' (default: week)."
    ),
  },
  async ({ metric, period }) => {
    if (!FB_TOKEN) return { content: [{ type: "text", text: "ERROR: FACEBOOK_PAGE_ACCESS_TOKEN not set." }] };
    if (!FB_PAGE_ID) return { content: [{ type: "text", text: "ERROR: FACEBOOK_PAGE_ID not set." }] };

    try {
      const data = await graphGet(`${FB_PAGE_ID}/insights`, {
        metric,
        period,
        fields: "name,values,title",
      });

      const metrics = (data.data || []).map(m => {
        const latest = m.values?.at(-1);
        const val    = typeof latest?.value === "object"
          ? JSON.stringify(latest.value)
          : (latest?.value ?? "n/a");
        return `${m.title || m.name}: ${val}`;
      });

      const summary = metrics.length === 0
        ? "No insights data returned."
        : `Page Insights (${period}):\n\n${metrics.join("\n")}`;

      auditLog("get_page_insights", { metric, period, metrics_count: metrics.length });
      return { content: [{ type: "text", text: summary }] };
    } catch (err) {
      auditLog("get_page_insights_error", { error: err.message });
      return { content: [{ type: "text", text: `Error fetching insights: ${err.message}` }] };
    }
  }
);

// ── Tool: instagram_get_comments ──────────────────────────────────────────────

server.tool(
  "instagram_get_comments",
  "Get recent comments on an Instagram media object (post or reel). " +
  "Returns username, comment text, timestamp, and comment ID for replying.",
  {
    media_id: z.string().describe(
      "Instagram media ID (numeric string). Found in watcher action files or via the IG API."
    ),
    limit: z.number().int().min(1).max(100).default(25).describe(
      "Number of comments to fetch (default 25, max 100)."
    ),
  },
  async ({ media_id, limit }) => {
    if (!FB_TOKEN) return { content: [{ type: "text", text: "ERROR: FACEBOOK_PAGE_ACCESS_TOKEN not set." }] };
    if (!IG_USER_ID) return { content: [{ type: "text", text: "ERROR: INSTAGRAM_USER_ID not set." }] };

    try {
      const data = await graphGet(`${media_id}/comments`, {
        fields: "id,text,username,timestamp,like_count,replies{id,text,username}",
        limit,
      });

      const comments = (data.data || []).map((c, i) => {
        const time    = c.timestamp ? new Date(c.timestamp).toLocaleString() : "";
        const replies = c.replies?.data?.length ? ` (${c.replies.data.length} replies)` : "";
        return `${i + 1}. [${c.id}] @${c.username || "unknown"} (${time})${replies}\n   ${c.text}\n   👍 ${c.like_count ?? 0}`;
      });

      const total   = data.data?.length ?? 0;
      const summary = total === 0
        ? `No comments on media ${media_id}.`
        : `${total} comment(s) on IG media ${media_id}:\n\n${comments.join("\n\n")}`;

      auditLog("ig_get_comments", { media_id, count: total });
      return { content: [{ type: "text", text: summary }] };
    } catch (err) {
      auditLog("ig_get_comments_error", { media_id, error: err.message });
      return { content: [{ type: "text", text: `Error fetching IG comments: ${err.message}` }] };
    }
  }
);

// ── Tool: instagram_reply_comment ─────────────────────────────────────────────

server.tool(
  "instagram_reply_comment",
  "Reply to an Instagram comment on a Business account media. " +
  "In DRY_RUN mode writes an approval file to /Pending_Approval/ instead of posting.",
  {
    comment_id: z.string().describe("ID of the Instagram comment to reply to."),
    message:    z.string().min(1).max(2200).describe(
      "Reply text (max 2200 chars — Instagram's caption limit)."
    ),
  },
  async ({ comment_id, message }) => {
    if (!FB_TOKEN)    return { content: [{ type: "text", text: "ERROR: FACEBOOK_PAGE_ACCESS_TOKEN not set." }] };
    if (!IG_USER_ID)  return { content: [{ type: "text", text: "ERROR: INSTAGRAM_USER_ID not set." }] };

    checkRateLimit(replyTimestamps, MAX_REPLIES_PER_HOUR, "instagram_reply_comment");

    if (DRY_RUN) {
      const file = writePendingApproval(
        "instagram_reply_comment",
        { comment_id, message },
        `Reply to Instagram comment ${comment_id}:\n\n> ${message.slice(0, 200)}${message.length > 200 ? "…" : ""}`
      );
      auditLog("ig_reply_comment_queued", { comment_id, file, dry_run: true });
      return { content: [{ type: "text", text: `[DRY_RUN] IG reply queued → Pending_Approval/${file}` }] };
    }

    try {
      // Instagram Graph API reply endpoint
      const result = await graphPost(`${IG_USER_ID}/replies`, {
        commented_media_id: comment_id,
        message,
      });
      recordCall(replyTimestamps);
      auditLog("ig_reply_comment", { comment_id, reply_id: result.id });
      return { content: [{ type: "text", text: `✅ Replied to IG comment ${comment_id} — reply ID: ${result.id}` }] };
    } catch (err) {
      auditLog("ig_reply_comment_error", { comment_id, error: err.message });
      return { content: [{ type: "text", text: `Error replying to IG comment: ${err.message}` }] };
    }
  }
);

// ── Start server ──────────────────────────────────────────────────────────────

const transport = new StdioServerTransport();
await server.connect(transport);
// Note: stdio transport keeps the process alive; no explicit listen() call needed.
