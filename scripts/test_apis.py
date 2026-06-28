#!/usr/bin/env python3
"""
API Health-Check Script — AI Employee Vault
Run: uv run python scripts/test_apis.py
     uv run python scripts/test_apis.py --service odoo
     uv run python scripts/test_apis.py --service facebook
"""

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime

# ── Config ────────────────────────────────────────────────────────────────────

VAULT = Path(os.getenv("VAULT_PATH", Path(__file__).parent.parent))

# Load .env manually (avoid dotenv dependency for a standalone script)
def _load_env():
    env_file = VAULT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val

_load_env()

GRAPH_BASE = "https://graph.facebook.com"
GRAPH_VER  = os.getenv("FACEBOOK_GRAPH_VERSION", "v19.0")

# ── Helpers ───────────────────────────────────────────────────────────────────

PASS = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"
WARN = "\033[93m⚠\033[0m"
INFO = "\033[94m·\033[0m"

results: list[dict] = []

def check(name: str, ok: bool, detail: str = ""):
    icon = PASS if ok else FAIL
    status = "PASS" if ok else "FAIL"
    print(f"  {icon} {name}" + (f": {detail}" if detail else ""))
    results.append({"check": name, "status": status, "detail": detail})
    return ok

def warn(name: str, detail: str = ""):
    print(f"  {WARN} {name}" + (f": {detail}" if detail else ""))
    results.append({"check": name, "status": "WARN", "detail": detail})

def info(msg: str):
    print(f"  {INFO} {msg}")

def http_get(url: str, timeout: int = 8) -> tuple[int, dict | str]:
    """Return (status_code, body_dict_or_str)."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "AIEmployee/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode()
            try:
                return r.status, json.loads(body)
            except json.JSONDecodeError:
                return r.status, body
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, body
    except Exception as e:
        return 0, str(e)

def http_post(url: str, data: dict, timeout: int = 8) -> tuple[int, dict | str]:
    payload = json.dumps(data).encode()
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "AIEmployee/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode()
            try:
                return r.status, json.loads(body)
            except json.JSONDecodeError:
                return r.status, body
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, body
    except Exception as e:
        return 0, str(e)

# ── Service checks ────────────────────────────────────────────────────────────

def check_odoo():
    print("\n[Odoo]")
    url  = os.getenv("ODOO_URL", "http://localhost:8069")
    db   = os.getenv("ODOO_DB", "mycompany")
    user = os.getenv("ODOO_USER", "admin")
    pw   = os.getenv("ODOO_PASS", "admin")

    # 1. Reachability
    # Use web/session/authenticate to test reachability (works as POST)
    code, resp = http_post(
        f"{url}/web/session/authenticate",
        {"jsonrpc": "2.0", "method": "call", "id": 1,
         "params": {"db": db, "login": user, "password": pw}},
    )
    # Any HTTP response (even error) means server is up
    reachable = code != 0
    if not check("Odoo reachable", reachable, f"HTTP {code}"):
        warn("Odoo offline", "Start with: docker run -p 8069:8069 odoo:17")
        return

    # 2. DB list
    _, dbresp = http_post(
        f"{url}/jsonrpc",
        {"jsonrpc": "2.0", "method": "call", "id": 2,
         "params": {"service": "db", "method": "list", "args": []}},
    )
    dbs = dbresp.get("result", []) if isinstance(dbresp, dict) else []
    check("Odoo DB list", db in dbs, f"DBs: {dbs}")

    # 3. Auth
    uid = resp.get("result", {}).get("uid") if isinstance(resp, dict) else None
    name = resp.get("result", {}).get("name", "") if isinstance(resp, dict) else ""
    check("Odoo auth", bool(uid) and isinstance(uid, int),
          f"uid={uid}, name='{name}'" if uid else str(resp)[:80])


def check_gmail():
    print("\n[Gmail]")
    token = os.getenv("GMAIL_ACCESS_TOKEN", "")
    refresh = os.getenv("GMAIL_REFRESH_TOKEN", "")

    if not token:
        warn("GMAIL_ACCESS_TOKEN not set", "Set in .env to test Gmail API")
        return

    # Check token via tokeninfo endpoint
    code, body = http_get(
        f"https://oauth2.googleapis.com/tokeninfo?access_token={token}"
    )
    if isinstance(body, dict):
        if "error" in body:
            # Try to indicate if it's just expired (refresh token exists)
            if refresh:
                warn("Gmail token expired", "Has refresh token → will auto-renew when MCP starts")
            else:
                check("Gmail token valid", False, body.get("error_description", body["error"]))
        else:
            scope = body.get("scope", "")
            exp   = body.get("expires_in", "?")
            check("Gmail token valid", True, f"expires_in={exp}s, scope snippet={scope[:60]}")
    else:
        check("Gmail token valid", code == 200, str(body)[:80])


def check_facebook():
    print("\n[Facebook / Instagram]")
    token   = os.getenv("FACEBOOK_PAGE_ACCESS_TOKEN", "")
    page_id = os.getenv("FACEBOOK_PAGE_ID", "")
    ig_id   = os.getenv("INSTAGRAM_USER_ID", "")

    if not token:
        warn("FACEBOOK_PAGE_ACCESS_TOKEN not set", "Set in .env to test Facebook API")
        return

    # 1. Token debug
    code, body = http_get(
        f"{GRAPH_BASE}/debug_token?input_token={token}&access_token={token}"
    )
    if isinstance(body, dict) and "data" in body:
        d = body["data"]
        valid = d.get("is_valid", False)
        exp   = d.get("expires_at", 0)
        exp_s = datetime.fromtimestamp(exp).isoformat() if exp else "never"
        check("FB token valid", valid, f"expires={exp_s}, type={d.get('type','?')}")
    else:
        check("FB token debug endpoint", code == 200, str(body)[:80])

    # 2. Page info
    if page_id:
        code, body = http_get(
            f"{GRAPH_BASE}/{GRAPH_VER}/{page_id}?fields=id,name,fan_count&access_token={token}"
        )
        if isinstance(body, dict) and "id" in body:
            check("FB page accessible", True, f"name='{body.get('name')}' fans={body.get('fan_count','?')}")
        else:
            check("FB page accessible", False, str(body)[:80])
    else:
        warn("FACEBOOK_PAGE_ID not set")

    # 3. Instagram
    if ig_id:
        code, body = http_get(
            f"{GRAPH_BASE}/{GRAPH_VER}/{ig_id}?fields=id,username,followers_count&access_token={token}"
        )
        if isinstance(body, dict) and "id" in body:
            check("Instagram accessible", True, f"@{body.get('username')} followers={body.get('followers_count','?')}")
        else:
            check("Instagram accessible", False, str(body)[:80])
    else:
        warn("INSTAGRAM_USER_ID not set")


def check_twitter():
    print("\n[Twitter / X]")
    bearer = os.getenv("TWITTER_BEARER_TOKEN", "")

    if not bearer:
        warn("TWITTER_BEARER_TOKEN not set", "Set in .env to test Twitter API")
        return

    code, body = http_get(
        "https://api.twitter.com/2/tweets/search/recent?query=from:twitterdev&max_results=1",
    )
    # Need Authorization header — urllib.request.Request
    req = urllib.request.Request(
        "https://api.twitter.com/2/tweets/search/recent?query=from:twitterdev&max_results=1",
        headers={"Authorization": f"Bearer {bearer}", "User-Agent": "AIEmployee/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            body = json.loads(r.read().decode())
            check("Twitter Bearer token", True,
                  f"search OK, meta={body.get('meta', {})}")
    except urllib.error.HTTPError as e:
        body = json.loads(e.read().decode())
        err = body.get("errors", [{}])[0].get("message", str(body))[:80]
        check("Twitter Bearer token", e.code == 200, f"HTTP {e.code}: {err}")
    except Exception as e:
        check("Twitter Bearer token", False, str(e)[:80])


def check_linkedin():
    print("\n[LinkedIn]")
    token = os.getenv("LINKEDIN_ACCESS_TOKEN", "")

    if not token:
        warn("LINKEDIN_ACCESS_TOKEN not set", "Set in .env to test LinkedIn API")
        return

    req = urllib.request.Request(
        "https://api.linkedin.com/v2/userinfo",
        headers={"Authorization": f"Bearer {token}", "User-Agent": "AIEmployee/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            body = json.loads(r.read().decode())
            check("LinkedIn token", True,
                  f"sub={body.get('sub','?')}, name='{body.get('name','?')}'")
    except urllib.error.HTTPError as e:
        body = json.loads(e.read().decode())
        check("LinkedIn token", False, f"HTTP {e.code}: {body}")
    except Exception as e:
        check("LinkedIn token", False, str(e)[:80])


def check_youtube():
    print("\n[YouTube]")
    token = os.getenv("YOUTUBE_ACCESS_TOKEN", "")
    refresh = os.getenv("YOUTUBE_REFRESH_TOKEN", "")

    if not token:
        warn("YOUTUBE_ACCESS_TOKEN not set", "Set in .env to test YouTube API")
        return

    code, body = http_get(
        f"https://oauth2.googleapis.com/tokeninfo?access_token={token}"
    )
    if isinstance(body, dict):
        if "error" in body:
            if refresh:
                warn("YouTube token expired", "Has refresh token → will auto-renew when MCP starts")
            else:
                check("YouTube token valid", False, body.get("error_description", body["error"]))
        else:
            check("YouTube token valid", True, f"expires_in={body.get('expires_in','?')}s")
    else:
        check("YouTube token valid", code == 200, str(body)[:80])


def check_pm2():
    print("\n[pm2 Processes]")
    import subprocess
    try:
        out = subprocess.check_output(["pm2", "jlist"], text=True, timeout=5)
        apps = json.loads(out)
        online  = [a["name"] for a in apps if a["pm2_env"]["status"] == "online"]
        errored = [a["name"] for a in apps if a["pm2_env"]["status"] == "errored"]
        stopped = [a["name"] for a in apps if a["pm2_env"]["status"] == "stopped"]
        check("MCP servers online", all(
            any(a.startswith("mcp-") for a in online) for _ in [1]
        ), f"{[n for n in online if n.startswith('mcp-')]}")
        check("Python watchers online", all(
            any(a.startswith("watcher-") for a in online) for _ in [1]
        ), f"{[n for n in online if n.startswith('watcher-')]}")
        if errored:
            # weekly-audit erroring is OK (run-once job)
            real_errors = [n for n in errored if n != "weekly-audit"]
            if real_errors:
                check("No unexpected errors", False, str(real_errors))
            else:
                warn("weekly-audit errored (expected)", "run-once job exits after completion")
    except Exception as e:
        warn("pm2 check skipped", str(e))


def check_vault():
    print("\n[Vault Structure]")
    required = ["Inbox", "Needs_Action", "Plans", "Pending_Approval",
                "Approved", "Rejected", "Done", "Logs", "Accounting",
                "Briefings", "Active_Project", "Skills"]
    missing = [d for d in required if not (VAULT / d).is_dir()]
    check("All vault folders present", not missing,
          f"missing: {missing}" if missing else f"{len(required)} folders OK")

    key_files = ["Dashboard.md", "Company_Handbook.md", "CLAUDE.md",
                 ".mcp.json", "ecosystem.config.cjs"]
    missing_f = [f for f in key_files if not (VAULT / f).exists()]
    check("Key files present", not missing_f,
          f"missing: {missing_f}" if missing_f else f"{len(key_files)} files OK")

    # MCP servers
    mcp_dirs = ["gmail-send", "linkedin-post", "odoo-accounting",
                "social-post", "twitter", "youtube", "facebook-mcp"]
    missing_mcp = [d for d in mcp_dirs if not (VAULT / "mcp-servers" / d).exists()]
    check("MCP server dirs present", not missing_mcp,
          f"missing: {missing_mcp}" if missing_mcp else f"{len(mcp_dirs)} MCP dirs OK")


# ── Main ──────────────────────────────────────────────────────────────────────

SERVICES = {
    "odoo":      check_odoo,
    "gmail":     check_gmail,
    "facebook":  check_facebook,
    "twitter":   check_twitter,
    "linkedin":  check_linkedin,
    "youtube":   check_youtube,
    "pm2":       check_pm2,
    "vault":     check_vault,
}

def main():
    parser = argparse.ArgumentParser(description="AI Employee Vault — API health checks")
    parser.add_argument("--service", "-s",
                        choices=list(SERVICES.keys()) + ["all"],
                        default="all",
                        help="Which service to test (default: all)")
    parser.add_argument("--json", action="store_true",
                        help="Output results as JSON")
    args = parser.parse_args()

    print(f"\n{'='*55}")
    print(f"  AI Employee Vault — API Health Check")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*55}")

    to_run = list(SERVICES.values()) if args.service == "all" else [SERVICES[args.service]]
    for fn in to_run:
        fn()

    # Summary
    passed = sum(1 for r in results if r["status"] == "PASS")
    failed = sum(1 for r in results if r["status"] == "FAIL")
    warned = sum(1 for r in results if r["status"] == "WARN")

    print(f"\n{'='*55}")
    print(f"  Results: {PASS} {passed} passed  {FAIL} {failed} failed  {WARN} {warned} warnings")
    print(f"{'='*55}\n")

    if args.json:
        print(json.dumps(results, indent=2))

    sys.exit(1 if failed else 0)

if __name__ == "__main__":
    main()
