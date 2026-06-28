"""Twitter/X Watcher — Monitors mentions and DMs via Twitter API v2.

Uses the official Twitter API v2 (tweepy) instead of Playwright:
  - More reliable than DOM scraping
  - Respects Twitter's official rate limits
  - Supports both mentions timeline and direct messages

Auth setup (one-time):
  1. Go to developer.twitter.com → create an app
  2. Set app permissions to "Read + Direct Messages"
  3. Generate all four OAuth 1.0a credentials + Bearer Token
  4. Add all five values to .env (see env vars below)

Optional OAuth 2.0 refresh token (for user-context token rotation):
  5. Set TWITTER_OAUTH2_CLIENT_ID + TWITTER_OAUTH2_CLIENT_SECRET
  6. Run the PKCE flow once to obtain an initial refresh token
  7. Set TWITTER_OAUTH2_REFRESH_TOKEN in .env
  8. The watcher will automatically refresh when the access token expires

What gets flagged:
  - Mentions (@yourhandle) matching ACTION_KEYWORDS → high priority
  - All DMs from anyone → high priority (DMs always need attention)
  - Any mention from VIP_ACCOUNTS regardless of keywords → critical priority

Retry / resilience:
  - Transient Twitter server errors (5xx) → exponential backoff, max 3 attempts
  - Rate limit (429) → skip cycle, log retry-after time if available
  - Auth errors → attempt OAuth 2.0 token refresh, then rebuild client
  - After MAX_CONSECUTIVE_FAILURES cycles → log CRITICAL, pause for FAILURE_PAUSE_S

Rate limits (Twitter free tier):
  - Mentions timeline: 5 requests / 15 min (app-level)
  - DM conversations: 300 requests / 15 min
  - CHECK_INTERVAL defaults to 150s to stay safely within mentions limit

Environment variables:
    VAULT_PATH                      Path to Obsidian vault
    TWITTER_BEARER_TOKEN            App-only bearer token (read-only, mentions)
    TWITTER_API_KEY                 OAuth 1.0a consumer key
    TWITTER_API_SECRET              OAuth 1.0a consumer secret
    TWITTER_ACCESS_TOKEN            OAuth 1.0a access token
    TWITTER_ACCESS_TOKEN_SECRET     OAuth 1.0a access token secret
    TWITTER_OAUTH2_CLIENT_ID        (optional) OAuth 2.0 client ID for token refresh
    TWITTER_OAUTH2_CLIENT_SECRET    (optional) OAuth 2.0 client secret
    TWITTER_OAUTH2_REFRESH_TOKEN    (optional) OAuth 2.0 refresh token
    TWITTER_CHECK_INTERVAL          Poll interval in seconds (default: 150)
    TWITTER_MAX_FAILURES            Consecutive errors before pause (default: 5)
    TWITTER_FAILURE_PAUSE           Pause seconds after max failures (default: 600)

Usage:
    uv run python twitter_watcher.py
    TWITTER_CHECK_INTERVAL=300 uv run python twitter_watcher.py
"""

import os
import re
import time
import json
from pathlib import Path
from datetime import datetime, timezone

import tweepy
from dotenv import load_dotenv

from base_watcher import BaseWatcher
from error_recovery import RetryConfig

load_dotenv()

# ── Config ─────────────────────────────────────────────────────────────────────

VAULT_PATH     = os.getenv("VAULT_PATH", "/mnt/d/AI_Employee_Vault/AI_Employee_Vault")
CHECK_INTERVAL = int(os.getenv("TWITTER_CHECK_INTERVAL", "150"))

# OAuth 1.0a credentials (primary auth — required for DMs and posting)
BEARER_TOKEN        = os.getenv("TWITTER_BEARER_TOKEN", "")
API_KEY             = os.getenv("TWITTER_API_KEY", "")
API_SECRET          = os.getenv("TWITTER_API_SECRET", "")
ACCESS_TOKEN        = os.getenv("TWITTER_ACCESS_TOKEN", "")
ACCESS_TOKEN_SECRET = os.getenv("TWITTER_ACCESS_TOKEN_SECRET", "")

# OAuth 2.0 PKCE credentials (optional — enables automatic token refresh)
OAUTH2_CLIENT_ID      = os.getenv("TWITTER_OAUTH2_CLIENT_ID", "")
OAUTH2_CLIENT_SECRET  = os.getenv("TWITTER_OAUTH2_CLIENT_SECRET", "")
OAUTH2_REFRESH_TOKEN  = os.getenv("TWITTER_OAUTH2_REFRESH_TOKEN", "")

# Resilience settings
MAX_CONSECUTIVE_FAILURES = int(os.getenv("TWITTER_MAX_FAILURES", "5"))
FAILURE_PAUSE_S          = int(os.getenv("TWITTER_FAILURE_PAUSE", "600"))

# Retry settings for transient errors
RETRY_BASE_DELAY_S = 5    # first retry after 5 s
RETRY_MAX_ATTEMPTS = 3    # 5 s → 10 s → 20 s

# Keywords that flag a mention as requiring action
ACTION_KEYWORDS: list[str] = [
    "urgent", "help", "invoice", "payment", "issue", "problem",
    "question", "hire", "collab", "partnership", "project",
    "pricing", "demo", "feedback", "review",
]

# Twitter usernames (without @) whose activity is ALWAYS flagged
VIP_ACCOUNTS: list[str] = [
    # e.g. "elonmusk", "client_handle"
]

MAX_MENTIONS_PER_POLL = 20
MAX_DM_EVENTS         = 20

# Path to persist OAuth 2.0 tokens between restarts
_TOKEN_CACHE_FILE = Path(VAULT_PATH) / ".twitter_oauth2_cache.json"


# ── OAuth 2.0 token store ──────────────────────────────────────────────────────

def _load_token_cache() -> dict:
    """Load cached OAuth 2.0 tokens from disk."""
    try:
        if _TOKEN_CACHE_FILE.exists():
            return json.loads(_TOKEN_CACHE_FILE.read_text())
    except Exception:
        pass
    return {}


def _save_token_cache(data: dict) -> None:
    """Persist OAuth 2.0 tokens to disk (vault root, gitignored)."""
    try:
        _TOKEN_CACHE_FILE.write_text(json.dumps(data, indent=2))
    except Exception:
        pass  # non-fatal — tokens live in env as fallback


# ── Watcher ────────────────────────────────────────────────────────────────────

class TwitterWatcher(BaseWatcher):
    """
    Watches Twitter/X for new @mentions and Direct Messages.

    Resilience:
      - Transient 5xx errors: exponential back-off retry (3 attempts)
      - Rate limits (429): skip cycle, log retry-after
      - Auth errors: attempt OAuth 2.0 refresh, rebuild client
      - MAX_CONSECUTIVE_FAILURES cycles: pause FAILURE_PAUSE_S seconds
    """

    def __init__(
        self,
        vault_path: str,
        bearer_token: str        = BEARER_TOKEN,
        api_key: str             = API_KEY,
        api_secret: str          = API_SECRET,
        access_token: str        = ACCESS_TOKEN,
        access_token_secret: str = ACCESS_TOKEN_SECRET,
    ):
        super().__init__(vault_path, check_interval=CHECK_INTERVAL)

        self._bearer_token        = bearer_token
        self._api_key             = api_key
        self._api_secret          = api_secret
        self._access_token        = access_token
        self._access_token_secret = access_token_secret

        # Load cached OAuth 2.0 tokens (overrides env if newer)
        self._token_cache = _load_token_cache()

        # tweepy client — rebuilt on auth failure
        self.client: tweepy.Client | None = None

        # Authenticated user info (fetched once)
        self.user_id: str | None  = None
        self.username: str | None = None

        # Pagination: newest mention ID seen so far
        self.last_mention_id: str | None = None

        # Dedup set for DM event IDs
        self.processed_ids: set[str] = set()

        # Consecutive failure counter for circuit-breaker
        self._consecutive_failures: int = 0

        self._validate_credentials()
        self._authenticate()

    # ── Credential validation ──────────────────────────────────────────────────

    def _validate_credentials(self) -> None:
        """Warn clearly about missing credentials at startup."""
        missing = []
        if not self._bearer_token:
            missing.append("TWITTER_BEARER_TOKEN (required for mentions)")
        if not self._api_key or not self._access_token:
            missing.append("TWITTER_API_KEY + TWITTER_ACCESS_TOKEN (required for DMs)")
        if missing:
            self.logger.warning(
                "Twitter credentials incomplete. Missing:\n  " + "\n  ".join(missing) +
                "\nAdd them to .env — some features may not work."
            )

    # ── Authentication ─────────────────────────────────────────────────────────

    def _authenticate(self) -> None:
        """Build (or rebuild) the tweepy.Client from current credentials."""
        if not self._bearer_token and not (self._api_key and self._access_token):
            return

        self.client = tweepy.Client(
            bearer_token=self._bearer_token,
            consumer_key=self._api_key,
            consumer_secret=self._api_secret,
            access_token=self._access_token,
            access_token_secret=self._access_token_secret,
            wait_on_rate_limit=False,   # we handle rate limits manually
        )
        self.logger.info("Twitter API v2 client created")

    def _try_oauth2_refresh(self) -> bool:
        """
        Attempt to refresh OAuth 2.0 access token using the stored refresh token.
        Updates self._bearer_token with the new access token if successful.
        Returns True on success, False if refresh is not configured or failed.
        """
        refresh_token = (
            self._token_cache.get("refresh_token")
            or OAUTH2_REFRESH_TOKEN
        )
        if not (OAUTH2_CLIENT_ID and OAUTH2_CLIENT_SECRET and refresh_token):
            return False  # OAuth 2.0 not configured

        try:
            # tweepy OAuth2UserHandler for token refresh
            handler = tweepy.OAuth2UserHandler(
                client_id=OAUTH2_CLIENT_ID,
                redirect_uri="https://localhost",   # not used for refresh
                scope=["tweet.read", "users.read", "dm.read"],
                client_secret=OAUTH2_CLIENT_SECRET,
            )
            # Call the OAuth2 token endpoint directly via requests
            import requests
            response = requests.post(
                "https://api.twitter.com/2/oauth2/token",
                data={
                    "grant_type":    "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id":     OAUTH2_CLIENT_ID,
                },
                auth=(OAUTH2_CLIENT_ID, OAUTH2_CLIENT_SECRET),
                timeout=10,
            )
            response.raise_for_status()
            token_data = response.json()

            new_access_token  = token_data.get("access_token")
            new_refresh_token = token_data.get("refresh_token", refresh_token)

            if not new_access_token:
                self.logger.warning("OAuth 2.0 refresh returned no access_token")
                return False

            # Update bearer token and rebuild client
            self._bearer_token = new_access_token

            # Persist new tokens
            self._token_cache = {
                "access_token":  new_access_token,
                "refresh_token": new_refresh_token,
                "refreshed_at":  datetime.now(timezone.utc).isoformat(),
            }
            _save_token_cache(self._token_cache)

            self._authenticate()
            self.user_id = None  # re-fetch user ID with new token
            self.logger.info("OAuth 2.0 token refreshed successfully")
            return True

        except Exception as exc:
            self.logger.error(f"OAuth 2.0 token refresh failed: {exc}")
            return False

    # ── Retry helper ───────────────────────────────────────────────────────────

    def _retrying_call(self, fn, *args, label: str = "Twitter API", **kwargs):
        """
        Call fn(*args, **kwargs) with exponential back-off on transient errors.

        Delegates retry logic to self.recovery.retry() (error_recovery module)
        while preserving Twitter-specific exception handling:
          - TooManyRequests (429): skip immediately, honour Retry-After header
          - Unauthorized (401): attempt OAuth 2.0 refresh then retry once
          - TwitterServerProblem (5xx): retry up to RETRY_MAX_ATTEMPTS times
          - Forbidden / other TweepyException: skip immediately

        Returns the API response or None on failure.
        """
        def _twitter_call():
            try:
                return fn(*args, **kwargs)

            except tweepy.TooManyRequests as exc:
                # Rate limit — honour Retry-After if present, but do not retry
                retry_after = None
                if hasattr(exc, "response") and exc.response is not None:
                    retry_after = exc.response.headers.get("retry-after")
                wait_msg = f" (retry-after: {retry_after}s)" if retry_after else ""
                self.logger.warning(f"{label}: rate limit hit{wait_msg} — skipping cycle")
                self._consecutive_failures += 1
                return None   # signal caller: skip this cycle, no retry

            except tweepy.Unauthorized as exc:
                self.logger.warning(f"{label}: auth error — attempting OAuth 2.0 refresh")
                if self._try_oauth2_refresh():
                    return fn(*args, **kwargs)  # one retry after refresh
                self.logger.error(f"{label}: refresh unavailable: {exc}")
                self._consecutive_failures += 1
                return None

            except tweepy.Forbidden as exc:
                self.logger.warning(f"{label}: forbidden (check app permissions): {exc}")
                self._consecutive_failures += 1
                return None

            except tweepy.TweepyException:
                raise   # let error_recovery handle retries

        result = self.recovery.retry(
            _twitter_call,
            config=RetryConfig(
                max_attempts     = RETRY_MAX_ATTEMPTS,
                base_delay       = float(RETRY_BASE_DELAY_S),
                max_delay        = 60.0,
                exponential_base = 2.0,
                retryable_excs   = (tweepy.TwitterServerProblem, tweepy.TweepyException),
            ),
            label    = label,
            fallback = None,
        )
        if result is not None:
            self._consecutive_failures = 0
        return result

    def _check_circuit_breaker(self) -> bool:
        """
        If too many consecutive failures, pause and write a human alert.
        Returns True if safe to continue.
        """
        if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            self.logger.critical(
                f"Twitter watcher: {self._consecutive_failures} consecutive failures. "
                f"Pausing for {FAILURE_PAUSE_S}s before resuming."
            )
            self.log_action("twitter_circuit_breaker_triggered", {
                "consecutive_failures": self._consecutive_failures,
                "pause_seconds":        FAILURE_PAUSE_S,
            })
            self.recovery.alert(
                level="ERROR",
                message=(
                    f"TwitterWatcher paused after {self._consecutive_failures} consecutive "
                    f"failures. Will resume in {FAILURE_PAUSE_S}s."
                ),
                operation="circuit_breaker",
                action=(
                    "Check TWITTER_BEARER_TOKEN and API credentials in .env. "
                    "If rate-limited, wait for the pause to expire automatically."
                ),
            )
            time.sleep(FAILURE_PAUSE_S)
            self._consecutive_failures = 0  # reset after pause
        return True

    # ── User identity ──────────────────────────────────────────────────────────

    def _ensure_user_id(self) -> bool:
        """Fetch and cache the authenticated user's ID and handle."""
        if self.user_id:
            return True
        if not self.client:
            return False

        response = self._retrying_call(
            self.client.get_me,
            user_fields=["id", "name", "username"],
            label="get_me",
        )
        if response and response.data:
            self.user_id  = str(response.data.id)
            self.username = response.data.username
            self.logger.info(f"Authenticated as @{self.username} (id={self.user_id})")
            return True
        return False

    # ── Mention scanning ───────────────────────────────────────────────────────

    def _check_mentions(self) -> list[dict]:
        """
        Fetch @mentions since the last processed mention ID.
        Returns only mentions that match ACTION_KEYWORDS or come from VIP_ACCOUNTS.
        """
        if not self.client or not self._ensure_user_id():
            return []

        response = self._retrying_call(
            self.client.get_users_mentions,
            id=self.user_id,
            since_id=self.last_mention_id,
            max_results=MAX_MENTIONS_PER_POLL,
            tweet_fields=["created_at", "author_id", "text", "conversation_id"],
            expansions=["author_id"],
            user_fields=["name", "username"],
            label="get_users_mentions",
        )

        if response is None or not response.data:
            return []

        # Build author lookup from expansions
        author_map: dict[str, str] = {}
        if response.includes and "users" in response.includes:
            for user in response.includes["users"]:
                author_map[str(user.id)] = f"{user.name} (@{user.username})"

        # Advance pagination cursor to newest mention
        newest_id = str(response.data[0].id)
        if self.last_mention_id is None or int(newest_id) > int(self.last_mention_id):
            self.last_mention_id = newest_id

        flagged: list[dict] = []
        for tweet in response.data:
            tweet_id  = str(tweet.id)
            text      = tweet.text or ""
            author_id = str(tweet.author_id)
            author    = author_map.get(author_id, f"user:{author_id}")
            created   = str(tweet.created_at or "")
            conv_id   = str(getattr(tweet, "conversation_id", tweet_id) or tweet_id)

            if tweet_id in self.processed_ids:
                continue

            handle_match = re.search(r"@(\w+)", author)
            handle       = handle_match.group(1).lower() if handle_match else ""

            is_vip           = any(vip.lower() == handle for vip in VIP_ACCOUNTS)
            matched_keywords = [kw for kw in ACTION_KEYWORDS if kw in text.lower()]

            if is_vip or matched_keywords:
                flagged.append({
                    "id":               tweet_id,
                    "conversation_id":  conv_id,
                    "type":             "mention",
                    "author":           author,
                    "text":             text,
                    "created_at":       created,
                    "matched_keywords": matched_keywords,
                    "is_vip":           is_vip,
                    "priority":         "critical" if is_vip else "high",
                    "tweet_url":        f"https://twitter.com/i/web/status/{tweet_id}",
                })

        return flagged

    # ── DM scanning ────────────────────────────────────────────────────────────

    def _check_dms(self) -> list[dict]:
        """
        Fetch recent DM events from all conversations.
        All inbound DMs are flagged — they always require attention.
        """
        if not self.client or not self._ensure_user_id():
            return []

        response = self._retrying_call(
            self.client.get_dm_events,
            max_results=MAX_DM_EVENTS,
            dm_event_fields=["id", "text", "created_at", "sender_id"],
            expansions=["sender_id"],
            user_fields=["name", "username"],
            label="get_dm_events",
        )

        if response is None or not response.data:
            return []

        # Build sender lookup
        sender_map: dict[str, str] = {}
        if response.includes and "users" in response.includes:
            for user in response.includes["users"]:
                sender_map[str(user.id)] = f"{user.name} (@{user.username})"

        flagged: list[dict] = []
        for event in response.data:
            event_id  = str(event.id)
            sender_id = str(event.sender_id or "")
            text      = event.text or ""
            created   = str(event.created_at or "")

            if sender_id == self.user_id:   # skip own messages
                continue
            if event_id in self.processed_ids:
                continue

            sender           = sender_map.get(sender_id, f"user:{sender_id}")
            matched_keywords = [kw for kw in ACTION_KEYWORDS if kw in text.lower()]

            flagged.append({
                "id":               event_id,
                "conversation_id":  None,
                "type":             "dm",
                "author":           sender,
                "text":             text,
                "created_at":       created,
                "matched_keywords": matched_keywords,
                "is_vip":           False,
                "priority":         "high",
                "tweet_url":        "https://twitter.com/messages",
            })

        return flagged

    # ── BaseWatcher interface ──────────────────────────────────────────────────

    def check_for_updates(self) -> list[dict]:
        """Check for new @mentions and DMs. Applies circuit-breaker guard."""
        self._check_circuit_breaker()
        items: list[dict] = []
        items.extend(self._check_mentions())
        items.extend(self._check_dms())
        return items

    def create_action_file(self, item: dict) -> Path:
        """Write a TWITTER_*.md action file to /Needs_Action."""
        if item["id"] in self.processed_ids:
            return Path()

        now         = datetime.now(timezone.utc)
        safe_author = re.sub(r"[^\w-]", "_", item["author"])[:40].strip("_")
        type_tag    = "DM" if item["type"] == "dm" else "MENTION"
        filename    = f"TWITTER_{type_tag}_{safe_author}_{now.strftime('%Y-%m-%d_%H%M%S')}.md"
        filepath    = self.needs_action / filename

        keywords_str = (
            ", ".join(item["matched_keywords"])
            if item["matched_keywords"]
            else "VIP contact" if item["is_vip"] else "direct message"
        )
        tweet_text   = item["text"].replace('"', "'")
        conv_id      = item.get("conversation_id") or item["id"]

        content = f"""---
type: twitter_{item['type']}
from: "{item['author']}"
preview: "{tweet_text[:200]}"
tweet_url: "{item['tweet_url']}"
conversation_id: "{conv_id}"
matched_keywords: [{keywords_str}]
is_vip: {str(item['is_vip']).lower()}
priority: {item['priority']}
twitter_created_at: "{item['created_at']}"
detected: {now.isoformat()}
status: pending
---

# Twitter {type_tag}: {item['author']}

## Message
> {item['text']}

## Context
- **Type**: {"Direct Message" if item['type'] == 'dm' else "@Mention"}
- **Priority**: {item['priority'].upper()}
- **Triggered by**: {keywords_str}
- **Link**: {item['tweet_url']}
- **Conversation ID**: {conv_id}

## Suggested Actions
- [ ] Read full {"conversation" if item['type'] == 'dm' else "mention thread"} at {item['tweet_url']}
- [ ] Use `twitter_generate_summary("{conv_id}")` for thread metrics
- [ ] Draft reply → write to /Pending_Approval if business-critical
- [ ] Move to /Done when handled
"""
        filepath.write_text(content, encoding="utf-8")
        self.processed_ids.add(item["id"])

        self.logger.info(
            f"Action created: {filename} ({type_tag} from {item['author']}, keywords: {keywords_str})"
        )
        self.log_action(
            f"twitter_{item['type']}_flagged",
            {
                "author":          item["author"],
                "type":            item["type"],
                "preview":         item["text"][:100],
                "keywords":        item["matched_keywords"],
                "is_vip":          item["is_vip"],
                "conversation_id": conv_id,
                "tweet_url":       item["tweet_url"],
                "action_file":     str(filepath),
            },
        )
        return filepath


# ── Entry Point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    watcher = TwitterWatcher(vault_path=VAULT_PATH)
    watcher.run()
