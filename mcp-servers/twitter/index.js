/**
 * Twitter/X MCP Server
 *
 * Dedicated MCP server for Twitter/X with full thread support, OAuth 2.0
 * automatic token refresh, and exponential back-off retries.
 *
 * Tools:
 *   twitter_post_tweet          — Post a tweet (HITL in DRY_RUN mode)
 *   twitter_reply_tweet         — Reply to a specific tweet (HITL)
 *   twitter_generate_summary    — Aggregate metrics for an entire thread
 *   twitter_get_thread          — Fetch all tweets in a conversation chain
 *   twitter_delete_tweet        — Delete a tweet (HITL)
 *   twitter_get_mentions        — Fetch recent @mentions
 *   twitter_get_rate_limit_status — Current rate-limit usage + credential check
 *
 * Auth:
 *   Primary write path  — OAuth 1.0a (API key/secret + access token/secret)
 *   Primary read path   — Bearer token (app-only)
 *   Optional refresh    — OAuth 2.0 PKCE with refresh_token (auto-rotated)
 *
 * Retry strategy (withRetry helper):
 *   - HTTP 429 (rate limit): respect Retry-After header, back off then retry
 *   - HTTP 5xx (server error): exponential back-off, up to MAX_ATTEMPTS
 *   - HTTP 401 (auth): attempt OAuth 2.0 token refresh, rebuild client, retry once
 *   - Other errors: propagate immediately
 *
 * Rate-limit guard:
 *   TWITTER_MAX_TWEETS_PER_DAY (default 15) — checked before every post
 *
 * Environment variables: see .env.example
 */

import "dotenv/config";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { TwitterApi } from "twitter-api-v2";
import { z } from "zod";
import fs from "node:fs";
import path from "node:path";

// ── Config ────────────────────────────────────────────────────────────────────

const VAULT_PATH   = process.env.VAULT_PATH   || "/mnt/d/AI_Employee_Vault/AI_Employee_Vault";
const DRY_RUN      = (process.env.DRY_RUN     || "true").toLowerCase() === "true";
const MAX_PER_DAY  = parseInt(process.env.TWITTER_MAX_TWEETS_PER_DAY || "15", 10);

const TW_API_KEY   = process.env.TWITTER_API_KEY              || "";
const TW_API_SEC   = process.env.TWITTER_API_SECRET           || "";
const TW_ACC_TOK   = process.env.TWITTER_ACCESS_TOKEN         || "";
const TW_ACC_SEC   = process.env.TWITTER_ACCESS_TOKEN_SECRET  || "";
const TW_BEARER    = process.env.TWITTER_BEARER_TOKEN         || "";

const OAUTH2_CLIENT_ID     = process.env.TWITTER_OAUTH2_CLIENT_ID     || "";
const OAUTH2_CLIENT_SECRET = process.env.TWITTER_OAUTH2_CLIENT_SECRET || "";

const TOKEN_CACHE_FILE = path.join(
  VAULT_PATH,
  process.env.TWITTER_TOKEN_CACHE_PATH || ".twitter_oauth2_cache.json"
);

// ── Retry constants ───────────────────────────────────────────────────────────

const MAX_ATTEMPTS     = 4;
const BASE_DELAY_MS    = 1_000;   // 1 s → 2 s → 4 s → 8 s
const MAX_DELAY_MS     = 30_000;  // cap at 30 s

// ── Rate limiter ──────────────────────────────────────────────────────────────

const tweetTimestamps = [];  // sliding window for posted tweets

function canTweet() {
  const cutoff = Date.now() - 24 * 60 * 60_000;
  const recent = tweetTimestamps.filter((t) => t > cutoff);
  tweetTimestamps.length = 0;
  tweetTimestamps.push(...recent);
  return tweetTimestamps.length < MAX_PER_DAY;
}

function recordTweet() {
  tweetTimestamps.push(Date.now());
}

// ── OAuth 2.0 token cache ─────────────────────────────────────────────────────

function loadTokenCache() {
  try {
    if (fs.existsSync(TOKEN_CACHE_FILE)) {
      return JSON.parse(fs.readFileSync(TOKEN_CACHE_FILE, "utf-8"));
    }
  } catch { /* ignore */ }
  return {};
}

function saveTokenCache(data) {
  try {
    fs.writeFileSync(TOKEN_CACHE_FILE, JSON.stringify(data, null, 2), "utf-8");
  } catch { /* non-fatal */ }
}

const tokenCache = loadTokenCache();

// ── Twitter client factory ────────────────────────────────────────────────────

/** OAuth 1.0a write client — required for posting. */
function makeWriteClient() {
  if (!TW_API_KEY || !TW_ACC_TOK) return null;
  return new TwitterApi({
    appKey:       TW_API_KEY,
    appSecret:    TW_API_SEC,
    accessToken:  TW_ACC_TOK,
    accessSecret: TW_ACC_SEC,
  });
}

/** Bearer token read-only client — for fetching tweets/threads. */
function makeReadClient(bearerOverride) {
  const token = bearerOverride || tokenCache.access_token || TW_BEARER;
  return token ? new TwitterApi(token) : null;
}

let _writeClient = makeWriteClient();
let _readClient  = makeReadClient();

// ── OAuth 2.0 token refresh ───────────────────────────────────────────────────

/**
 * Attempt to refresh the OAuth 2.0 access token using the stored refresh token.
 * On success: updates _readClient and persists new tokens.
 * Returns true on success, false if OAuth 2.0 is not configured.
 */
async function tryOAuth2Refresh() {
  const refreshToken = tokenCache.refresh_token
    || process.env.TWITTER_OAUTH2_REFRESH_TOKEN;

  if (!OAUTH2_CLIENT_ID || !OAUTH2_CLIENT_SECRET || !refreshToken) {
    return false;
  }

  try {
    const params = new URLSearchParams({
      grant_type:    "refresh_token",
      refresh_token: refreshToken,
      client_id:     OAUTH2_CLIENT_ID,
    });

    const credentials = Buffer.from(`${OAUTH2_CLIENT_ID}:${OAUTH2_CLIENT_SECRET}`).toString("base64");
    const res = await fetch("https://api.twitter.com/2/oauth2/token", {
      method:  "POST",
      headers: {
        "Content-Type":  "application/x-www-form-urlencoded",
        "Authorization": `Basic ${credentials}`,
      },
      body: params.toString(),
    });

    if (!res.ok) {
      const text = await res.text();
      console.error(`[TwitterMCP] OAuth2 refresh HTTP ${res.status}: ${text}`);
      return false;
    }

    const data = await res.json();
    const newAccessToken  = data.access_token;
    const newRefreshToken = data.refresh_token || refreshToken;

    if (!newAccessToken) return false;

    // Update cache and clients
    Object.assign(tokenCache, {
      access_token:  newAccessToken,
      refresh_token: newRefreshToken,
      refreshed_at:  new Date().toISOString(),
    });
    saveTokenCache(tokenCache);

    _readClient = makeReadClient(newAccessToken);
    console.error("[TwitterMCP] OAuth2 token refreshed successfully");
    return true;

  } catch (err) {
    console.error(`[TwitterMCP] OAuth2 refresh failed: ${err.message}`);
    return false;
  }
}

// ── Retry helper ──────────────────────────────────────────────────────────────

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/**
 * Call fn() with exponential back-off retry.
 *
 * Retryable:
 *   - HTTP 429: respect Retry-After header, then back off
 *   - HTTP 5xx: exponential back-off with jitter
 *   - Network errors: back off
 *
 * Non-retryable after OAuth2 refresh attempt:
 *   - HTTP 401: try token refresh once, rebuild client, retry; fail if still 401
 *
 * @param {Function} fn         - async function to call
 * @param {Object}   opts
 * @param {string}   opts.label - label for logging
 * @param {number}   opts.maxAttempts
 */
async function withRetry(fn, { label = "Twitter API", maxAttempts = MAX_ATTEMPTS } = {}) {
  let lastErr;
  let refreshAttempted = false;

  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    try {
      return await fn();
    } catch (err) {
      lastErr = err;
      const status  = err.code || err.status || 0;
      const headers = err.headers || {};

      // ── 429 Rate limit ──────────────────────────────────────────────────────
      if (status === 429) {
        const retryAfterSec = parseInt(headers["retry-after"] || "0", 10);
        const waitMs = retryAfterSec > 0
          ? retryAfterSec * 1000
          : Math.min(BASE_DELAY_MS * Math.pow(2, attempt - 1) + Math.random() * 500, MAX_DELAY_MS);

        if (attempt < maxAttempts) {
          console.error(`[TwitterMCP] ${label}: rate limit (attempt ${attempt}), waiting ${Math.ceil(waitMs / 1000)}s`);
          await sleep(waitMs);
          continue;
        }
        throw new Error(`${label}: rate limit persists after ${maxAttempts} attempts`);
      }

      // ── 401 Unauthorized — try OAuth2 refresh once ─────────────────────────
      if (status === 401 && !refreshAttempted) {
        refreshAttempted = true;
        console.error(`[TwitterMCP] ${label}: 401 — attempting OAuth2 token refresh`);
        const refreshed = await tryOAuth2Refresh();
        if (refreshed) {
          // re-build fn closure on next iteration (caller must use _readClient/_writeClient)
          continue;
        }
        throw new Error(`${label}: auth failed and token refresh unavailable`);
      }

      // ── 5xx Server errors ──────────────────────────────────────────────────
      if (status >= 500 || err.message?.includes("ECONNRESET") || err.message?.includes("ETIMEDOUT")) {
        if (attempt < maxAttempts) {
          const delay = Math.min(BASE_DELAY_MS * Math.pow(2, attempt - 1) + Math.random() * 500, MAX_DELAY_MS);
          console.error(`[TwitterMCP] ${label}: server error (attempt ${attempt}/${maxAttempts}), retrying in ${Math.ceil(delay / 1000)}s: ${err.message}`);
          await sleep(delay);
          continue;
        }
        throw new Error(`${label}: server error after ${maxAttempts} attempts: ${err.message}`);
      }

      // ── Non-retryable ──────────────────────────────────────────────────────
      throw err;
    }
  }
  throw lastErr;
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
  entries.push({ timestamp: new Date().toISOString(), watcher: "TwitterMCP", action_type: actionType, details });
  fs.writeFileSync(file, JSON.stringify(entries, null, 2), "utf-8");
}

function nowTs() {
  return new Date().toISOString().slice(0, 19).replace(/[T:]/g, "-");
}

function safeName(str, max = 40) {
  return str.slice(0, max).replace(/[^a-zA-Z0-9 ]/g, "").trim().replace(/ /g, "_");
}

// ── MCP Server ────────────────────────────────────────────────────────────────

const server = new McpServer({ name: "twitter", version: "1.0.0" });

// ── Tool: twitter_post_tweet ──────────────────────────────────────────────────

server.tool(
  "twitter_post_tweet",
  `Post a tweet to Twitter/X.
In DRY_RUN mode (default) the tweet is saved to /Pending_Approval for human review.
Supports quote tweets. Uses OAuth 1.0a — requires all four credential env vars.
Rate guard: ${MAX_PER_DAY} tweets/day maximum.`,
  {
    content: z
      .string().min(1).max(280)
      .describe("Tweet text (max 280 chars, Unicode supported)"),
    quote_tweet_id: z
      .string().optional()
      .describe("Tweet ID to quote-tweet"),
  },
  async ({ content, quote_tweet_id }) => {
    const ts       = nowTs();
    const preview  = safeName(content, 40);
    const filename = `TW_TWEET_${preview}_${ts}.md`;

    if (DRY_RUN) {
      const filepath = writeVaultFile("Pending_Approval", filename, `---
type: twitter_tweet
content: "${content.replace(/"/g, "'")}"
quote_tweet_id: "${quote_tweet_id || ""}"
chars: ${content.length}
status: pending_approval
created: ${new Date().toISOString()}
dry_run: true
---

# Tweet Draft (${content.length}/280 chars)

${content}

${quote_tweet_id ? `## Quoting\nhttps://twitter.com/i/web/status/${quote_tweet_id}\n` : ""}
## To Approve
Move to \`/Approved\` and ask Claude to post using \`twitter_post_tweet\`.
`);
      logAction("twitter_tweet_queued", { filename, content_length: content.length });
      return {
        content: [{
          type: "text",
          text: `[DRY RUN] Tweet draft saved to:\n${filepath}\n\nContent (${content.length}/280):\n---\n${content}\n---\n\nMove to /Approved to publish.`,
        }],
      };
    }

    if (!_writeClient) {
      return { content: [{ type: "text", text: "Error: Twitter OAuth 1.0a credentials not configured. Set TWITTER_API_KEY, TWITTER_API_SECRET, TWITTER_ACCESS_TOKEN, TWITTER_ACCESS_TOKEN_SECRET in .env." }], isError: true };
    }
    if (!canTweet()) {
      return { content: [{ type: "text", text: `Rate limit: already at ${MAX_PER_DAY} tweets today. Try again tomorrow.` }], isError: true };
    }

    try {
      const payload = { text: content };
      if (quote_tweet_id) payload.quote_tweet_id = quote_tweet_id;

      const result = await withRetry(() => _writeClient.v2.tweet(payload), { label: "post_tweet" });
      const tweetId  = result.data.id;
      const tweetUrl = `https://twitter.com/i/web/status/${tweetId}`;

      recordTweet();
      logAction("twitter_tweet_posted", { tweet_id: tweetId, tweet_url: tweetUrl, content_length: content.length });

      writeVaultFile("Done", `TW_PUBLISHED_${tweetId}_${ts}.md`,
        `---\ntype: twitter_tweet_published\ntweet_id: "${tweetId}"\ntweet_url: "${tweetUrl}"\npublished: ${new Date().toISOString()}\n---\n\n# Tweet Published\n\n**ID**: ${tweetId}\n**URL**: ${tweetUrl}\n**Content**: ${content}\n`);

      return {
        content: [{ type: "text", text: `Tweet posted!\n\n  ID:      ${tweetId}\n  URL:     ${tweetUrl}\n  Length:  ${content.length}/280 chars` }],
      };
    } catch (err) {
      const msg = err.data?.detail || err.data?.title || err.message;
      return { content: [{ type: "text", text: `Post failed: ${msg}` }], isError: true };
    }
  }
);

// ── Tool: twitter_reply_tweet ─────────────────────────────────────────────────

server.tool(
  "twitter_reply_tweet",
  `Reply to a specific tweet, creating a threaded conversation.
In DRY_RUN mode the reply is saved to /Pending_Approval.
Always use this tool instead of twitter_post_tweet when responding to a mention or DM.`,
  {
    tweet_id: z
      .string().min(1)
      .describe("ID of the tweet to reply to"),
    content: z
      .string().min(1).max(280)
      .describe("Reply text (max 280 chars). You can @mention the author at the start."),
  },
  async ({ tweet_id, content }) => {
    const ts       = nowTs();
    const preview  = safeName(content, 35);
    const filename = `TW_REPLY_${tweet_id}_${preview}_${ts}.md`;
    const tweetUrl = `https://twitter.com/i/web/status/${tweet_id}`;

    if (DRY_RUN) {
      const filepath = writeVaultFile("Pending_Approval", filename, `---
type: twitter_reply
reply_to_tweet_id: "${tweet_id}"
reply_to_url: "${tweetUrl}"
content: "${content.replace(/"/g, "'")}"
chars: ${content.length}
status: pending_approval
created: ${new Date().toISOString()}
dry_run: true
---

# Tweet Reply Draft (${content.length}/280 chars)

**Replying to**: ${tweetUrl}

${content}

## To Approve
Move to \`/Approved\` and ask Claude to post using \`twitter_reply_tweet\`.
`);
      logAction("twitter_reply_queued", { filename, reply_to: tweet_id, content_length: content.length });
      return {
        content: [{ type: "text", text: `[DRY RUN] Reply draft saved to:\n${filepath}\n\nReplying to: ${tweetUrl}\nContent: ${content}` }],
      };
    }

    if (!_writeClient) {
      return { content: [{ type: "text", text: "Error: OAuth 1.0a credentials not set." }], isError: true };
    }
    if (!canTweet()) {
      return { content: [{ type: "text", text: `Rate limit: ${MAX_PER_DAY} tweets/day reached.` }], isError: true };
    }

    try {
      const result = await withRetry(
        () => _writeClient.v2.tweet({ text: content, reply: { in_reply_to_tweet_id: tweet_id } }),
        { label: "reply_tweet" }
      );
      const replyId  = result.data.id;
      const replyUrl = `https://twitter.com/i/web/status/${replyId}`;

      recordTweet();
      logAction("twitter_reply_posted", { reply_id: replyId, reply_to: tweet_id, reply_url: replyUrl });

      writeVaultFile("Done", `TW_REPLY_PUBLISHED_${replyId}_${ts}.md`,
        `---\ntype: twitter_reply_published\nreply_id: "${replyId}"\nreply_to_tweet_id: "${tweet_id}"\nreply_url: "${replyUrl}"\npublished: ${new Date().toISOString()}\n---\n\n# Reply Published\n\n- **Reply ID**: ${replyId}\n- **Replying to**: ${tweetUrl}\n- **URL**: ${replyUrl}\n- **Content**: ${content}\n`);

      return {
        content: [{ type: "text", text: `Reply posted!\n\n  Reply ID:    ${replyId}\n  Reply URL:   ${replyUrl}\n  Replied to:  ${tweetUrl}` }],
      };
    } catch (err) {
      const msg = err.data?.detail || err.data?.title || err.message;
      return { content: [{ type: "text", text: `Reply failed: ${msg}` }], isError: true };
    }
  }
);

// ── Tool: twitter_generate_summary ────────────────────────────────────────────

server.tool(
  "twitter_generate_summary",
  `Generate an aggregated engagement summary for an entire Twitter thread (conversation).
Fetches all tweets sharing the same conversation_id (last 7 days on free tier).
Returns: total tweets, cumulative metrics, top tweet, participant count, time span.
Use thread_id = the root tweet ID (also the conversation_id).`,
  {
    thread_id: z
      .string().min(1)
      .describe("Root tweet ID (= conversation_id). All replies in this conversation are fetched."),
    max_results: z
      .number().int().min(10).max(100).default(100)
      .describe("Max tweets to analyse (default 100, capped by API at 100)"),
  },
  async ({ thread_id, max_results }) => {
    if (!_readClient) {
      return { content: [{ type: "text", text: "Error: TWITTER_BEARER_TOKEN not set — required for reading tweets." }], isError: true };
    }

    try {
      // Fetch the root tweet first (to get its metrics + creation time)
      const rootResponse = await withRetry(
        () => _readClient.v2.singleTweet(thread_id, {
          "tweet.fields": ["public_metrics", "created_at", "text", "author_id"],
          "expansions":   ["author_id"],
          "user.fields":  ["username", "name"],
        }),
        { label: "get_root_tweet" }
      );

      const root = rootResponse.data;
      if (!root) {
        return { content: [{ type: "text", text: `Tweet ID ${thread_id} not found.` }], isError: true };
      }

      // Fetch all replies in the conversation using search/recent
      // Note: search/recent only covers the last 7 days on free tier
      const searchResponse = await withRetry(
        () => _readClient.v2.search(
          `conversation_id:${thread_id} -is:retweet`,
          {
            "tweet.fields":  ["public_metrics", "created_at", "author_id", "in_reply_to_user_id"],
            "expansions":    ["author_id"],
            "user.fields":   ["username", "name"],
            "max_results":   Math.min(max_results, 100),
            "sort_order":    "recency",
          }
        ),
        { label: "search_thread" }
      );

      const replies = searchResponse.data?.data || [];

      // Build author map from expansions across both calls
      const authorMap = new Map();
      for (const res of [rootResponse, searchResponse.data]) {
        const users = res?.includes?.users || [];
        for (const u of users) {
          authorMap.set(u.id, `${u.name} (@${u.username})`);
        }
      }

      // Aggregate metrics across root + all replies
      const allTweets = [root, ...replies];
      let totalLikes       = 0;
      let totalRetweets    = 0;
      let totalReplies     = 0;
      let totalQuotes      = 0;
      let totalImpressions = 0;
      const participants   = new Set();
      let topTweet         = null;
      let topLikes         = -1;
      let oldestCreated    = root.created_at;
      let newestCreated    = root.created_at;

      for (const tweet of allTweets) {
        const m = tweet.public_metrics || {};
        totalLikes       += m.like_count       || 0;
        totalRetweets    += m.retweet_count     || 0;
        totalReplies     += m.reply_count       || 0;
        totalQuotes      += m.quote_count       || 0;
        totalImpressions += m.impression_count  || 0;

        if (tweet.author_id) participants.add(tweet.author_id);

        if ((m.like_count || 0) > topLikes) {
          topLikes = m.like_count || 0;
          topTweet = tweet;
        }

        if (tweet.created_at) {
          if (tweet.created_at < oldestCreated) oldestCreated = tweet.created_at;
          if (tweet.created_at > newestCreated) newestCreated = tweet.created_at;
        }
      }

      const topAuthor = topTweet?.author_id
        ? (authorMap.get(topTweet.author_id) || `user:${topTweet.author_id}`)
        : "unknown";

      const threadUrl = `https://twitter.com/i/web/status/${thread_id}`;

      logAction("twitter_thread_summary", {
        thread_id,
        tweet_count: allTweets.length,
        total_likes: totalLikes,
        total_retweets: totalRetweets,
        participant_count: participants.size,
      });

      const summary = [
        `Twitter Thread Summary — conversation_id: ${thread_id}`,
        `URL: ${threadUrl}`,
        `─`.repeat(60),
        ``,
        `Thread Overview:`,
        `  Root tweet:       ${(root.text || "").slice(0, 100)}`,
        `  Total tweets:     ${allTweets.length} (root + ${replies.length} replies)`,
        `  Participants:     ${participants.size} unique authors`,
        `  Time span:        ${oldestCreated?.slice(0, 10)} → ${newestCreated?.slice(0, 10)}`,
        ``,
        `Cumulative Metrics:`,
        `  Likes:            ${totalLikes}`,
        `  Retweets:         ${totalRetweets}`,
        `  Replies:          ${totalReplies}`,
        `  Quotes:           ${totalQuotes}`,
        `  Impressions:      ${totalImpressions}`,
        ``,
        `Top Tweet (most liked — ${topLikes} likes):`,
        `  Author:  ${topAuthor}`,
        `  Content: ${(topTweet?.text || "").slice(0, 200)}`,
        `  URL:     https://twitter.com/i/web/status/${topTweet?.id || thread_id}`,
        ``,
        `Note: search/recent covers last 7 days only (free tier).`,
      ].join("\n");

      return { content: [{ type: "text", text: summary }] };
    } catch (err) {
      const msg = err.data?.detail || err.message;
      return { content: [{ type: "text", text: `Thread summary failed: ${msg}` }], isError: true };
    }
  }
);

// ── Tool: twitter_get_thread ──────────────────────────────────────────────────

server.tool(
  "twitter_get_thread",
  `Fetch all tweets in a conversation chain (thread).
Returns tweet IDs, authors, text, metrics, and timestamps in chronological order.
Use this to read full context before replying. Covers last 7 days on free tier.`,
  {
    tweet_id: z
      .string().min(1)
      .describe("Any tweet ID in the conversation (ideally the root tweet)"),
    max_results: z
      .number().int().min(10).max(100).default(50)
      .describe("Maximum number of thread tweets to return (default 50)"),
  },
  async ({ tweet_id, max_results }) => {
    if (!_readClient) {
      return { content: [{ type: "text", text: "Error: TWITTER_BEARER_TOKEN not set." }], isError: true };
    }

    try {
      // Get root tweet to confirm conversation_id
      const rootResp = await withRetry(
        () => _readClient.v2.singleTweet(tweet_id, {
          "tweet.fields": ["conversation_id", "created_at", "author_id", "public_metrics"],
          "expansions":   ["author_id"],
          "user.fields":  ["username"],
        }),
        { label: "get_root_for_thread" }
      );

      const convId = rootResp.data?.conversation_id || tweet_id;

      const searchResp = await withRetry(
        () => _readClient.v2.search(
          `conversation_id:${convId} -is:retweet`,
          {
            "tweet.fields": ["created_at", "author_id", "public_metrics", "in_reply_to_user_id", "text"],
            "expansions":   ["author_id"],
            "user.fields":  ["username", "name"],
            "max_results":  Math.min(max_results, 100),
            "sort_order":   "recency",
          }
        ),
        { label: "search_thread_tweets" }
      );

      const replies = searchResp.data?.data || [];
      const allTweets = [rootResp.data, ...replies]
        .filter(Boolean)
        .sort((a, b) => (a.created_at || "").localeCompare(b.created_at || ""));

      // Build author map
      const authorMap = new Map();
      for (const res of [rootResp, searchResp.data]) {
        const users = res?.includes?.users || [];
        for (const u of users) authorMap.set(u.id, `@${u.username}`);
      }

      const rows = allTweets.map((t, i) => {
        const m      = t.public_metrics || {};
        const author = authorMap.get(t.author_id) || `user:${t.author_id}`;
        const flag   = t.id === tweet_id ? " [ROOT]" : "";
        return [
          `${i + 1}. [${t.created_at?.slice(0, 16) || "?"}]${flag} ${author}`,
          `   ID: ${t.id}  Likes:${m.like_count || 0}  RT:${m.retweet_count || 0}  Replies:${m.reply_count || 0}`,
          `   ${(t.text || "").slice(0, 200)}`,
        ].join("\n");
      });

      return {
        content: [{ type: "text", text: [`Thread: conversation_id=${convId} (${allTweets.length} tweets)`, `Root: https://twitter.com/i/web/status/${convId}`, ``, ...rows].join("\n") }],
      };
    } catch (err) {
      const msg = err.data?.detail || err.message;
      return { content: [{ type: "text", text: `Thread fetch failed: ${msg}` }], isError: true };
    }
  }
);

// ── Tool: twitter_delete_tweet ────────────────────────────────────────────────

server.tool(
  "twitter_delete_tweet",
  `Delete a tweet by ID. In DRY_RUN mode logs the intent but does not delete.
Use this to remove incorrectly posted content. Requires OAuth 1.0a write credentials.`,
  {
    tweet_id: z
      .string().min(1)
      .describe("ID of the tweet to delete"),
    reason: z
      .string().optional()
      .describe("Internal reason for deletion (for audit log only, not sent to Twitter)"),
  },
  async ({ tweet_id, reason }) => {
    if (DRY_RUN) {
      logAction("twitter_tweet_delete_queued", { tweet_id, reason });
      return {
        content: [{ type: "text", text: `[DRY RUN] Would delete tweet: https://twitter.com/i/web/status/${tweet_id}\nReason: ${reason || "(none)"}` }],
      };
    }

    if (!_writeClient) {
      return { content: [{ type: "text", text: "Error: OAuth 1.0a credentials not set." }], isError: true };
    }

    try {
      await withRetry(() => _writeClient.v2.deleteTweet(tweet_id), { label: "delete_tweet" });
      logAction("twitter_tweet_deleted", { tweet_id, reason });
      return {
        content: [{ type: "text", text: `Deleted tweet: ${tweet_id}\nReason: ${reason || "(none)"}` }],
      };
    } catch (err) {
      const msg = err.data?.detail || err.message;
      return { content: [{ type: "text", text: `Delete failed: ${msg}` }], isError: true };
    }
  }
);

// ── Tool: twitter_get_mentions ────────────────────────────────────────────────

server.tool(
  "twitter_get_mentions",
  `Fetch the most recent @mentions of the authenticated account.
Returns author, text, metrics, and conversation_id for each mention.
Useful for catching up after a gap or before drafting replies.`,
  {
    count: z
      .number().int().min(5).max(100).default(20)
      .describe("Number of recent mentions to fetch (default 20)"),
    since_id: z
      .string().optional()
      .describe("Only fetch mentions newer than this tweet ID"),
  },
  async ({ count, since_id }) => {
    if (!_readClient) {
      return { content: [{ type: "text", text: "Error: TWITTER_BEARER_TOKEN not set." }], isError: true };
    }

    try {
      // Need user ID — use write client if available for get_me
      let userId;
      const meClient = _writeClient || _readClient;
      const meResp = await withRetry(
        () => meClient.v2.me({ "user.fields": ["id", "username"] }),
        { label: "get_me_for_mentions" }
      );
      userId = meResp.data?.id;
      if (!userId) {
        return { content: [{ type: "text", text: "Could not determine authenticated user ID." }], isError: true };
      }

      const params = {
        "tweet.fields": ["created_at", "author_id", "public_metrics", "conversation_id"],
        "expansions":   ["author_id"],
        "user.fields":  ["name", "username"],
        "max_results":  Math.min(count, 100),
      };
      if (since_id) params.since_id = since_id;

      const response = await withRetry(
        () => _readClient.v2.userMentionTimeline(userId, params),
        { label: "get_mentions" }
      );

      const mentions = response.data?.data || [];
      if (!mentions.length) {
        return { content: [{ type: "text", text: "No recent mentions found." }] };
      }

      const authorMap = new Map();
      const users = response.data?.includes?.users || [];
      for (const u of users) authorMap.set(u.id, `${u.name} (@${u.username})`);

      const rows = mentions.map((t, i) => {
        const author = authorMap.get(t.author_id) || `user:${t.author_id}`;
        const m      = t.public_metrics || {};
        return `${i + 1}. ${t.created_at?.slice(0, 16) || "?"}  ${author}\n   ID: ${t.id}  Conv: ${t.conversation_id || t.id}  Likes:${m.like_count || 0}\n   ${(t.text || "").slice(0, 200)}\n   URL: https://twitter.com/i/web/status/${t.id}`;
      });

      return { content: [{ type: "text", text: [`${mentions.length} recent mention(s):`, "", ...rows].join("\n") }] };
    } catch (err) {
      const msg = err.data?.detail || err.message;
      return { content: [{ type: "text", text: `Mentions fetch failed: ${msg}` }], isError: true };
    }
  }
);

// ── Tool: twitter_get_rate_limit_status ───────────────────────────────────────

server.tool(
  "twitter_get_rate_limit_status",
  "Show current tweet rate-limit usage, credential status, and OAuth 2.0 token info.",
  {},
  async () => {
    const now     = Date.now();
    const cutoff  = now - 24 * 60 * 60_000;
    const used    = tweetTimestamps.filter((t) => t > cutoff).length;
    const refresh = tokenCache.refreshed_at || "never";

    const lines = [
      "Twitter MCP — Status",
      "─".repeat(50),
      `Rate limit guard:  ${used}/${MAX_PER_DAY} tweets used today`,
      `DRY_RUN:           ${DRY_RUN}`,
      ``,
      `Credentials:`,
      `  OAuth 1.0a write:   ${_writeClient ? "configured" : "MISSING (set TWITTER_API_KEY + ACCESS_TOKEN)"}`,
      `  Bearer token read:  ${_readClient  ? "configured" : "MISSING (set TWITTER_BEARER_TOKEN)"}`,
      `  OAuth 2.0 refresh:  ${OAUTH2_CLIENT_ID && OAUTH2_CLIENT_SECRET ? "configured" : "not configured"}`,
      `  Last token refresh: ${refresh}`,
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
  console.error("Twitter MCP server failed to start:", err);
  process.exit(1);
});
