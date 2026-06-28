"""LinkedIn Watcher - Monitors LinkedIn for new DMs and mentions via Playwright.

=== WHY PLAYWRIGHT INSTEAD OF THE API? ===

LinkedIn's official REST API has severe limitations for personal use:
  - Messaging API: Restricted to LinkedIn Marketing/Sales Navigator partners.
    Regular developer apps CANNOT read or send DMs.
  - Notifications/Mentions: No public API endpoint for @mentions.
  - Profile API: Only returns your own basic profile (name, email).
  - Posting API: Available via "Share on LinkedIn" (w_member_social scope),
    but reading feed/mentions is not supported for basic apps.

To apply for LinkedIn API access (for the posting features):
  1. Go to https://developer.linkedin.com/
  2. Create an app → Request "Sign In with LinkedIn using OpenID Connect"
  3. For posting: Request "Share on LinkedIn" (w_member_social)
  4. You will NOT get messaging access unless you're an approved partner.

So this watcher uses Playwright to scrape LinkedIn Web, similar to the
WhatsApp Watcher pattern. This is fragile (LinkedIn changes DOM often)
but is the only practical path for DMs and mentions.

Authentication:
    1. First run opens a visible browser. Log in to LinkedIn manually.
    2. Session cookies are saved to LINKEDIN_SESSION_DIR.
    3. Subsequent runs reuse the session (headless).
    4. If session expires, re-run with LINKEDIN_HEADLESS=false to re-login.
    5. LinkedIn may trigger 2FA — complete it in the visible browser.

Important:
    - LinkedIn actively blocks automation. Use conservative polling intervals
      (90s+ recommended) to avoid rate limits or account restrictions.
    - This is for personal/educational use. Respect LinkedIn's User Agreement.
    - Consider using the official API for posting (see linkedin_post_skill.md).

Usage:
    # First run (visible browser for login):
    LINKEDIN_HEADLESS=false uv run python linkedin_watcher.py

    # Subsequent runs (headless):
    uv run python linkedin_watcher.py
"""

import os
import re
from pathlib import Path
from datetime import datetime, timezone

from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

from base_watcher import BaseWatcher
from error_recovery import RetryConfig

# === CONFIG ===
VAULT_PATH = os.getenv("VAULT_PATH", "/mnt/d/AI_Employee_Vault/AI_Employee_Vault")
LINKEDIN_SESSION_DIR = os.getenv(
    "LINKEDIN_SESSION_DIR",
    str(Path.home() / ".linkedin_session"),
)
HEADLESS = os.getenv("LINKEDIN_HEADLESS", "true").lower() == "true"
CHECK_INTERVAL = int(os.getenv("LINKEDIN_CHECK_INTERVAL", "90"))  # seconds

# Keywords that flag a DM as requiring action
ACTION_KEYWORDS = [
    "urgent",
    "opportunity",
    "invoice",
    "proposal",
    "meeting",
    "interview",
    "offer",
    "partnership",
    "collaborate",
    "pricing",
    "project",
    "deadline",
    "hire",
]

# Contacts whose messages ALWAYS create action files
VIP_CONTACTS: list[str] = [
    # Add LinkedIn display names, e.g.:
    # "John Smith",
    # "Jane Doe",
]


class LinkedInWatcher(BaseWatcher):
    """Watches LinkedIn Web for unread DMs and notification mentions."""

    def __init__(self, vault_path: str, session_dir: str, headless: bool = True):
        super().__init__(vault_path, check_interval=CHECK_INTERVAL)
        self.session_dir = Path(session_dir)
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.headless = headless
        self.processed_ids: set[str] = set()

        self._playwright = None
        self._browser = None
        self._page = None

    # ------------------------------------------------------------------
    # Browser lifecycle
    # ------------------------------------------------------------------

    def _launch_browser(self):
        self.logger.info(
            f"Launching browser (headless={self.headless}, "
            f"session={self.session_dir})"
        )
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.session_dir),
            headless=self.headless,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
            ],
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
        self._page = (
            self._browser.pages[0]
            if self._browser.pages
            else self._browser.new_page()
        )

    def _navigate_to_linkedin(self):
        """Open LinkedIn and verify we're logged in."""
        # Use 90s timeout — WSL2 headless browsers can be slow on first navigation
        self._page.set_default_navigation_timeout(90_000)
        self._page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
        self.logger.info("Waiting for LinkedIn to load (log in if needed)...")

        try:
            # Wait for the global nav (proves we're logged in)
            self._page.wait_for_selector(
                'div.global-nav, nav.global-nav__nav',
                timeout=120_000,
            )
            self.logger.info("LinkedIn loaded — logged in")
        except PwTimeout:
            self.logger.error(
                "Timed out waiting for LinkedIn. "
                "Re-run with LINKEDIN_HEADLESS=false to log in."
            )
            raise

    def _close_browser(self):
        if self._browser:
            self._browser.close()
        if self._playwright:
            self._playwright.stop()

    # ------------------------------------------------------------------
    # DM scanning
    # ------------------------------------------------------------------

    def _check_messaging(self) -> list[dict]:
        """Navigate to messaging and scan for unread conversations."""
        messages: list[dict] = []

        def _navigate():
            self._page.goto(
                "https://www.linkedin.com/messaging/",
                wait_until="domcontentloaded",
            )
            self._page.wait_for_selector(
                'ul.msg-conversations-container__conversations-list, '
                'div.msg-conversations-container',
                timeout=30_000,
            )

        nav_ok = self.recovery.retry(
            _navigate,
            config   = RetryConfig(max_attempts=3, base_delay=5, max_delay=30,
                                   retryable_excs=(PwTimeout, Exception)),
            label    = "LinkedIn.messaging.navigate",
            fallback = False,
        )
        if nav_ok is False:
            return []

        try:
            # Brief pause for DOM to settle
            self._page.wait_for_timeout(2000)

            # Find conversation items with unread indicators
            conv_items = self._page.query_selector_all(
                'li.msg-conversation-listitem'
            )

            for conv in conv_items:
                try:
                    # Check for unread badge
                    unread_badge = conv.query_selector(
                        '.msg-conversation-card__unread-count, '
                        'span[class*="notification-badge"], '
                        'span[class*="unread"]'
                    )
                    if not unread_badge:
                        continue

                    # Extract sender name
                    name_el = conv.query_selector(
                        'h3.msg-conversation-listitem__participant-names, '
                        'h3.msg-conversation-card__participant-names, '
                        'span.msg-conversation-listitem__participant-names'
                    )
                    sender = name_el.inner_text().strip() if name_el else "Unknown"

                    # Extract message preview
                    preview_el = conv.query_selector(
                        'p.msg-conversation-card__message-snippet, '
                        'p.msg-conversation-listitem__message-snippet, '
                        'div.msg-conversation-card__message-snippet-body'
                    )
                    preview = preview_el.inner_text().strip() if preview_el else ""

                    # Extract timestamp
                    time_el = conv.query_selector(
                        'time.msg-conversation-listitem__time-stamp, '
                        'time.msg-conversation-card__time-stamp'
                    )
                    timestamp = time_el.get_attribute("datetime") if time_el else ""

                    msg_id = f"dm_{sender}_{timestamp or preview[:30]}"
                    if msg_id in self.processed_ids:
                        continue

                    # Keyword / VIP matching
                    preview_lower = preview.lower()
                    is_vip = any(
                        vip.lower() in sender.lower() for vip in VIP_CONTACTS
                    )
                    matched_keywords = [
                        kw for kw in ACTION_KEYWORDS if kw in preview_lower
                    ]

                    if matched_keywords or is_vip:
                        messages.append(
                            {
                                "id": msg_id,
                                "source": "dm",
                                "sender": sender,
                                "preview": preview,
                                "timestamp": timestamp,
                                "matched_keywords": matched_keywords,
                                "is_vip": is_vip,
                                "priority": "critical" if is_vip else "high",
                            }
                        )

                except Exception as e:
                    self.logger.debug(f"Skipping conversation element: {e}")

        except PwTimeout:
            self.logger.warning("Messaging page didn't load in time — skipping DM check")
        except Exception as e:
            self.logger.error(f"Error checking messages: {e}")

        return messages

    # ------------------------------------------------------------------
    # Notification / mention scanning
    # ------------------------------------------------------------------

    def _check_notifications(self) -> list[dict]:
        """Scan the notifications page for @mentions and post interactions."""
        mentions: list[dict] = []

        def _navigate():
            self._page.goto(
                "https://www.linkedin.com/notifications/",
                wait_until="domcontentloaded",
            )
            self._page.wait_for_selector(
                'div.nt-card-list, section.nt-card-list',
                timeout=30_000,
            )

        nav_ok = self.recovery.retry(
            _navigate,
            config   = RetryConfig(max_attempts=3, base_delay=5, max_delay=30,
                                   retryable_excs=(PwTimeout, Exception)),
            label    = "LinkedIn.notifications.navigate",
            fallback = False,
        )
        if nav_ok is False:
            return []

        try:
            self._page.wait_for_timeout(2000)

            notif_cards = self._page.query_selector_all(
                'article.nt-card, div.nt-card'
            )

            for card in notif_cards[:15]:  # Only check recent 15
                try:
                    # Check if it's unread (has the blue dot / bold styling)
                    is_unread = card.query_selector(
                        '.nt-card__unread-indicator, '
                        'span[class*="unread"], '
                        'div[class*="unread"]'
                    )
                    if not is_unread:
                        continue

                    text_el = card.query_selector(
                        '.nt-card__text, '
                        'div.nt-card__text--multi-line, '
                        'span.nt-card__text'
                    )
                    text = text_el.inner_text().strip() if text_el else ""

                    time_el = card.query_selector(
                        'time, span.nt-card__time-ago'
                    )
                    timestamp = (
                        time_el.get_attribute("datetime")
                        if time_el
                        else ""
                    )

                    notif_id = f"notif_{text[:50]}_{timestamp}"
                    if notif_id in self.processed_ids:
                        continue

                    # Only flag mentions, comments on your posts, and connection messages
                    text_lower = text.lower()
                    is_mention = any(
                        trigger in text_lower
                        for trigger in [
                            "mentioned you",
                            "commented on your",
                            "replied to your",
                            "tagged you",
                            "sent you a",
                            "wants to connect",
                            "accepted your",
                        ]
                    )

                    if is_mention:
                        mentions.append(
                            {
                                "id": notif_id,
                                "source": "notification",
                                "sender": text.split(" ")[0] if text else "Unknown",
                                "preview": text,
                                "timestamp": timestamp,
                                "matched_keywords": [],
                                "is_vip": False,
                                "priority": "medium",
                            }
                        )

                except Exception as e:
                    self.logger.debug(f"Skipping notification card: {e}")

        except PwTimeout:
            self.logger.warning("Notifications page didn't load — skipping")
        except Exception as e:
            self.logger.error(f"Error checking notifications: {e}")

        return mentions

    # ------------------------------------------------------------------
    # BaseWatcher interface
    # ------------------------------------------------------------------

    def check_for_updates(self) -> list:
        """Check both DMs and notifications for actionable items."""
        items = []
        items.extend(self._check_messaging())
        items.extend(self._check_notifications())
        return items

    def create_action_file(self, item: dict) -> Path:
        """Create an action .md file in /Needs_Action."""
        if item["id"] in self.processed_ids:
            return Path()

        now = datetime.now(timezone.utc)
        safe_sender = re.sub(r"[^\w\s-]", "", item["sender"]).strip().replace(" ", "_")
        source_tag = "DM" if item["source"] == "dm" else "MENTION"
        filename = (
            f"LINKEDIN_{source_tag}_{safe_sender}_{now.strftime('%Y-%m-%d_%H%M%S')}.md"
        )
        filepath = self.needs_action / filename

        keywords_str = (
            ", ".join(item["matched_keywords"])
            if item["matched_keywords"]
            else "VIP contact" if item["is_vip"] else "notification trigger"
        )

        content = f"""---
type: linkedin_{item['source']}
from: "{item['sender']}"
preview: "{item['preview'][:200]}"
source: {item['source']}
matched_keywords: [{keywords_str}]
is_vip: {str(item['is_vip']).lower()}
priority: {item['priority']}
linkedin_timestamp: "{item.get('timestamp', '')}"
detected: {now.isoformat()}
status: pending
---

# LinkedIn {source_tag}: {item['sender']}

## Message Preview
> {item['preview']}

## Context
- **Type**: {"Direct Message" if item['source'] == 'dm' else "Notification / Mention"}
- **Triggered by**: {keywords_str}
- **Priority**: {item['priority'].upper()}
- **LinkedIn time**: {item.get('timestamp', 'N/A')}

## Suggested Actions
- [ ] Open LinkedIn and read full {"conversation" if item['source'] == 'dm' else "notification"}
- [ ] Draft response (write to /Pending_Approval if business-related)
- [ ] {"Reply to message" if item['source'] == 'dm' else "Engage with post/comment"}
- [ ] Move to /Done when handled
"""
        filepath.write_text(content)
        self.processed_ids.add(item["id"])
        self.logger.info(
            f"Action created: {filename} ({item['source']} from {item['sender']})"
        )

        self.log_action(
            f"linkedin_{item['source']}_flagged",
            {
                "sender": item["sender"],
                "source": item["source"],
                "preview": item["preview"][:100],
                "keywords": item["matched_keywords"],
                "action_file": str(filepath),
            },
        )
        return filepath

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self):
        """Launch browser, log in to LinkedIn, then poll for DMs and mentions."""
        self._launch_browser()
        try:
            self._navigate_to_linkedin()
            self.logger.info(
                f"Polling every {self.check_interval}s for DMs and mentions"
            )
            super().run()
        finally:
            self.logger.info("Closing browser...")
            self._close_browser()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="LinkedIn Watcher")
    parser.add_argument(
        "--setup",
        action="store_true",
        help="Force visible browser (headless=False) for initial login session",
    )
    args = parser.parse_args()

    # --setup overrides LINKEDIN_HEADLESS → opens visible browser for login
    headless = False if args.setup else HEADLESS

    if args.setup:
        print(
            "\n[SETUP MODE] A visible browser window will open.\n"
            "  1. Log in to LinkedIn in the browser\n"
            "  2. Complete any 2FA if prompted\n"
            "  3. Wait for the feed to load, then press Ctrl+C to save session\n"
            f"  Session saved to: {LINKEDIN_SESSION_DIR}\n"
        )

    watcher = LinkedInWatcher(
        vault_path=VAULT_PATH,
        session_dir=LINKEDIN_SESSION_DIR,
        headless=headless,
    )
    watcher.run()
