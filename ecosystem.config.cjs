/**
 * pm2 Ecosystem Config — Personal AI Employee
 *
 * Manages all long-running processes for the Gold-tier AI Employee stack:
 *   - 5 MCP servers  (Node.js)
 *   - 8 Python watchers  (uv run python)
 *   - 1 Orchestrator (Python)
 *
 * Quick start:
 *   npm install -g pm2          # one-time
 *   pm2 start ecosystem.config.cjs
 *   pm2 save                    # persist across reboots
 *   pm2 startup                 # generate systemd/launchd hook
 *
 * Useful commands:
 *   pm2 status                  # table of all apps
 *   pm2 logs mcp-twitter        # tail a specific app's logs
 *   pm2 restart mcp-odoo        # restart one app
 *   pm2 reload ecosystem.config.cjs --update-env   # reload with new env vars
 *   pm2 stop all                # graceful stop
 *   pm2 delete all              # remove from pm2 registry
 *
 * WSL2 note:
 *   Run `nvm use 24` once in your shell before starting pm2 so Node.js 24
 *   is on PATH. Then `pm2 start ecosystem.config.cjs` will pick it up.
 *
 * Startup groups (pm2 does not enforce order natively — use orchestrator.py
 * for sequenced startup):
 *   Priority 1 — Critical MCPs:   mcp-gmail, mcp-odoo
 *   Priority 2 — Social MCPs:     mcp-linkedin, mcp-twitter, mcp-social
 *   Priority 3 — Python watchers: watcher-*
 *   Priority 4 — Orchestrator:    orchestrator
 */

"use strict";

const VAULT   = process.env.VAULT_PATH || "/mnt/d/AI_Employee_Vault/AI_Employee_Vault";
const MCP_DIR = `${VAULT}/mcp-servers`;

// Detect Python: prefer the uv virtual-env Python, fall back to system python3
const fs   = require("fs");
const path = require("path");
const uvPython = path.join(VAULT, ".venv", "bin", "python3");
const PYTHON   = fs.existsSync(uvPython) ? uvPython : "python3";

// ── Shared defaults ───────────────────────────────────────────────────────────

/** Settings shared by all MCP servers. */
const mcpDefaults = {
  cwd:            VAULT,
  interpreter:    "node",
  autorestart:    true,
  max_restarts:   5,
  min_uptime:     "10s",        // must stay alive ≥10s to count as a stable start
  restart_delay:  3000,         // 3 s between auto-restarts
  watch:          false,        // don't watch filesystem (stdio-based)
  log_date_format: "YYYY-MM-DD HH:mm:ss",
  env: {
    VAULT_PATH: VAULT,
    DRY_RUN:    process.env.DRY_RUN || "true",
    NODE_ENV:   "production",
  },
};

/** Settings shared by all Python watchers. */
const watcherDefaults = {
  cwd:            VAULT,
  interpreter:    PYTHON,
  autorestart:    true,
  max_restarts:   10,
  min_uptime:     "30s",        // watchers must live ≥30s to count as stable
  restart_delay:  5000,
  watch:          false,
  log_date_format: "YYYY-MM-DD HH:mm:ss",
  env: {
    VAULT_PATH: VAULT,
    DRY_RUN:    process.env.DRY_RUN || "true",
    PYTHONUNBUFFERED: "1",      // ensure logs are not buffered
  },
};

// ── Merge helper ──────────────────────────────────────────────────────────────

function mcp(name, script, extraEnv = {}) {
  return {
    ...mcpDefaults,
    name,
    script,
    env: { ...mcpDefaults.env, ...extraEnv },
    error_file: `${VAULT}/Logs/pm2-${name}-error.log`,
    out_file:   `${VAULT}/Logs/pm2-${name}-out.log`,
  };
}

function watcher(name, script, extraEnv = {}) {
  return {
    ...watcherDefaults,
    name,
    script,
    env: { ...watcherDefaults.env, ...extraEnv },
    error_file: `${VAULT}/Logs/pm2-${name}-error.log`,
    out_file:   `${VAULT}/Logs/pm2-${name}-out.log`,
  };
}

// ── App Definitions ───────────────────────────────────────────────────────────

module.exports = {
  apps: [

    // ── Priority 1: Critical MCP Servers ─────────────────────────────────────

    mcp("mcp-gmail", `${MCP_DIR}/gmail-send/index.js`, {
      GMAIL_CLIENT_ID:      process.env.GMAIL_CLIENT_ID      || "",
      GMAIL_CLIENT_SECRET:  process.env.GMAIL_CLIENT_SECRET  || "",
      GMAIL_ACCESS_TOKEN:   process.env.GMAIL_ACCESS_TOKEN   || "",
      GMAIL_REFRESH_TOKEN:  process.env.GMAIL_REFRESH_TOKEN  || "",
      GMAIL_FROM_ADDRESS:   process.env.GMAIL_FROM_ADDRESS   || "",
    }),

    mcp("mcp-odoo", `${MCP_DIR}/odoo-accounting/index.js`, {
      ODOO_URL:            process.env.ODOO_URL            || "http://localhost:8069",
      ODOO_DB:             process.env.ODOO_DB             || "odoo-ai-FTE",
      ODOO_USER:           process.env.ODOO_USER           || "hdrshumaila@gmail.com",
      ODOO_PASS:           process.env.ODOO_PASS           || "112211@5533@Khi",
      ODOO_CURRENCY:       process.env.ODOO_CURRENCY       || "USD",
      ODOO_SALES_JOURNAL:  process.env.ODOO_SALES_JOURNAL  || "",
    }),

    // ── Priority 2: Social MCP Servers ───────────────────────────────────────

    mcp("mcp-linkedin", `${MCP_DIR}/linkedin-post/index.js`, {
      LINKEDIN_ACCESS_TOKEN: process.env.LINKEDIN_ACCESS_TOKEN || "",
    }),

    mcp("mcp-twitter", `${MCP_DIR}/twitter/index.js`, {
      TWITTER_BEARER_TOKEN:           process.env.TWITTER_BEARER_TOKEN           || "",
      TWITTER_API_KEY:                process.env.TWITTER_API_KEY                || "",
      TWITTER_API_SECRET:             process.env.TWITTER_API_SECRET             || "",
      TWITTER_ACCESS_TOKEN:           process.env.TWITTER_ACCESS_TOKEN           || "",
      TWITTER_ACCESS_TOKEN_SECRET:    process.env.TWITTER_ACCESS_TOKEN_SECRET    || "",
      TWITTER_OAUTH2_CLIENT_ID:       process.env.TWITTER_OAUTH2_CLIENT_ID       || "",
      TWITTER_OAUTH2_CLIENT_SECRET:   process.env.TWITTER_OAUTH2_CLIENT_SECRET   || "",
      TWITTER_OAUTH2_REFRESH_TOKEN:   process.env.TWITTER_OAUTH2_REFRESH_TOKEN   || "",
      TWITTER_MAX_TWEETS_PER_DAY:     process.env.TWITTER_MAX_TWEETS_PER_DAY     || "15",
    }),

    mcp("mcp-social", `${MCP_DIR}/social-post/index.js`, {
      FACEBOOK_PAGE_ACCESS_TOKEN: process.env.FACEBOOK_PAGE_ACCESS_TOKEN || "",
      FACEBOOK_PAGE_ID:           process.env.FACEBOOK_PAGE_ID           || "",
      FACEBOOK_GRAPH_VERSION:     process.env.FACEBOOK_GRAPH_VERSION     || "v19.0",
      INSTAGRAM_USER_ID:          process.env.INSTAGRAM_USER_ID          || "",
      TWITTER_API_KEY:            process.env.TWITTER_API_KEY            || "",
      TWITTER_API_SECRET:         process.env.TWITTER_API_SECRET         || "",
      TWITTER_ACCESS_TOKEN:       process.env.TWITTER_ACCESS_TOKEN       || "",
      TWITTER_ACCESS_TOKEN_SECRET:process.env.TWITTER_ACCESS_TOKEN_SECRET|| "",
      TWITTER_BEARER_TOKEN:       process.env.TWITTER_BEARER_TOKEN       || "",
    }),

    mcp("mcp-youtube", `${MCP_DIR}/youtube/index.js`, {
      YOUTUBE_CLIENT_ID:      process.env.YOUTUBE_CLIENT_ID      || "",
      YOUTUBE_CLIENT_SECRET:  process.env.YOUTUBE_CLIENT_SECRET  || "",
      YOUTUBE_ACCESS_TOKEN:   process.env.YOUTUBE_ACCESS_TOKEN   || "",
      YOUTUBE_REFRESH_TOKEN:  process.env.YOUTUBE_REFRESH_TOKEN  || "",
    }),

    // Dedicated Facebook/Instagram read+reply MCP (complements social-post's publish tools)
    mcp("mcp-facebook", `${MCP_DIR}/facebook-mcp/index.js`, {
      FACEBOOK_PAGE_ACCESS_TOKEN:  process.env.FACEBOOK_PAGE_ACCESS_TOKEN  || "",
      FACEBOOK_PAGE_ID:            process.env.FACEBOOK_PAGE_ID            || "",
      INSTAGRAM_USER_ID:           process.env.INSTAGRAM_USER_ID           || "",
      FACEBOOK_GRAPH_VERSION:      process.env.FACEBOOK_GRAPH_VERSION      || "v19.0",
      FB_MAX_REPLIES_PER_HOUR:     process.env.FB_MAX_REPLIES_PER_HOUR     || "20",
      FB_MAX_MESSAGES_PER_HOUR:    process.env.FB_MAX_MESSAGES_PER_HOUR    || "10",
    }),

    // ── Priority 3: Python Watchers ───────────────────────────────────────────

    watcher("watcher-file",      `${VAULT}/filesystem_watcher.py`),

    watcher("watcher-gmail",     `${VAULT}/gmail_watcher.py`, {
      GMAIL_CREDENTIALS_PATH: process.env.GMAIL_CREDENTIALS_PATH || "",
    }),

    watcher("watcher-whatsapp",  `${VAULT}/whatsapp_cloud_watcher.py`, {
      WHATSAPP_PHONE_NUMBER_ID: process.env.WHATSAPP_PHONE_NUMBER_ID || "",
      WHATSAPP_ACCESS_TOKEN:    process.env.WHATSAPP_ACCESS_TOKEN    || "",
      WHATSAPP_VERIFY_TOKEN:    process.env.WHATSAPP_VERIFY_TOKEN    || "myverifytoken123",
      WHATSAPP_APP_SECRET:      process.env.WHATSAPP_APP_SECRET      || "",
      WHATSAPP_WEBHOOK_PORT:    process.env.WHATSAPP_WEBHOOK_PORT    || "8080",
    }),

    watcher("watcher-linkedin",  `${VAULT}/linkedin_watcher.py`, {
      LINKEDIN_SESSION_DIR:    process.env.LINKEDIN_SESSION_DIR   || "",
      LINKEDIN_CHECK_INTERVAL: process.env.LINKEDIN_CHECK_INTERVAL|| "90",
      LINKEDIN_HEADLESS:       process.env.LINKEDIN_HEADLESS       || "true",
    }),

    watcher("watcher-youtube",   `${VAULT}/youtube_watcher.py`, {
      YOUTUBE_ACCESS_TOKEN:    process.env.YOUTUBE_ACCESS_TOKEN   || "",
      YOUTUBE_REFRESH_TOKEN:   process.env.YOUTUBE_REFRESH_TOKEN  || "",
    }),

    watcher("watcher-twitter",   `${VAULT}/twitter_watcher.py`, {
      TWITTER_BEARER_TOKEN:           process.env.TWITTER_BEARER_TOKEN           || "",
      TWITTER_API_KEY:                process.env.TWITTER_API_KEY                || "",
      TWITTER_API_SECRET:             process.env.TWITTER_API_SECRET             || "",
      TWITTER_ACCESS_TOKEN:           process.env.TWITTER_ACCESS_TOKEN           || "",
      TWITTER_ACCESS_TOKEN_SECRET:    process.env.TWITTER_ACCESS_TOKEN_SECRET    || "",
      TWITTER_OAUTH2_REFRESH_TOKEN:   process.env.TWITTER_OAUTH2_REFRESH_TOKEN   || "",
      TWITTER_CHECK_INTERVAL:         process.env.TWITTER_CHECK_INTERVAL         || "150",
    }),

    watcher("watcher-facebook",  `${VAULT}/facebook_watcher.py`, {
      FACEBOOK_PAGE_ACCESS_TOKEN: process.env.FACEBOOK_PAGE_ACCESS_TOKEN || "",
      FACEBOOK_PAGE_ID:           process.env.FACEBOOK_PAGE_ID           || "",
      INSTAGRAM_USER_ID:          process.env.INSTAGRAM_USER_ID          || "",
      FACEBOOK_CHECK_INTERVAL:    process.env.FACEBOOK_CHECK_INTERVAL    || "180",
    }),

    // ── Priority 4: Cross-Domain Integrator ──────────────────────────────────

    watcher("watcher-cross-domain", `${VAULT}/cross_domain_integration.py`, {
      CROSS_DOMAIN_POLL_INTERVAL:      process.env.CROSS_DOMAIN_POLL_INTERVAL      || "30",
      CROSS_DOMAIN_CORRELATION_WINDOW: process.env.CROSS_DOMAIN_CORRELATION_WINDOW || "24",
    }),

    // ── Priority 5: Weekly Audit (cron-style, not a daemon) ──────────────────
    // pm2 runs weekly_audit.py on a cron schedule.
    // This is the preferred alternative to a raw crontab entry.

    {
      ...watcherDefaults,
      name:        "weekly-audit",
      script:      `${VAULT}/weekly_audit.py`,
      cron_restart: "0 21 * * 0",   // Every Sunday at 21:00 UTC
      autorestart:  false,           // Run-once job — do NOT loop
      watch:        false,
      max_restarts: 1,
      min_uptime:   "5s",
      restart_delay: 0,
      env: {
        ...watcherDefaults.env,
        BANK_CSV_DIR: process.env.BANK_CSV_DIR || "Accounting/bank_statements",
      },
      error_file: `${VAULT}/Logs/pm2-weekly-audit-error.log`,
      out_file:   `${VAULT}/Logs/pm2-weekly-audit-out.log`,
    },

    // ── Priority 6: Orchestrator ──────────────────────────────────────────────
    // Run with --no-watchers since pm2 manages watchers directly above.

    {
      ...watcherDefaults,
      name:        "orchestrator",
      script:      `${VAULT}/orchestrator.py`,
      args:        "--no-watchers",      // pm2 owns watchers; orchestrator owns logic
      max_restarts: 3,
      min_uptime:   "60s",
      restart_delay: 10000,
      env: {
        ...watcherDefaults.env,
        ORCHESTRATOR_POLL_INTERVAL: process.env.ORCHESTRATOR_POLL_INTERVAL || "15",
        RALPH_MAX_ITERATIONS:       process.env.RALPH_MAX_ITERATIONS       || "10",
        CLAUDE_CMD:                 process.env.CLAUDE_CMD                 || "claude",
      },
      error_file: `${VAULT}/Logs/pm2-orchestrator-error.log`,
      out_file:   `${VAULT}/Logs/pm2-orchestrator-out.log`,
    },

  ],
};
