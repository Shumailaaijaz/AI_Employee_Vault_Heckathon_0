/**
 * YouTube MCP Server
 *
 * Exposes tools for Claude Code to interact with YouTube:
 *   - youtube_reply_comment: Reply to a comment (requires approval)
 *   - youtube_list_comments: List recent comments on channel
 *   - youtube_get_video_stats: Get video statistics
 *   - youtube_draft_reply: Draft a reply for HITL approval
 *
 * All replies go through the vault's HITL approval flow.
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
const DRY_RUN = (process.env.DRY_RUN || "true").toLowerCase() === "true";

// ── YouTube API Setup ───────────────────────────────────────────────────────

const oauth2Client = new google.auth.OAuth2(
  process.env.YOUTUBE_CLIENT_ID,
  process.env.YOUTUBE_CLIENT_SECRET,
  "http://localhost:3458/callback"
);

oauth2Client.setCredentials({
  access_token: process.env.YOUTUBE_ACCESS_TOKEN,
  refresh_token: process.env.YOUTUBE_REFRESH_TOKEN,
});

oauth2Client.on("tokens", (tokens) => {
  if (tokens.access_token) {
    writeLog("youtube_token_refreshed", { expires: tokens.expiry_date });
  }
});

const youtube = google.youtube({ version: "v3", auth: oauth2Client });

// ── Helpers ─────────────────────────────────────────────────────────────────

function writeVaultFile(subdir, filename, content) {
  const dir = path.join(VAULT_PATH, subdir);
  fs.mkdirSync(dir, { recursive: true });
  const filepath = path.join(dir, filename);
  fs.writeFileSync(filepath, content, "utf-8");
  return filepath;
}

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
    watcher: "YouTubeMCP",
    action_type: actionType,
    details,
  });

  fs.writeFileSync(logFile, JSON.stringify(entries, null, 2), "utf-8");
}

// ── MCP Server ──────────────────────────────────────────────────────────────

const server = new McpServer({
  name: "youtube",
  version: "1.0.0",
});

// ── Tool: youtube_list_comments ─────────────────────────────────────────────

server.tool(
  "youtube_list_comments",
  "List recent comments on your YouTube channel. Read-only, no approval needed.",
  {
    max_results: z
      .number()
      .int()
      .min(1)
      .max(50)
      .default(10)
      .describe("Maximum number of comments to return (1-50)"),
  },
  async ({ max_results }) => {
    try {
      // First get channel ID
      const channelRes = await youtube.channels.list({
        part: "snippet",
        mine: true,
      });

      if (!channelRes.data.items?.length) {
        return { content: [{ type: "text", text: "No YouTube channel found for this account." }], isError: true };
      }

      const channelId = channelRes.data.items[0].id;

      // Get comments
      const commentsRes = await youtube.commentThreads.list({
        part: "snippet,replies",
        allThreadsRelatedToChannelId: channelId,
        maxResults: max_results,
        order: "time",
      });

      const comments = commentsRes.data.items || [];

      if (comments.length === 0) {
        return { content: [{ type: "text", text: "No comments found." }] };
      }

      const output = comments.map((item, i) => {
        const snippet = item.snippet.topLevelComment.snippet;
        return `${i + 1}. **${snippet.authorDisplayName}** (${item.snippet.totalReplyCount} replies, ${snippet.likeCount} likes)
   Video: https://youtube.com/watch?v=${snippet.videoId}
   Comment: ${snippet.textDisplay.slice(0, 200)}${snippet.textDisplay.length > 200 ? "..." : ""}
   ID: ${item.id}`;
      }).join("\n\n");

      writeLog("youtube_comments_listed", { count: comments.length });

      return {
        content: [{ type: "text", text: `Found ${comments.length} recent comment(s):\n\n${output}` }],
      };
    } catch (err) {
      return { content: [{ type: "text", text: `Error: ${err.message}` }], isError: true };
    }
  }
);

// ── Tool: youtube_draft_reply ───────────────────────────────────────────────

server.tool(
  "youtube_draft_reply",
  `Draft a reply to a YouTube comment and save to /Pending_Approval.
The reply is NOT posted until approved and youtube_reply_comment is called.
Use this for all comment replies — ensures human review before posting.`,
  {
    comment_id: z.string().describe("The comment thread ID to reply to"),
    video_id: z.string().describe("The video ID where the comment was made"),
    original_comment: z.string().describe("The original comment text (for context)"),
    reply_text: z.string().min(1).max(10000).describe("Your reply text"),
    author_name: z.string().optional().describe("The original commenter's name"),
  },
  async ({ comment_id, video_id, original_comment, reply_text, author_name }) => {
    try {
      const now = new Date();
      const ts = now.toISOString().slice(0, 19).replace(/[T:]/g, "-");
      const safeAuthor = (author_name || "user").replace(/[^a-zA-Z0-9]/g, "_").slice(0, 15);
      const filename = `YOUTUBE_REPLY_${safeAuthor}_${ts}.md`;

      const content = `---
type: youtube_reply
comment_id: "${comment_id}"
video_id: "${video_id}"
author: "${author_name || "Unknown"}"
status: pending_approval
created: ${now.toISOString()}
dry_run: ${DRY_RUN}
---

# YouTube Reply Draft

## Original Comment
**From:** ${author_name || "Unknown"}
**Video:** https://youtube.com/watch?v=${video_id}

> ${original_comment}

## Your Reply
${reply_text}

## Instructions
- **To Approve**: Move this file to \`/Approved/\`
- **To Reject**: Move this file to \`/Rejected/\`

## MCP Arguments
\`\`\`json
{
  "comment_id": "${comment_id}",
  "reply_text": "${reply_text.replace(/"/g, '\\"')}"
}
\`\`\`
`;

      const filepath = writeVaultFile("Pending_Approval", filename, content);
      writeLog("youtube_reply_drafted", { comment_id, video_id, filename });

      return {
        content: [{
          type: "text",
          text: `Draft reply saved to: ${filepath}\n\nReply preview: ${reply_text.slice(0, 100)}...\n\nMove the file to /Approved and call youtube_reply_comment to post.`,
        }],
      };
    } catch (err) {
      return { content: [{ type: "text", text: `Error: ${err.message}` }], isError: true };
    }
  }
);

// ── Tool: youtube_reply_comment ─────────────────────────────────────────────

server.tool(
  "youtube_reply_comment",
  `Post a reply to a YouTube comment. Requires approved file in /Approved.
In DRY_RUN mode this logs the action but does NOT actually post.
ALWAYS use youtube_draft_reply first and wait for human approval.`,
  {
    comment_id: z.string().describe("The comment thread ID to reply to"),
    reply_text: z.string().min(1).max(10000).describe("The reply text to post"),
  },
  async ({ comment_id, reply_text }) => {
    // Check for approval file
    const approvedDir = path.join(VAULT_PATH, "Approved");
    let approvedFile = null;

    if (fs.existsSync(approvedDir)) {
      const files = fs.readdirSync(approvedDir).filter(f => f.startsWith("YOUTUBE_REPLY_"));
      for (const f of files) {
        const content = fs.readFileSync(path.join(approvedDir, f), "utf-8");
        if (content.includes(comment_id)) {
          approvedFile = f;
          break;
        }
      }
    }

    if (!approvedFile) {
      return {
        content: [{
          type: "text",
          text: "BLOCKED: No matching approved file found in /Approved.\nDraft the reply first with youtube_draft_reply, then move to /Approved before posting.",
        }],
        isError: true,
      };
    }

    // DRY RUN
    if (DRY_RUN) {
      writeLog("youtube_reply_dry_run", { comment_id, reply_preview: reply_text.slice(0, 100) });

      const doneDir = path.join(VAULT_PATH, "Done");
      fs.mkdirSync(doneDir, { recursive: true });
      fs.renameSync(path.join(approvedDir, approvedFile), path.join(doneDir, approvedFile));

      return {
        content: [{
          type: "text",
          text: `[DRY RUN] Would post reply to comment ${comment_id}:\n"${reply_text.slice(0, 200)}..."\n\nApproval file moved to /Done.\nSet DRY_RUN=false to post for real.`,
        }],
      };
    }

    // REAL POST
    try {
      const response = await youtube.comments.insert({
        part: "snippet",
        requestBody: {
          snippet: {
            parentId: comment_id,
            textOriginal: reply_text,
          },
        },
      });

      // Move approval file to Done
      const doneDir = path.join(VAULT_PATH, "Done");
      fs.mkdirSync(doneDir, { recursive: true });
      fs.renameSync(path.join(approvedDir, approvedFile), path.join(doneDir, approvedFile));

      writeLog("youtube_reply_posted", {
        comment_id,
        reply_id: response.data.id,
        approved_file: approvedFile,
      });

      return {
        content: [{
          type: "text",
          text: `Reply posted!\nComment ID: ${comment_id}\nReply ID: ${response.data.id}\nApproval file moved to /Done.`,
        }],
      };
    } catch (err) {
      writeLog("youtube_reply_error", { comment_id, error: err.message });
      return { content: [{ type: "text", text: `Post failed: ${err.message}` }], isError: true };
    }
  }
);

// ── Tool: youtube_get_video_stats ───────────────────────────────────────────

server.tool(
  "youtube_get_video_stats",
  "Get statistics for a YouTube video (views, likes, comments). Read-only.",
  {
    video_id: z.string().describe("The YouTube video ID"),
  },
  async ({ video_id }) => {
    try {
      const response = await youtube.videos.list({
        part: "snippet,statistics",
        id: video_id,
      });

      if (!response.data.items?.length) {
        return { content: [{ type: "text", text: `Video not found: ${video_id}` }], isError: true };
      }

      const video = response.data.items[0];
      const stats = video.statistics;
      const snippet = video.snippet;

      const output = `**${snippet.title}**

Channel: ${snippet.channelTitle}
Published: ${snippet.publishedAt}

Stats:
- Views: ${parseInt(stats.viewCount).toLocaleString()}
- Likes: ${parseInt(stats.likeCount).toLocaleString()}
- Comments: ${parseInt(stats.commentCount).toLocaleString()}

URL: https://youtube.com/watch?v=${video_id}`;

      return { content: [{ type: "text", text: output }] };
    } catch (err) {
      return { content: [{ type: "text", text: `Error: ${err.message}` }], isError: true };
    }
  }
);

// ── Start ───────────────────────────────────────────────────────────────────

async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
}

main().catch((err) => {
  console.error("YouTube MCP server failed:", err);
  process.exit(1);
});
