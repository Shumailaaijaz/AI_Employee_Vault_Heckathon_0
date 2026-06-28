"""Filesystem Watcher - Monitors the /Inbox folder for new file drops.

When files are dropped into /Inbox, this watcher creates corresponding
action files in /Needs_Action for Claude to process.

Usage:
    uv run python filesystem_watcher.py
"""

import shutil
import os
from pathlib import Path
from datetime import datetime, timezone

from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver
from watchdog.events import FileSystemEventHandler

from base_watcher import BaseWatcher

# === CONFIG ===
VAULT_PATH = os.getenv("VAULT_PATH", "/mnt/d/AI_Employee_Vault/AI_Employee_Vault")


class InboxHandler(FileSystemEventHandler):
    """Handles new files appearing in the /Inbox folder."""

    def __init__(self, watcher: "FileSystemWatcher"):
        self.watcher = watcher

    def on_created(self, event):
        if event.is_directory:
            return
        source = Path(event.src_path)
        # Ignore hidden files and .md metadata files we create
        if source.name.startswith("."):
            return
        self.watcher.logger.info(f"New file detected: {source.name}")
        self.watcher.create_action_file(source)


class FileSystemWatcher(BaseWatcher):
    def __init__(self, vault_path: str):
        super().__init__(vault_path, check_interval=5)
        self.inbox = self.vault_path / "Inbox"
        self.inbox.mkdir(parents=True, exist_ok=True)
        self.processed_files: set[str] = set()

    def check_for_updates(self) -> list:
        """Check /Inbox for any files not yet processed."""
        items = []
        if self.inbox.exists():
            for f in self.inbox.iterdir():
                if f.is_file() and not f.name.startswith(".") and str(f) not in self.processed_files:
                    items.append(f)
        return items

    def create_action_file(self, item) -> Path:
        """Create an action .md file in /Needs_Action for a dropped file."""
        source = Path(item)
        if str(source) in self.processed_files:
            return Path()

        now = datetime.now(timezone.utc)
        timestamp = now.strftime("%Y-%m-%d")
        safe_name = "".join(
            c for c in source.stem if c.isalnum() or c in " -_"
        ).strip()

        # Create action file
        action_filename = f"FILE_{safe_name}_{timestamp}.md"
        action_path = self.needs_action / action_filename

        size_kb = source.stat().st_size / 1024
        extension = source.suffix.lower()

        content = f"""---
type: file_drop
original_name: {source.name}
original_path: {source}
size_kb: {size_kb:.1f}
extension: {extension}
detected: {now.isoformat()}
status: pending
---

# New File: {source.name}

A new file was dropped into the Inbox folder.

## File Details
- **Name**: {source.name}
- **Size**: {size_kb:.1f} KB
- **Type**: {extension or 'unknown'}
- **Detected**: {now.strftime('%Y-%m-%d %H:%M:%S')} UTC

## Suggested Actions
- [ ] Review file contents
- [ ] Categorize and move to appropriate folder
- [ ] Process according to file type
- [ ] Move to /Done when complete
"""
        action_path.write_text(content)
        self.processed_files.add(str(source))
        self.logger.info(f"Action file created: {action_path.name}")

        self.log_action(
            "file_drop_detected",
            {
                "filename": source.name,
                "size_kb": round(size_kb, 1),
                "action_file": str(action_path),
            },
        )
        return action_path

    def run(self):
        """Run both polling and event-based file watching."""
        self.logger.info(f"Watching /Inbox at: {self.inbox}")

        # Process any files already in Inbox
        existing = self.check_for_updates()
        for item in existing:
            self.create_action_file(item)
        if existing:
            self.logger.info(f"Processed {len(existing)} existing file(s)")

        # Use PollingObserver for WSL2/NTFS compatibility (inotify doesn't
        # work on /mnt/* drives). Falls back to periodic stat() checks.
        handler = InboxHandler(self)
        observer = PollingObserver(timeout=self.check_interval)
        observer.schedule(handler, str(self.inbox), recursive=False)
        observer.start()
        self.logger.info("Polling file observer started (WSL2-compatible)")

        try:
            while True:
                # Also run our own poll to catch anything the observer misses
                new_items = self.check_for_updates()
                for item in new_items:
                    self.create_action_file(item)
                observer.join(timeout=self.check_interval)
        except KeyboardInterrupt:
            self.logger.info("Shutting down filesystem watcher...")
            observer.stop()
        observer.join()


if __name__ == "__main__":
    watcher = FileSystemWatcher(vault_path=VAULT_PATH)
    watcher.run()
