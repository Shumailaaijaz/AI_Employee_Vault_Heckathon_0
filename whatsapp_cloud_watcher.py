"""
WhatsApp Business Cloud API Watcher
====================================
Replaces the Playwright-based watcher with Meta's official WhatsApp Cloud API.

No browser, no QR code, no display needed — pure HTTP webhooks.

HOW IT WORKS:
  1. Runs a Flask webhook server on port 8080
  2. Meta sends incoming WhatsApp messages to this server
  3. Messages matching keywords are written to /Needs_Action/
  4. MCP server (mcp-social or dedicated) sends replies via API

SETUP (one-time):
  Step 1 — Get credentials from Meta:
    a. Go to https://developers.facebook.com/apps/
    b. Open your app "Hackathon0-FTE"
    c. Add product: WhatsApp → WhatsApp Business
    d. Under "API Setup" copy:
         - Phone Number ID  → WHATSAPP_PHONE_NUMBER_ID
         - Access Token     → WHATSAPP_ACCESS_TOKEN
    e. Use the free Meta test number (no real number needed to start)

  Step 2 — Expose localhost with cloudflared (free, no account needed):
    curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o cloudflared
    chmod +x cloudflared
    ./cloudflared tunnel --url http://localhost:8080
    → Gives you a public URL like https://xxxx.trycloudflare.com

  Step 3 — Register the webhook in Meta:
    a. Go to app → WhatsApp → Configuration → Webhook
    b. Callback URL: https://xxxx.trycloudflare.com/webhook
    c. Verify token: (value of WHATSAPP_VERIFY_TOKEN in .env)
    d. Subscribe to: messages

  Step 4 — Start the watcher:
    uv run python whatsapp_cloud_watcher.py
    OR: pm2 start watcher-whatsapp (after updating ecosystem.config.cjs)

ENVIRONMENT VARIABLES (.env):
  WHATSAPP_PHONE_NUMBER_ID=   # from Meta API Setup page
  WHATSAPP_ACCESS_TOKEN=      # from Meta API Setup page (long-lived)
  WHATSAPP_VERIFY_TOKEN=      # any secret string you choose (e.g. "myverifytoken123")
  WHATSAPP_WEBHOOK_PORT=8080  # port for the webhook server
"""

import hashlib
import hmac
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, request, jsonify

# ── Config ─────────────────────────────────────────────────────────────────────

VAULT_PATH         = Path(os.getenv("VAULT_PATH", "/mnt/d/AI_Employee_Vault/AI_Employee_Vault"))
PHONE_NUMBER_ID    = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
ACCESS_TOKEN       = os.getenv("WHATSAPP_ACCESS_TOKEN", "")
VERIFY_TOKEN       = os.getenv("WHATSAPP_VERIFY_TOKEN", "myverifytoken123")
APP_SECRET         = os.getenv("WHATSAPP_APP_SECRET", "")   # optional, for signature verify
PORT               = int(os.getenv("WHATSAPP_WEBHOOK_PORT", "8080"))
DRY_RUN            = os.getenv("DRY_RUN", "true").lower() == "true"
GRAPH_VER          = "v19.0"
GRAPH_BASE         = f"https://graph.facebook.com/{GRAPH_VER}"

NEEDS_ACTION = VAULT_PATH / "Needs_Action"
LOGS_DIR     = VAULT_PATH / "Logs"

# Keywords that flag a message as requiring action
ACTION_KEYWORDS = [
    "urgent", "invoice", "payment", "proposal", "meeting",
    "interview", "offer", "partnership", "project", "deadline",
    "hire", "contract", "quote", "help", "asap", "important",
]

# VIP numbers — always create action file (include country code, e.g. "923001234567")
VIP_NUMBERS: list[str] = []

# ── Logging ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [WhatsAppCloud] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("WhatsAppCloud")

# ── Flask app ──────────────────────────────────────────────────────────────────

app = Flask(__name__)

# ── Helpers ────────────────────────────────────────────────────────────────────

def _verify_signature(payload: bytes, signature: str) -> bool:
    """Verify X-Hub-Signature-256 from Meta (optional but recommended)."""
    if not APP_SECRET:
        return True  # skip verification if secret not configured
    expected = "sha256=" + hmac.new(
        APP_SECRET.encode(), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def _matches_keywords(text: str) -> list[str]:
    text_lower = text.lower()
    return [kw for kw in ACTION_KEYWORDS if kw in text_lower]


def _sanitize(text: str) -> str:
    return re.sub(r"[^\w\s\-]", "", text)[:50].strip()


def _write_action_file(sender: str, sender_name: str, message: str,
                       msg_id: str, keywords: list[str]) -> Path:
    """Write WHATSAPP_*.md to /Needs_Action/."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    safe_sender = _sanitize(sender_name or sender)
    filename = f"WHATSAPP_{safe_sender}_{ts}.md"
    filepath = NEEDS_ACTION / filename

    content = f"""---
source: whatsapp
channel: whatsapp_cloud
sender: "{sender_name or sender}"
phone: "{sender}"
message_id: "{msg_id}"
keywords: {json.dumps(keywords)}
date: "{datetime.now(timezone.utc).isoformat()}"
mcp_route: "mcp-social"
priority: "{'high' if keywords else 'medium'}"
requires_approval: true
---

# WhatsApp Message — {sender_name or sender}

**From:** {sender_name or sender} (`{sender}`)
**Received:** {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}
**Keywords:** {", ".join(keywords) if keywords else "none"}

## Message

{message}

## Suggested Action

Reply via WhatsApp using the `whatsapp_send_message` MCP tool with phone `{sender}`.

<promise>TASK_COMPLETE</promise>
"""
    filepath.write_text(content)
    logger.info(f"Action file: {filename}")
    return filepath


def _log_action(event: str, data: dict):
    """Append to today's JSON log."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log_file = LOGS_DIR / f"{today}.json"
    entry = {"timestamp": datetime.now(timezone.utc).isoformat(),
             "event": event, **data}
    try:
        existing = json.loads(log_file.read_text()) if log_file.exists() else []
        existing.append(entry)
        log_file.write_text(json.dumps(existing, indent=2))
    except Exception as e:
        logger.warning(f"Log write failed: {e}")


def send_whatsapp_message(to: str, body: str) -> dict:
    """Send a WhatsApp message via Cloud API."""
    import urllib.request
    import urllib.error

    if DRY_RUN:
        logger.info(f"[DRY RUN] Would send to {to}: {body[:80]}")
        return {"dry_run": True, "to": to}

    if not PHONE_NUMBER_ID or not ACCESS_TOKEN:
        return {"error": "WHATSAPP_PHONE_NUMBER_ID or WHATSAPP_ACCESS_TOKEN not set"}

    payload = json.dumps({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body},
    }).encode()

    req = urllib.request.Request(
        f"{GRAPH_BASE}/{PHONE_NUMBER_ID}/messages",
        data=payload,
        headers={
            "Authorization": f"Bearer {ACCESS_TOKEN}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"error": json.loads(e.read().decode())}
    except Exception as e:
        return {"error": str(e)}


# ── Webhook routes ─────────────────────────────────────────────────────────────

@app.get("/webhook")
def webhook_verify():
    """Meta webhook verification handshake."""
    mode      = request.args.get("hub.mode")
    token     = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        logger.info("Webhook verified by Meta")
        return challenge, 200
    else:
        logger.warning(f"Webhook verification failed: token={token}")
        return "Forbidden", 403


@app.post("/webhook")
def webhook_receive():
    """Receive incoming WhatsApp messages from Meta."""
    # Optional signature verification
    sig = request.headers.get("X-Hub-Signature-256", "")
    if APP_SECRET and not _verify_signature(request.data, sig):
        logger.warning("Invalid signature — ignoring request")
        return "Forbidden", 403

    data = request.get_json(silent=True) or {}

    # Parse the WhatsApp message structure
    try:
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})

                # Extract contacts map
                contacts = {
                    c["wa_id"]: c.get("profile", {}).get("name", c["wa_id"])
                    for c in value.get("contacts", [])
                }

                for msg in value.get("messages", []):
                    msg_type = msg.get("type", "")
                    sender   = msg.get("from", "")
                    msg_id   = msg.get("id", "")

                    # Only process text messages for now
                    if msg_type == "text":
                        text = msg["text"]["body"]
                    elif msg_type == "audio":
                        text = "[Voice message received]"
                    elif msg_type == "image":
                        text = "[Image received]"
                    elif msg_type == "document":
                        text = f"[Document: {msg.get('document', {}).get('filename', 'unknown')}]"
                    else:
                        text = f"[{msg_type} message]"

                    sender_name = contacts.get(sender, sender)
                    keywords    = _matches_keywords(text)
                    is_vip      = sender in VIP_NUMBERS

                    logger.info(
                        f"Message from {sender_name} ({sender}): "
                        f"{text[:60]}  keywords={keywords}"
                    )

                    if keywords or is_vip:
                        _write_action_file(sender, sender_name, text, msg_id, keywords)
                        _log_action("whatsapp_message_flagged", {
                            "sender": sender,
                            "sender_name": sender_name,
                            "keywords": keywords,
                            "is_vip": is_vip,
                            "preview": text[:100],
                        })
                    else:
                        logger.info(f"No keywords matched — skipping action file")
                        _log_action("whatsapp_message_ignored", {
                            "sender": sender,
                            "preview": text[:100],
                        })

    except Exception as e:
        logger.error(f"Error processing webhook: {e}", exc_info=True)

    # Always return 200 to Meta so it doesn't retry
    return jsonify({"status": "ok"}), 200


@app.get("/health")
def health():
    return jsonify({
        "status": "ok",
        "phone_number_id": PHONE_NUMBER_ID or "NOT SET",
        "dry_run": DRY_RUN,
        "verify_token": VERIFY_TOKEN,
    })


# ── Main ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    NEEDS_ACTION.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    if not PHONE_NUMBER_ID:
        logger.warning("WHATSAPP_PHONE_NUMBER_ID not set — see setup instructions in this file")
    if not ACCESS_TOKEN:
        logger.warning("WHATSAPP_ACCESS_TOKEN not set — see setup instructions in this file")

    logger.info(f"WhatsApp Cloud Watcher starting on port {PORT}")
    logger.info(f"Webhook URL: http://localhost:{PORT}/webhook")
    logger.info(f"DRY_RUN={DRY_RUN} | Verify token: {VERIFY_TOKEN}")
    logger.info("Run cloudflared to expose this port publicly for Meta webhooks")

    app.run(host="0.0.0.0", port=PORT, debug=False)
