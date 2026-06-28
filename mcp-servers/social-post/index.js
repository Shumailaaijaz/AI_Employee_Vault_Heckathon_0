/**
 * Social Post MCP Server
 *
 * Exposes publishing and analytics tools for Facebook, Instagram, and Twitter/X
 * to Claude Code. All write operations go through the vault's HITL approval flow
 * when DRY_RUN=true (default).
 *
 * Tools:
 *   facebook_post_message    — Post text (+ optional link) to a Facebook Page
 *   instagram_post_message   — Publish a captioned image to Instagram Business
 *   twitter_post_tweet       — Post a tweet (with optional reply threading)
 *   social_generate_summary  — Get engagement metrics for any post across platforms
 *   social_rate_limit_status — Show current rate-limit windows for each platform
 *
 * Auth requirements:
 *   Facebook / Instagram:
 *     Long-lived Page Access Token with:
 *       pages_manage_posts, pages_read_engagement,
 *       instagram_content_publish, instagram_basic
 *   Twitter:
 *     OAuth 1.0a (API key + secret + access token + secret) for writing
 *     Bearer token for read-only analytics
 *
 * Rate-limit guards (conservative — well inside platform limits):
 *   Facebook:  25 posts / hour   (platform limit: ~200 calls/hour)
 *   Instagram: 24 posts / day    (platform limit: 50 API posts/day)
 *   Twitter:   15 tweets / day   (platform limit: 17/day on free tier)
 *
 * Environment variables: see .env.example
 */

import "dotenv/config";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import { TwitterApi } from "twitter-api-v2";
import fs from "node:fs";
import path from "node:path";

// ── Config ───────────────────────────────────────────────────────────────────

const VAULT_PATH   = process.env.VAULT_PATH   || "/mnt/d/AI_Employee_Vault/AI_Employee_Vault";
const DRY_RUN      = (process.env.DRY_RUN || "true").toLowerCase() === "true";
const GRAPH_VER    = process.env.FACEBOOK_GRAPH_VERSION || "v19.0";
const GRAPH_BASE   = `https://graph.facebook.com/${GRAPH_VER}`;

const FB_TOKEN     = process.env.FACEBOOK_PAGE_ACCESS_TOKEN || "";
const FB_PAGE_ID   = process.env.FACEBOOK_PAGE_ID           || "";
const IG_USER_ID   = process.env.INSTAGRAM_USER_ID          || "";

const TW_API_KEY   = process.env.TWITTER_API_KEY              || "";
const TW_API_SEC   = process.env.TWITTER_API_SECRET           || "";
const TW_ACC_TOK   = process.env.TWITTER_ACCESS_TOKEN         || "";
const TW_ACC_SEC   = process.env.TWITTER_ACCESS_TOKEN_SECRET  || "";
const TW_BEARER    = process.env.TWITTER_BEARER_TOKEN         || "";

const FB_MAX_HOUR  = parseInt(process.env.FACEBOOK_MAX_POSTS_PER_HOUR  || "25",  10);
const IG_MAX_DAY   = parseInt(process.env.INSTAGRAM_MAX_POSTS_PER_DAY  || "24",  10);
const TW_MAX_DAY   = parseInt(process.env.TWITTER_MAX_TWEETS_PER_DAY   || "15",  10);

// ── Twitter clients ──────────────────────────────────────────────────────────

/** OAuth 1.0a client — required for posting tweets. */
const twitterWrite = (TW_API_KEY && TW_ACC_TOK)
  ? new TwitterApi({
      appKey:        TW_API_KEY,
      appSecret:     TW_API_SEC,
      accessToken:   TW_ACC_TOK,
      accessSecret:  TW_ACC_SEC,
    })
  : null;

/** App-only Bearer token client — for read operations. */
const twitterRead = TW_BEARER ? new TwitterApi(TW_BEARER) : null;

// ── Rate Limiter ─────────────────────────────────────────────────────────────

/**
 * Sliding-window rate limiter.
 * Tracks timestamps of recent calls and rejects if over limit.
 */
class RateLimiter {
  constructor(maxCalls, windowMs, label) {
    this.maxCalls  = maxCalls;
    this.windowMs  = windowMs;
    this.label     = label;
    this._calls    = [];
  }

  /** Remove expired entries and return current count. */
  _prune() {
    const cutoff = Date.now() - this.windowMs;
    this._calls  = this._calls.filter((t) => t > cutoff);
    return this._calls.length;
  }

  /** Returns true if a call is permitted. Does NOT record it. */
  canCall() {
    return this._prune() < this.maxCalls;
  }

  /** Record a call (call after canCall() returns true). */
  record() {
    this._prune();
    this._calls.push(Date.now());
  }

  /** Human-readable status string. */
  status() {
    const count = this._prune();
    const windowLabel =
      this.windowMs >= 3_600_000
        ? `${this.windowMs / 3_600_000}h`
        : `${this.windowMs / 60_000}min`;
    const resetMs = this._calls.length
      ? this.windowMs - (Date.now() - this._calls[0])
      : 0;
    const resetMin = Math.ceil(resetMs / 60_000);
    return (
      `${this.label}: ${count}/${this.maxCalls} per ${windowLabel}` +
      (count >= this.maxCalls ? ` — resets in ~${resetMin} min` : " — OK")
    );
  }
}

const rateLimiters = {
  facebook:  new RateLimiter(FB_MAX_HOUR, 60 * 60_000,      "Facebook"),
  instagram: new RateLimiter(IG_MAX_DAY,  24 * 60 * 60_000, "Instagram"),
  twitter:   new RateLimiter(TW_MAX_DAY,  24 * 60 * 60_000, "Twitter"),
};

/** Throws if the given platform is over its rate limit. */
function checkRateLimit(platform) {
  const limiter = rateLimiters[platform];
  if (!limiter) return;
  if (!limiter.canCall()) {
    throw new Error(
      `Rate limit reached for ${platform}. ${limiter.status()}`
    );
  }
}

// ── Graph API helpers ─────────────────────────────────────────────────────────

/**
 * Make an authenticated call to the Facebook Graph API.
 * Always injects access_token into query params for GET or body for POST.
 */
async function graphFetch(endpoint, options = {}) {
  if (!FB_TOKEN) {
    throw new Error(
      "FACEBOOK_PAGE_ACCESS_TOKEN is not set. Add it to .env."
    );
  }

  const isGet = !options.method || options.method.toUpperCase() === "GET";
  let url = endpoint.startsWith("http") ? endpoint : `${GRAPH_BASE}${endpoint}`;

  if (isGet) {
    const sep = url.includes("?") ? "&" : "?";
    url += `${sep}access_token=${encodeURIComponent(FB_TOKEN)}`;
  }

  const headers = { "Content-Type": "application/json" };
  let body = options.body;

  if (!isGet) {
    // Inject token into POST body
    const parsed = body ? JSON.parse(body) : {};
    parsed.access_token = FB_TOKEN;
    body = JSON.stringify(parsed);
  }

  const res = await fetch(url, { ...options, headers, body });
  const text = await res.text();

  let json;
  try { json = JSON.parse(text); } catch { json = { raw: text }; }

  // Check for Graph API error
  if (json.error) {
    const code = json.error.code || 0;
    const msg  = json.error.message || JSON.stringify(json.error);

    // Token expired
    if (code === 190) {
      throw new Error(
        `Facebook token expired (code 190): ${msg}. ` +
        "Refresh your long-lived Page Access Token in the Graph API Explorer."
      );
    }
    // Rate limited
    if ([4, 17, 32, 613].includes(code)) {
      throw new Error(`Facebook rate limit hit (code ${code}): ${msg}`);
    }
    throw new Error(`Facebook Graph API error (${code}): ${msg}`);
  }

  if (!res.ok) {
    throw new Error(`HTTP ${res.status} from Graph API: ${text}`);
  }

  return json;
}

// ── Vault helpers ─────────────────────────────────────────────────────────────

function writeVaultFile(subdir, filename, content) {
  const dir = path.join(VAULT_PATH, subdir);
  fs.mkdirSync(dir, { recursive: true });
  const filepath = path.join(dir, filename);
  fs.writeFileSync(filepath, content, "utf-8");
  return filepath;
}

function logAction(actionType, details) {
  const dir   = path.join(VAULT_PATH, "Logs");
  fs.mkdirSync(dir, { recursive: true });
  const today = new Date().toISOString().slice(0, 10);
  const file  = path.join(dir, `${today}.json`);

  let entries = [];
  if (fs.existsSync(file)) {
    try { entries = JSON.parse(fs.readFileSync(file, "utf-8")); } catch { entries = []; }
  }
  entries.push({ timestamp: new Date().toISOString(), watcher: "SocialPostMCP", action_type: actionType, details });
  fs.writeFileSync(file, JSON.stringify(entries, null, 2), "utf-8");
}

/**
 * In DRY_RUN mode: save the post spec to /Pending_Approval and return filepath.
 * Otherwise: check for a matching approval file in /Approved, return filename if found.
 */
function handleDryRun(platform, prefix, filename, markdownContent) {
  if (DRY_RUN) {
    const filepath = writeVaultFile("Pending_Approval", filename, markdownContent);
    logAction(`${platform}_post_queued`, { filename, platform });
    return { isDryRun: true, filepath };
  }

  // Real mode — look for approval
  const approvedDir = path.join(VAULT_PATH, "Approved");
  if (fs.existsSync(approvedDir)) {
    const match = fs.readdirSync(approvedDir).find((f) => f.startsWith(prefix));
    if (match) return { isDryRun: false, approvedFile: match };
  }
  return { isDryRun: false, approvedFile: null };
}

function moveToArchive(subdir, filename) {
  const src  = path.join(VAULT_PATH, subdir, filename);
  const dest = path.join(VAULT_PATH, "Done", filename);
  fs.mkdirSync(path.join(VAULT_PATH, "Done"), { recursive: true });
  if (fs.existsSync(src)) fs.renameSync(src, dest);
}

// ── Timestamp helpers ─────────────────────────────────────────────────────────

function nowTs() {
  return new Date().toISOString().slice(0, 19).replace(/[T:]/g, "-");
}

function safeName(str, maxLen = 40) {
  return str.slice(0, maxLen).replace(/[^a-zA-Z0-9 ]/g, "").trim().replace(/ /g, "_");
}

// ── MCP Server ────────────────────────────────────────────────────────────────

const server = new McpServer({
  name: "social-post",
  version: "1.0.0",
});

// ── Tool: facebook_post_message ───────────────────────────────────────────────

server.tool(
  "facebook_post_message",
  `Post a text message (and optional link) to a Facebook Page.
In DRY_RUN mode the post is saved to /Pending_Approval for human review.
Move the file to /Approved and call this tool again (DRY_RUN=false) to publish.`,
  {
    content: z
      .string()
      .min(1)
      .max(63_206)
      .describe("The post text content (supports Unicode, emojis, newlines)"),
    page_id: z
      .string()
      .optional()
      .describe(`Facebook Page ID (defaults to env FACEBOOK_PAGE_ID: ${FB_PAGE_ID})`),
    link: z
      .string()
      .url()
      .optional()
      .describe("Optional URL to attach as a link preview"),
    scheduled_publish_time: z
      .number()
      .int()
      .optional()
      .describe("Unix timestamp to schedule the post (leave empty for immediate publish)"),
  },
  async ({ content, page_id, link, scheduled_publish_time }) => {
    const pid = page_id || FB_PAGE_ID;
    if (!pid) {
      return {
        content: [{ type: "text", text: "Error: page_id is required (or set FACEBOOK_PAGE_ID in .env)" }],
        isError: true,
      };
    }

    const now      = new Date();
    const ts       = nowTs();
    const preview  = safeName(content, 40);
    const prefix   = `FB_POST_${preview}_`;
    const filename = `${prefix}${ts}.md`;

    const draftMd = `---
type: facebook_post
page_id: "${pid}"
preview: "${content.slice(0, 100).replace(/"/g, "'")}"
link: "${link || ""}"
scheduled: ${scheduled_publish_time || "null"}
status: pending_approval
created: ${now.toISOString()}
dry_run: true
---

# Facebook Post Draft

## Page
${pid}

## Content
${content}

${link ? `## Link\n${link}\n` : ""}
## To Approve
Move this file to \`/Approved\` and ask Claude to publish it using the \`facebook_post_message\` tool.

## To Reject
Move this file to \`/Rejected\`.
`;

    if (DRY_RUN) {
      const filepath = writeVaultFile("Pending_Approval", filename, draftMd);
      logAction("facebook_post_queued", { page_id: pid, content_length: content.length, filepath });
      return {
        content: [
          {
            type: "text",
            text: `[DRY RUN] Facebook post draft saved to:\n${filepath}\n\nPreview (${content.length} chars):\n---\n${content.slice(0, 300)}${content.length > 300 ? "..." : ""}\n---\n\nMove to /Approved and call facebook_post_message to publish.`,
          },
        ],
      };
    }

    // Real publish
    try {
      checkRateLimit("facebook");

      const body = { message: content };
      if (link) body.link = link;
      if (scheduled_publish_time) {
        body.scheduled_publish_time = scheduled_publish_time;
        body.published = false;
      }

      const result = await graphFetch(`/${pid}/feed`, {
        method: "POST",
        body: JSON.stringify(body),
      });

      rateLimiters.facebook.record();

      const postId   = result.id || "unknown";
      const postUrl  = `https://www.facebook.com/${postId.replace("_", "/posts/")}`;

      logAction("facebook_post_published", { page_id: pid, post_id: postId, post_url: postUrl, content_length: content.length });

      writeVaultFile(
        "Done",
        `FB_PUBLISHED_${postId.replace(/_/g, "-")}_${ts}.md`,
        `---\ntype: facebook_post_published\npost_id: "${postId}"\npage_id: "${pid}"\npost_url: "${postUrl}"\npublished: ${now.toISOString()}\n---\n\n# Published: Facebook Post\n\n- **Post ID**: ${postId}\n- **URL**: ${postUrl}\n- **Content**: ${content.slice(0, 200)}\n`
      );

      return {
        content: [
          {
            type: "text",
            text: `Published to Facebook Page ${pid}!\n\n  Post ID:  ${postId}\n  URL:      ${postUrl}\n  Length:   ${content.length} chars\n${scheduled_publish_time ? `  Scheduled: ${new Date(scheduled_publish_time * 1000).toISOString()}` : ""}`,
          },
        ],
      };
    } catch (err) {
      return { content: [{ type: "text", text: `Facebook post failed: ${err.message}` }], isError: true };
    }
  }
);

// ── Tool: instagram_post_message ──────────────────────────────────────────────

server.tool(
  "instagram_post_message",
  `Publish a captioned image (or Reel) to an Instagram Business account.
Instagram requires an image_url for feed posts and a video_url for Reels.
In DRY_RUN mode the spec is saved to /Pending_Approval.
Two-step process: (1) create media container, (2) publish container.`,
  {
    caption: z
      .string()
      .max(2_200)
      .describe("Post caption (max 2,200 chars). Include hashtags here."),
    image_url: z
      .string()
      .url()
      .optional()
      .describe("Publicly accessible HTTPS URL of the image to post (JPEG/PNG, min 500px)"),
    video_url: z
      .string()
      .url()
      .optional()
      .describe("Publicly accessible HTTPS URL of the video for a Reel"),
    ig_user_id: z
      .string()
      .optional()
      .describe(`Instagram Business User ID (defaults to env INSTAGRAM_USER_ID: ${IG_USER_ID})`),
    media_type: z
      .enum(["IMAGE", "REELS"])
      .default("IMAGE")
      .describe("Media type: IMAGE (feed post) or REELS"),
  },
  async ({ caption, image_url, video_url, ig_user_id, media_type }) => {
    const uid = ig_user_id || IG_USER_ID;
    if (!uid) {
      return {
        content: [{ type: "text", text: "Error: ig_user_id is required (or set INSTAGRAM_USER_ID in .env)" }],
        isError: true,
      };
    }
    if (media_type === "IMAGE" && !image_url) {
      return {
        content: [{ type: "text", text: "Error: image_url is required for IMAGE posts." }],
        isError: true,
      };
    }
    if (media_type === "REELS" && !video_url) {
      return {
        content: [{ type: "text", text: "Error: video_url is required for REELS posts." }],
        isError: true,
      };
    }

    const now      = new Date();
    const ts       = nowTs();
    const preview  = safeName(caption, 40);
    const filename = `IG_POST_${preview}_${ts}.md`;

    const draftMd = `---
type: instagram_post
ig_user_id: "${uid}"
media_type: "${media_type}"
image_url: "${image_url || ""}"
video_url: "${video_url || ""}"
caption_preview: "${caption.slice(0, 100).replace(/"/g, "'")}"
status: pending_approval
created: ${now.toISOString()}
dry_run: true
---

# Instagram Post Draft

## Account
${uid}

## Media Type
${media_type}

## Media URL
${image_url || video_url || "(none)"}

## Caption
${caption}

## To Approve
Move this file to \`/Approved\` and ask Claude to publish using \`instagram_post_message\`.

## To Reject
Move this file to \`/Rejected\`.
`;

    if (DRY_RUN) {
      const filepath = writeVaultFile("Pending_Approval", filename, draftMd);
      logAction("instagram_post_queued", { ig_user_id: uid, media_type, filepath });
      return {
        content: [
          {
            type: "text",
            text: `[DRY RUN] Instagram post draft saved to:\n${filepath}\n\nCaption preview:\n---\n${caption.slice(0, 300)}${caption.length > 300 ? "..." : ""}\n---\n\nMove to /Approved and call instagram_post_message to publish.`,
          },
        ],
      };
    }

    // Real publish — two-step: create container → publish
    try {
      checkRateLimit("instagram");

      // Step 1: Create media container
      const containerBody = { caption };
      if (media_type === "REELS") {
        containerBody.media_type = "REELS";
        containerBody.video_url  = video_url;
      } else {
        containerBody.image_url = image_url;
      }

      const container = await graphFetch(`/${uid}/media`, {
        method: "POST",
        body: JSON.stringify(containerBody),
      });

      const creationId = container.id;
      if (!creationId) {
        throw new Error("Instagram media container creation returned no ID.");
      }

      // Step 2: Publish the container
      const publishResult = await graphFetch(`/${uid}/media_publish`, {
        method: "POST",
        body: JSON.stringify({ creation_id: creationId }),
      });

      rateLimiters.instagram.record();

      const mediaId  = publishResult.id || creationId;
      const mediaUrl = `https://www.instagram.com/p/${mediaId}/`;

      logAction("instagram_post_published", {
        ig_user_id: uid, media_id: mediaId, media_type, caption_length: caption.length,
      });

      writeVaultFile(
        "Done",
        `IG_PUBLISHED_${mediaId}_${ts}.md`,
        `---\ntype: instagram_post_published\nmedia_id: "${mediaId}"\nig_user_id: "${uid}"\nmedia_type: "${media_type}"\npublished: ${now.toISOString()}\n---\n\n# Published: Instagram ${media_type}\n\n- **Media ID**: ${mediaId}\n- **URL**: ${mediaUrl}\n- **Caption**: ${caption.slice(0, 200)}\n`
      );

      return {
        content: [
          {
            type: "text",
            text: `Published to Instagram!\n\n  Media ID:   ${mediaId}\n  Type:       ${media_type}\n  URL:        ${mediaUrl}\n  Caption:    ${caption.length} chars`,
          },
        ],
      };
    } catch (err) {
      return { content: [{ type: "text", text: `Instagram post failed: ${err.message}` }], isError: true };
    }
  }
);

// ── Tool: twitter_post_tweet ──────────────────────────────────────────────────

server.tool(
  "twitter_post_tweet",
  `Post a tweet to Twitter/X using OAuth 1.0a.
Supports reply threading (set reply_to_tweet_id for threaded conversations).
In DRY_RUN mode the tweet spec is saved to /Pending_Approval.
Requires TWITTER_API_KEY + TWITTER_ACCESS_TOKEN (+ secrets) in .env.`,
  {
    content: z
      .string()
      .min(1)
      .max(280)
      .describe("Tweet text (max 280 chars). Emojis and Unicode supported."),
    reply_to_tweet_id: z
      .string()
      .optional()
      .describe("Tweet ID to reply to (creates a threaded conversation)"),
    quote_tweet_id: z
      .string()
      .optional()
      .describe("Tweet ID to quote-tweet"),
  },
  async ({ content, reply_to_tweet_id, quote_tweet_id }) => {
    const now      = new Date();
    const ts       = nowTs();
    const preview  = safeName(content, 40);
    const filename = `TW_TWEET_${preview}_${ts}.md`;

    const draftMd = `---
type: twitter_tweet
content: "${content.replace(/"/g, "'")}"
reply_to: "${reply_to_tweet_id || ""}"
quote_tweet: "${quote_tweet_id || ""}"
status: pending_approval
created: ${now.toISOString()}
dry_run: true
---

# Twitter Tweet Draft

## Content (${content.length}/280 chars)
${content}

${reply_to_tweet_id ? `## Reply To\nhttps://twitter.com/i/web/status/${reply_to_tweet_id}\n` : ""}
${quote_tweet_id    ? `## Quote Tweet\nhttps://twitter.com/i/web/status/${quote_tweet_id}\n` : ""}

## To Approve
Move this file to \`/Approved\` and ask Claude to post using \`twitter_post_tweet\`.

## To Reject
Move this file to \`/Rejected\`.
`;

    if (DRY_RUN) {
      const filepath = writeVaultFile("Pending_Approval", filename, draftMd);
      logAction("twitter_tweet_queued", { content_length: content.length, reply_to: reply_to_tweet_id, filepath });
      return {
        content: [
          {
            type: "text",
            text: `[DRY RUN] Tweet draft saved to:\n${filepath}\n\nContent (${content.length}/280):\n---\n${content}\n---\n\nMove to /Approved and call twitter_post_tweet to publish.`,
          },
        ],
      };
    }

    // Real tweet
    if (!twitterWrite) {
      return {
        content: [
          {
            type: "text",
            text: "Twitter credentials not configured. Set TWITTER_API_KEY, TWITTER_API_SECRET, TWITTER_ACCESS_TOKEN, TWITTER_ACCESS_TOKEN_SECRET in .env.",
          },
        ],
        isError: true,
      };
    }

    try {
      checkRateLimit("twitter");

      const tweetPayload = { text: content };
      if (reply_to_tweet_id) {
        tweetPayload.reply = { in_reply_to_tweet_id: reply_to_tweet_id };
      }
      if (quote_tweet_id) {
        tweetPayload.quote_tweet_id = quote_tweet_id;
      }

      const result = await twitterWrite.v2.tweet(tweetPayload);
      const tweetId  = result.data.id;
      const tweetUrl = `https://twitter.com/i/web/status/${tweetId}`;

      rateLimiters.twitter.record();

      logAction("twitter_tweet_posted", { tweet_id: tweetId, tweet_url: tweetUrl, content_length: content.length });

      writeVaultFile(
        "Done",
        `TW_PUBLISHED_${tweetId}_${ts}.md`,
        `---\ntype: twitter_tweet_published\ntweet_id: "${tweetId}"\ntweet_url: "${tweetUrl}"\npublished: ${now.toISOString()}\n---\n\n# Published: Tweet\n\n- **Tweet ID**: ${tweetId}\n- **URL**: ${tweetUrl}\n- **Content**: ${content}\n`
      );

      return {
        content: [
          {
            type: "text",
            text: `Tweet posted!\n\n  Tweet ID: ${tweetId}\n  URL:      ${tweetUrl}\n  Length:   ${content.length}/280 chars`,
          },
        ],
      };
    } catch (err) {
      // twitter-api-v2 wraps errors with .data
      const msg = err.data?.detail || err.data?.title || err.message;
      return { content: [{ type: "text", text: `Twitter post failed: ${msg}` }], isError: true };
    }
  }
);

// ── Tool: social_generate_summary ─────────────────────────────────────────────

server.tool(
  "social_generate_summary",
  `Fetch engagement metrics (likes, comments, shares, reach) for a published post.
Supports Facebook, Instagram, and Twitter posts. Pass the post ID returned by the publish tools.`,
  {
    platform: z
      .enum(["facebook", "instagram", "twitter"])
      .describe("Which platform the post lives on"),
    post_id: z
      .string()
      .min(1)
      .describe(
        "Platform-specific post ID. Facebook: 'pageId_postId', Instagram: numeric media ID, Twitter: numeric tweet ID"
      ),
  },
  async ({ platform, post_id }) => {
    try {
      if (platform === "facebook") {
        const data = await graphFetch(
          `/${post_id}?fields=message,created_time,likes.summary(true),comments.summary(true),shares,reactions.summary(true)`
        );

        const likes     = data.likes?.summary?.total_count     ?? 0;
        const comments  = data.comments?.summary?.total_count  ?? 0;
        const shares    = data.shares?.count                   ?? 0;
        const reactions = data.reactions?.summary?.total_count ?? 0;
        const created   = data.created_time || "unknown";
        const postUrl   = `https://www.facebook.com/${post_id.replace("_", "/posts/")}`;

        logAction("facebook_summary_fetched", { post_id, likes, comments, shares, reactions });

        return {
          content: [
            {
              type: "text",
              text: [
                `Facebook Post Summary — ${post_id}`,
                `URL: ${postUrl}`,
                `Created: ${created}`,
                ``,
                `Engagement:`,
                `  Likes / Reactions: ${likes} / ${reactions}`,
                `  Comments:          ${comments}`,
                `  Shares:            ${shares}`,
                ``,
                `Preview: ${(data.message || "(no text)").slice(0, 200)}`,
              ].join("\n"),
            },
          ],
        };
      }

      if (platform === "instagram") {
        // Basic metrics (available without Advanced Access)
        const data = await graphFetch(
          `/${post_id}?fields=like_count,comments_count,caption,timestamp,media_type,permalink`
        );

        // Insights require instagram_manage_insights permission
        let insights = null;
        try {
          const insightsRes = await graphFetch(
            `/${post_id}/insights?metric=impressions,reach,engagement`
          );
          insights = insightsRes.data || [];
        } catch {
          // Insights may not be available on all account types — graceful skip
        }

        const insightLines = insights
          ? insights.map((m) => `  ${m.name.padEnd(15)} ${m.values?.[0]?.value ?? m.value ?? "-"}`)
          : ["  (Insights not available — requires instagram_manage_insights permission)"];

        logAction("instagram_summary_fetched", {
          post_id,
          like_count: data.like_count,
          comments_count: data.comments_count,
        });

        return {
          content: [
            {
              type: "text",
              text: [
                `Instagram Post Summary — ${post_id}`,
                `URL: ${data.permalink || "n/a"}`,
                `Type: ${data.media_type || "unknown"}  Published: ${data.timestamp || "unknown"}`,
                ``,
                `Engagement:`,
                `  Likes:    ${data.like_count ?? 0}`,
                `  Comments: ${data.comments_count ?? 0}`,
                ``,
                `Insights:`,
                ...insightLines,
                ``,
                `Caption: ${(data.caption || "(none)").slice(0, 200)}`,
              ].join("\n"),
            },
          ],
        };
      }

      if (platform === "twitter") {
        if (!twitterRead) {
          return {
            content: [
              { type: "text", text: "TWITTER_BEARER_TOKEN not set — required for reading tweet metrics." },
            ],
            isError: true,
          };
        }

        const result = await twitterRead.v2.singleTweet(post_id, {
          "tweet.fields": ["public_metrics", "created_at", "text", "author_id"],
        });

        const m        = result.data.public_metrics || {};
        const tweetUrl = `https://twitter.com/i/web/status/${post_id}`;

        logAction("twitter_summary_fetched", { tweet_id: post_id, metrics: m });

        return {
          content: [
            {
              type: "text",
              text: [
                `Twitter Tweet Summary — ${post_id}`,
                `URL: ${tweetUrl}`,
                `Posted: ${result.data.created_at || "unknown"}`,
                ``,
                `Engagement:`,
                `  Likes:        ${m.like_count       ?? 0}`,
                `  Retweets:     ${m.retweet_count     ?? 0}`,
                `  Replies:      ${m.reply_count       ?? 0}`,
                `  Quotes:       ${m.quote_count       ?? 0}`,
                `  Bookmarks:    ${m.bookmark_count    ?? 0}`,
                `  Impressions:  ${m.impression_count  ?? 0}`,
                ``,
                `Text: ${result.data.text || "(empty)"}`,
              ].join("\n"),
            },
          ],
        };
      }
    } catch (err) {
      return { content: [{ type: "text", text: `Summary failed: ${err.message}` }], isError: true };
    }
  }
);

// ── Tool: social_rate_limit_status ────────────────────────────────────────────

server.tool(
  "social_rate_limit_status",
  "Show current rate-limit usage for all social platforms. Use before batch posting to avoid hitting limits.",
  {},
  async () => {
    const lines = [
      "Social Post MCP — Rate Limit Status",
      "─".repeat(50),
      rateLimiters.facebook.status(),
      rateLimiters.instagram.status(),
      rateLimiters.twitter.status(),
      "",
      `DRY_RUN: ${DRY_RUN}`,
      `Facebook credentials: ${FB_TOKEN ? "configured" : "MISSING"}`,
      `Instagram credentials: ${FB_TOKEN && IG_USER_ID ? "configured" : "MISSING"}`,
      `Twitter write credentials: ${twitterWrite ? "configured" : "MISSING"}`,
      `Twitter read credentials:  ${twitterRead  ? "configured" : "MISSING"}`,
    ];
    return { content: [{ type: "text", text: lines.join("\n") }] };
  }
);

// ── Start server ──────────────────────────────────────────────────────────────

async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
}

main().catch((err) => {
  console.error("Social Post MCP server failed to start:", err);
  process.exit(1);
});
