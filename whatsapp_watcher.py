"""WhatsApp Watcher - Monitors WhatsApp Web for unread messages via Playwright.

Uses a persistent Chromium browser context so you only scan the QR code once.
On each poll it finds unread chats, reads their latest messages, and creates
action files in /Needs_Action when keyword-matched or from VIP contacts.

Authentication:
    1. First run opens a visible browser window with the WhatsApp Web QR page.
    2. Scan the QR code with your phone (WhatsApp → Linked Devices → Link a Device).
    3. Session is saved to WHATSAPP_SESSION_DIR — subsequent runs reuse it silently.
    4. If the session expires (usually ~14 days of inactivity) you'll need to re-scan.
    5. Set WHATSAPP_HEADLESS=false in .env to force a visible window for re-auth.

Important:
    - WhatsApp Web automation may conflict with WhatsApp's Terms of Service.
      Use this for personal/educational purposes at your own discretion.
    - Only ONE browser session can be linked at a time per WhatsApp account.
    - Keep your phone connected to the internet for the session to stay alive.

Usage:
    # First run (visible browser for QR scan):
    WHATSAPP_HEADLESS=false uv run python whatsapp_watcher.py

    # Subsequent runs (headless):
    uv run python whatsapp_watcher.py
"""

import os
import re
from pathlib import Path
from datetime import datetime, timezone

from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

from base_watcher import BaseWatcher

# === CONFIG ===
VAULT_PATH = os.getenv("VAULT_PATH", "/mnt/d/AI_Employee_Vault/AI_Employee_Vault")
WHATSAPP_SESSION_DIR = os.getenv(
    "WHATSAPP_SESSION_DIR",
    str(Path.home() / ".whatsapp_session"),
)
HEADLESS = os.getenv("WHATSAPP_HEADLESS", "true").lower() == "true"
CHECK_INTERVAL = int(os.getenv("WHATSAPP_CHECK_INTERVAL", "45"))  # seconds

# Keywords that flag a message as requiring action
URGENT_KEYWORDS = [
    "urgent",
    "asap",
    "invoice",
    "payment",
    "help",
    "emergency",
    "deadline",
    "reminder",
    "overdue",
]

# Contacts whose messages ALWAYS create action files regardless of keywords
VIP_CONTACTS: list[str] = [
    # Add names exactly as they appear in WhatsApp, e.g.:
    # "Boss Name",
    # "Client A",
]


class WhatsAppWatcher(BaseWatcher):
    """Watches WhatsApp Web for unread messages that match keywords or VIP senders."""

    def __init__(self, vault_path: str, session_dir: str, headless: bool = True):
        super().__init__(vault_path, check_interval=CHECK_INTERVAL)
        self.session_dir = Path(session_dir)
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.headless = headless
        self.processed_ids: set[str] = set()

        # Playwright objects — created in run(), closed on exit
        self._playwright = None
        self._browser = None
        self._page = None

    # ------------------------------------------------------------------
    # Browser lifecycle
    # ------------------------------------------------------------------

    def _launch_browser(self):
        """Launch (or reattach to) a persistent Chromium context."""
        import os
        # Ensure WSLg display env vars are set for headed mode
        uid = os.getuid()
        os.environ.setdefault("DISPLAY", ":0")
        os.environ.setdefault("WAYLAND_DISPLAY", "wayland-0")
        os.environ.setdefault("XDG_RUNTIME_DIR", f"/run/user/{uid}")

        self.logger.info(
            f"Launching browser (headless={self.headless}, "
            f"session={self.session_dir})"
        )
        self._playwright = sync_playwright().start()
        # Headed mode uses X11 (available via WSLg at DISPLAY=:0)
        # Headless mode uses no display at all
        self._browser = self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.session_dir),
            headless=self.headless,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-gpu",
                "--single-process",
            ],
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
        self._page = self._browser.pages[0] if self._browser.pages else self._browser.new_page()

    def _navigate_to_whatsapp(self):
        """Open WhatsApp Web and wait for the chat list to appear."""
        self._page.goto("https://web.whatsapp.com", wait_until="domcontentloaded")
        self.logger.info("Waiting for WhatsApp Web to load (scan QR if needed)...")

        # Wait up to 120 s for either the chat list OR the QR canvas
        try:
            self._page.wait_for_selector(
                'div[aria-label="Chat list"], div[aria-label="Chats"]',
                timeout=120_000,
            )
            self.logger.info("WhatsApp Web loaded — chat list visible")
        except PwTimeout:
            self.logger.error(
                "Timed out waiting for WhatsApp Web. "
                "Re-run with WHATSAPP_HEADLESS=false to scan the QR code."
            )
            raise

    def setup_qr(self, qr_path: str) -> bool:
        """Headless QR-code setup mode for WSL2 / no-display environments.

        Navigates to WhatsApp Web, waits for the QR canvas, saves a screenshot
        to *qr_path*, then waits up to 5 minutes for the user to scan it.
        Returns True if authentication succeeded, False on timeout.
        """
        import time

        self._page.goto("https://web.whatsapp.com", wait_until="networkidle")
        self.logger.info("Page loaded — waiting 30s for QR code to render...")

        import time
        time.sleep(30)  # WhatsApp needs ~20-30s to fully render QR in headless mode

        # Screenshot whatever is on screen
        self._page.screenshot(path=qr_path, full_page=True)
        self.logger.info(f"Screenshot taken")
        self.logger.info(f"QR code saved → {qr_path}")
        print(f"\n{'='*60}")
        print(f"  QR code saved to: {qr_path}")
        print(f"  Open this file in Windows Explorer to scan it:")
        print(f"  Windows path: {qr_path.replace('/mnt/d/', 'D:\\\\').replace('/', chr(92))}")
        print(f"  1. Open your phone's WhatsApp")
        print(f"  2. Tap ⋮ → Linked Devices → Link a Device")
        print(f"  3. Scan the QR code in the saved image")
        print(f"{'='*60}\n")

        # Poll for successful login — up to 5 minutes, re-screenshot every 10s
        self.logger.info("Waiting up to 5 minutes for QR scan (screenshot updates every 10s)...")
        deadline = time.time() + 300
        iteration = 0
        while time.time() < deadline:
            # Check if chat list appeared (means QR was scanned successfully)
            try:
                logged_in = self._page.query_selector(
                    'div[aria-label="Chat list"], div[aria-label="Chats"], '
                    '#pane-side, [data-testid="chat-list"]'
                )
                if logged_in:
                    self.logger.info("QR scanned — WhatsApp session authenticated!")
                    return True
            except Exception:
                pass

            # Re-screenshot so user always has the freshest QR
            try:
                self._page.screenshot(path=qr_path, full_page=True)
                if iteration % 3 == 0:  # log every 30s
                    self.logger.info(f"QR screenshot updated → {qr_path}  (scan it now!)")
            except Exception:
                pass

            time.sleep(10)
            iteration += 1

        self.logger.error("QR scan timed out after 5 minutes.")
        return False

    def _close_browser(self):
        if self._browser:
            self._browser.close()
        if self._playwright:
            self._playwright.stop()

    # ------------------------------------------------------------------
    # Core watcher interface
    # ------------------------------------------------------------------

    def check_for_updates(self) -> list:
        """Scan the chat list for unread conversations and extract messages."""
        messages: list[dict] = []

        try:
            # Find all chat rows with an unread badge
            unread_chats = self._page.query_selector_all(
                'div[aria-label="Chat list"] span[aria-label*="unread message"]'
            )
            if not unread_chats:
                # Try alternate selector (WhatsApp updates DOM frequently)
                unread_chats = self._page.query_selector_all(
                    'span[data-testid="icon-unread-count"]'
                )

            for badge in unread_chats:
                try:
                    chat_row = badge.evaluate_handle(
                        "el => el.closest('[data-testid=\"cell-frame-container\"]')"
                    )
                    if not chat_row:
                        chat_row = badge.evaluate_handle(
                            "el => el.closest('[tabindex=\"-1\"]')"
                        )
                    if not chat_row:
                        continue

                    # Extract sender name
                    name_el = chat_row.as_element().query_selector(
                        'span[dir="auto"][title]'
                    )
                    sender = name_el.get_attribute("title") if name_el else "Unknown"

                    # Extract preview text
                    preview_el = chat_row.as_element().query_selector(
                        'span[dir="ltr"], span.matched-text'
                    )
                    preview = preview_el.inner_text() if preview_el else ""

                    # Extract unread count
                    count_text = badge.inner_text().strip()
                    unread_count = int(count_text) if count_text.isdigit() else 1

                    # Dedupe key
                    msg_id = f"{sender}_{preview[:40]}"

                    if msg_id in self.processed_ids:
                        continue

                    # Decide whether this message needs action
                    preview_lower = preview.lower()
                    is_vip = any(
                        vip.lower() in sender.lower() for vip in VIP_CONTACTS
                    )
                    matched_keywords = [
                        kw for kw in URGENT_KEYWORDS if kw in preview_lower
                    ]

                    if matched_keywords or is_vip:
                        priority = "critical" if is_vip else "high"
                        messages.append(
                            {
                                "id": msg_id,
                                "sender": sender,
                                "preview": preview,
                                "unread_count": unread_count,
                                "matched_keywords": matched_keywords,
                                "is_vip": is_vip,
                                "priority": priority,
                            }
                        )
                except Exception as inner_err:
                    self.logger.debug(f"Skipping chat element: {inner_err}")

        except Exception as e:
            self.logger.error(f"Error scanning chats: {e}")

        return messages

    def create_action_file(self, item: dict) -> Path:
        """Write a Needs_Action markdown file for a flagged WhatsApp message."""
        msg = item
        if msg["id"] in self.processed_ids:
            return Path()

        now = datetime.now(timezone.utc)
        safe_sender = re.sub(r"[^\w\s-]", "", msg["sender"]).strip().replace(" ", "_")
        filename = f"WHATSAPP_{safe_sender}_{now.strftime('%Y-%m-%d_%H%M%S')}.md"
        filepath = self.needs_action / filename

        keywords_str = ", ".join(msg["matched_keywords"]) if msg["matched_keywords"] else "none (VIP contact)"

        content = f"""---
type: whatsapp_message
from: "{msg['sender']}"
preview: "{msg['preview'][:200]}"
unread_count: {msg['unread_count']}
matched_keywords: [{keywords_str}]
is_vip: {str(msg['is_vip']).lower()}
priority: {msg['priority']}
detected: {now.isoformat()}
status: pending
---

# WhatsApp: {msg['sender']}

## Message Preview
> {msg['preview']}

## Context
- **Unread messages**: {msg['unread_count']}
- **Triggered by**: {keywords_str}
- **Priority**: {msg['priority'].upper()}

## Suggested Actions
- [ ] Open WhatsApp and read full conversation
- [ ] Draft reply (write to /Pending_Approval if sensitive)
- [ ] Move to /Done when handled
"""
        filepath.write_text(content)
        self.processed_ids.add(msg["id"])
        self.logger.info(
            f"Action created: {filename} (from: {msg['sender']}, "
            f"keywords: {keywords_str})"
        )

        self.log_action(
            "whatsapp_message_flagged",
            {
                "sender": msg["sender"],
                "preview": msg["preview"][:100],
                "keywords": msg["matched_keywords"],
                "is_vip": msg["is_vip"],
                "action_file": str(filepath),
            },
        )
        return filepath

    # ------------------------------------------------------------------
    # Main loop (overrides BaseWatcher.run)
    # ------------------------------------------------------------------

    def run(self):
        """Launch browser, navigate to WhatsApp Web, then poll for messages."""
        self._launch_browser()
        try:
            self._navigate_to_whatsapp()
            self.logger.info(
                f"Polling every {self.check_interval}s for messages "
                f"matching keywords: {URGENT_KEYWORDS}"
            )
            # Delegate to BaseWatcher.run() which calls check/create in a loop
            super().run()
        finally:
            self.logger.info("Closing browser...")
            self._close_browser()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="WhatsApp Watcher")
    parser.add_argument(
        "--qr",
        action="store_true",
        help="Headless QR setup: save QR as PNG so you can scan it from Windows",
    )
    args = parser.parse_args()

    if args.qr:
        # --qr mode: headed browser renders to X11 display, we screenshot the QR
        qr_path = str(Path(VAULT_PATH) / "whatsapp_qr.png")
        watcher = WhatsAppWatcher(
            vault_path=VAULT_PATH,
            session_dir=WHATSAPP_SESSION_DIR,
            headless=False,  # headed so WhatsApp renders properly, X11 via WSLg
        )
        watcher._launch_browser()
        try:
            ok = watcher.setup_qr(qr_path)
            if ok:
                print("\nSession saved. Now run: pm2 start watcher-whatsapp")
        finally:
            watcher._close_browser()
    else:
        watcher = WhatsAppWatcher(
            vault_path=VAULT_PATH,
            session_dir=WHATSAPP_SESSION_DIR,
            headless=HEADLESS,  # respects WHATSAPP_HEADLESS env var
        )
        watcher.run()
