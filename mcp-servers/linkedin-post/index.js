/**
 * LinkedIn Post MCP Server
 *
 * Exposes tools for Claude Code to draft and publish LinkedIn posts
 * for business/sales updates. Uses LinkedIn REST API v2.
 *
 * Tools:
 *   linkedin_get_profile  — Fetch the authenticated user's LinkedIn profile
 *   linkedin_draft_post   — Draft a post and save to /Pending_Approval
 *   linkedin_publish_post — Publish an approved post to LinkedIn
 *   linkedin_delete_post  — Delete a published post
 *
 * All publishing goes through the vault's HITL approval flow when DRY_RUN=true.
 */

import "dotenv/config";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import fs from "node:fs";
import path from "node:path";

// ── Config ──────────────────────────────────────────────────────────────────

const VAULT_PATH = process.env.VAULT_PATH || "/mnt/d/AI_Employee_Vault/AI_Employee_Vault";
const ACCESS_TOKEN = process.env.LINKEDIN_ACCESS_TOKEN || "";
const DRY_RUN = (process.env.DRY_RUN || "true").toLowerCase() === "true";
const API_BASE = "https://api.linkedin.com";

// ── LinkedIn API helpers ────────────────────────────────────────────────────

/**
 * Make an authenticated request to the LinkedIn API.
 */
async function linkedinFetch(endpoint, options = {}) {
  const url = endpoint.startsWith("http") ? endpoint : `${API_BASE}${endpoint}`;
  const res = await fetch(url, {
    ...options,
    headers: {
      Authorization: `Bearer ${ACCESS_TOKEN}`,
      "Content-Type": "application/json",
      "LinkedIn-Version": "202402",
      "X-Restli-Protocol-Version": "2.0.0",
      ...options.headers,
    },
  });

  // LinkedIn returns 201 for successful post creation (no body)
  if (res.status === 201) {
    return { success: true, id: res.headers.get("x-restli-id") || "created" };
  }

  const text = await res.text();
  if (!res.ok) {
    throw new Error(`LinkedIn API ${res.status}: ${text}`);
  }

  return text ? JSON.parse(text) : { success: true };
}

/**
 * Get the authenticated user's person URN (required for posting).
 */
async function getPersonUrn() {
  const profile = await linkedinFetch("/v2/userinfo");
  return `urn:li:person:${profile.sub}`;
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
 * Append an entry to today's log file.
 */
function logAction(actionType, details) {
  const dir = path.join(VAULT_PATH, "Logs");
  fs.mkdirSync(dir, { recursive: true });
  const today = new Date().toISOString().slice(0, 10);
  const logFile = path.join(dir, `${today}.json`);

  let entries = [];
  if (fs.existsSync(logFile)) {
    try {
      entries = JSON.parse(fs.readFileSync(logFile, "utf-8"));
    } catch {
      entries = [];
    }
  }

  entries.push({
    timestamp: new Date().toISOString(),
    watcher: "LinkedInMCP",
    action_type: actionType,
    details,
  });

  fs.writeFileSync(logFile, JSON.stringify(entries, null, 2), "utf-8");
}

// ── MCP Server ──────────────────────────────────────────────────────────────

const server = new McpServer({
  name: "linkedin-post",
  version: "1.0.0",
});

// ── Tool: linkedin_get_profile ──────────────────────────────────────────────

server.tool(
  "linkedin_get_profile",
  "Fetch the authenticated LinkedIn user profile (name, ID, picture). Use this to verify credentials work.",
  {},
  async () => {
    try {
      const profile = await linkedinFetch("/v2/userinfo");
      return {
        content: [
          {
            type: "text",
            text: JSON.stringify(
              {
                name: profile.name,
                email: profile.email,
                person_urn: `urn:li:person:${profile.sub}`,
                picture: profile.picture,
              },
              null,
              2
            ),
          },
        ],
      };
    } catch (err) {
      return { content: [{ type: "text", text: `Error: ${err.message}` }], isError: true };
    }
  }
);

// ── Tool: linkedin_draft_post ───────────────────────────────────────────────

server.tool(
  "linkedin_draft_post",
  `Draft a LinkedIn business post and save it to the vault's /Pending_Approval folder for human review.
The post is NOT published until approved and linkedin_publish_post is called.
Use this for sales updates, product launches, thought leadership, etc.`,
  {
    text: z.string().min(1).max(3000).describe("The post body text (supports mentions with @[Name](urn:li:person:ID))"),
    visibility: z
      .enum(["PUBLIC", "CONNECTIONS"])
      .default("PUBLIC")
      .describe("Post visibility — PUBLIC (anyone) or CONNECTIONS (1st degree only)"),
    commentary: z
      .string()
      .optional()
      .describe("Optional internal note explaining why this post is being made (not published)"),
  },
  async ({ text, visibility, commentary }) => {
    try {
      const now = new Date();
      const ts = now.toISOString().slice(0, 19).replace(/[T:]/g, "-");
      const safeSummary = text.slice(0, 40).replace(/[^a-zA-Z0-9 ]/g, "").trim().replace(/ /g, "_");
      const filename = `LINKEDIN_POST_${safeSummary}_${ts}.md`;

      const content = `---
type: linkedin_post
visibility: ${visibility}
status: pending_approval
created: ${now.toISOString()}
dry_run: ${DRY_RUN}
---

# LinkedIn Post Draft

## Post Content
${text}

## Metadata
- **Visibility**: ${visibility}
- **Created**: ${now.toISOString()}
${commentary ? `- **Internal Note**: ${commentary}` : ""}

## To Approve
Move this file to /Approved folder, then ask Claude to publish it.

## To Reject
Move this file to /Rejected folder.
`;

      const filepath = writeVaultFile("Pending_Approval", filename, content);
      logAction("linkedin_post_drafted", { filename, visibility, text_length: text.length });

      return {
        content: [
          {
            type: "text",
            text: `Draft saved to: ${filepath}\n\nPost preview (${text.length} chars, ${visibility}):\n---\n${text.slice(0, 200)}${text.length > 200 ? "..." : ""}\n---\n\nMove the file to /Approved and call linkedin_publish_post to publish.`,
          },
        ],
      };
    } catch (err) {
      return { content: [{ type: "text", text: `Error: ${err.message}` }], isError: true };
    }
  }
);

// ── Tool: linkedin_publish_post ─────────────────────────────────────────────

server.tool(
  "linkedin_publish_post",
  `Publish a text post to LinkedIn. Use this ONLY after a draft has been approved (moved to /Approved).
In DRY_RUN mode this will log the action but NOT actually post to LinkedIn.`,
  {
    text: z.string().min(1).max(3000).describe("The post body text to publish"),
    visibility: z
      .enum(["PUBLIC", "CONNECTIONS"])
      .default("PUBLIC")
      .describe("Post visibility"),
  },
  async ({ text, visibility }) => {
    // Check approval — look for a matching file in /Approved
    const approvedDir = path.join(VAULT_PATH, "Approved");
    let approvedFile = null;
    if (fs.existsSync(approvedDir)) {
      const files = fs.readdirSync(approvedDir).filter((f) => f.startsWith("LINKEDIN_POST_"));
      for (const f of files) {
        const content = fs.readFileSync(path.join(approvedDir, f), "utf-8");
        // Match if the post text appears in the approved file
        if (content.includes(text.slice(0, 80))) {
          approvedFile = f;
          break;
        }
      }
    }

    if (!approvedFile) {
      return {
        content: [
          {
            type: "text",
            text: "BLOCKED: No matching approved file found in /Approved. Draft the post first with linkedin_draft_post, then move it from /Pending_Approval to /Approved before publishing.",
          },
        ],
        isError: true,
      };
    }

    if (DRY_RUN) {
      logAction("linkedin_post_dry_run", { text_length: text.length, visibility, approved_file: approvedFile });

      // Move to Done
      const doneDir = path.join(VAULT_PATH, "Done");
      fs.mkdirSync(doneDir, { recursive: true });
      fs.renameSync(path.join(approvedDir, approvedFile), path.join(doneDir, approvedFile));

      return {
        content: [
          {
            type: "text",
            text: `[DRY RUN] Would publish to LinkedIn:\n---\n${text.slice(0, 200)}...\n---\nVisibility: ${visibility}\nApproval file moved to /Done.\n\nSet DRY_RUN=false in .env to publish for real.`,
          },
        ],
      };
    }

    // Real publish
    try {
      const personUrn = await getPersonUrn();

      const postBody = {
        author: personUrn,
        commentary: text,
        visibility: visibility === "PUBLIC" ? "PUBLIC" : "CONNECTIONS",
        distribution: {
          feedDistribution: "MAIN_FEED",
          targetEntities: [],
          thirdPartyDistributionChannels: [],
        },
        lifecycleState: "PUBLISHED",
        isReshareDisabledByAuthor: false,
      };

      const result = await linkedinFetch("/rest/posts", {
        method: "POST",
        body: JSON.stringify(postBody),
      });

      // Move approved file to Done
      const doneDir = path.join(VAULT_PATH, "Done");
      fs.mkdirSync(doneDir, { recursive: true });
      fs.renameSync(path.join(approvedDir, approvedFile), path.join(doneDir, approvedFile));

      logAction("linkedin_post_published", {
        post_id: result.id || "unknown",
        text_length: text.length,
        visibility,
        approved_file: approvedFile,
      });

      return {
        content: [
          {
            type: "text",
            text: `Published to LinkedIn!\nPost ID: ${result.id || "created"}\nVisibility: ${visibility}\nApproval file moved to /Done.`,
          },
        ],
      };
    } catch (err) {
      return { content: [{ type: "text", text: `Publish failed: ${err.message}` }], isError: true };
    }
  }
);

// ── Tool: linkedin_delete_post ──────────────────────────────────────────────

server.tool(
  "linkedin_delete_post",
  "Delete a previously published LinkedIn post by its post URN/ID.",
  {
    post_id: z.string().describe("The LinkedIn post URN (e.g., urn:li:share:12345 or the ID returned from publish)"),
  },
  async ({ post_id }) => {
    if (DRY_RUN) {
      return {
        content: [{ type: "text", text: `[DRY RUN] Would delete post: ${post_id}` }],
      };
    }

    try {
      // URL-encode the URN
      const encoded = encodeURIComponent(post_id);
      await linkedinFetch(`/rest/posts/${encoded}`, { method: "DELETE" });

      logAction("linkedin_post_deleted", { post_id });

      return {
        content: [{ type: "text", text: `Deleted LinkedIn post: ${post_id}` }],
      };
    } catch (err) {
      return { content: [{ type: "text", text: `Delete failed: ${err.message}` }], isError: true };
    }
  }
);

// ── Start server ────────────────────────────────────────────────────────────

async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
}

main().catch((err) => {
  console.error("MCP server failed to start:", err);
  process.exit(1);
});
