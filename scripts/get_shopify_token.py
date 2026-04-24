"""
One-time script to complete the Shopify OAuth flow and obtain an access token.

Steps:
  1. Run this script: python scripts/get_shopify_token.py
  2. Your browser opens — approve the app on your Shopify store.
  3. Shopify redirects to localhost; the script captures the code automatically.
  4. The access token is printed and written to your .env file.

Before running:
  - In your Shopify Partner Dashboard → App setup → Allowed redirection URL(s),
    add: http://localhost:8888/callback
"""

import hashlib
import hmac
import os
import sys
import json
import secrets
import webbrowser
import requests
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

# ── load .env manually (avoid importing settings so this script is standalone) ──
from pathlib import Path
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
env = dotenv_values(ROOT / ".env")

CLIENT_ID     = env.get("SHOPIFY_CLIENT_ID")
CLIENT_SECRET = env.get("SHOPIFY_CLIENT_SECRET")
SHOP          = env.get("SHOPIFY_STORE_URL")  # e.g. men-supps.myshopify.com
REDIRECT_URI  = "http://localhost:8888/callback"
SCOPES        = "read_products,write_products,read_inventory,write_inventory,read_locations"
PORT          = 8888

if not all([CLIENT_ID, CLIENT_SECRET, SHOP]):
    print("ERROR: SHOPIFY_CLIENT_ID, SHOPIFY_CLIENT_SECRET, and SHOPIFY_STORE_URL must be set in .env")
    sys.exit(1)

state = secrets.token_hex(16)
auth_url = (
    f"https://{SHOP}/admin/oauth/authorize"
    f"?client_id={CLIENT_ID}"
    f"&scope={SCOPES}"
    f"&redirect_uri={REDIRECT_URI}"
    f"&state={state}"
)

received = {}


class CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return

        params = parse_qs(parsed.query)
        received["code"]  = params.get("code",  [None])[0]
        received["state"] = params.get("state", [None])[0]
        received["hmac"]  = params.get("hmac",  [None])[0]
        received["shop"]  = params.get("shop",  [None])[0]

        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<h2>Authorization received. You can close this tab.</h2>")

    def log_message(self, format, *args):
        pass  # silence request logs


def verify_hmac(params: dict, secret: str) -> bool:
    """Verify Shopify's HMAC signature on the callback."""
    hmac_value = params.pop("hmac", None)
    if not hmac_value:
        return False
    message = "&".join(f"{k}={v[0]}" for k, v in sorted(params.items()))
    digest = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, hmac_value)


def exchange_code_for_token(shop: str, code: str) -> str:
    r = requests.post(
        f"https://{shop}/admin/oauth/access_token",
        json={
            "client_id":     CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "code":          code,
        },
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def write_token_to_env(token: str):
    env_path = ROOT / ".env"
    lines = env_path.read_text().splitlines()
    updated = []
    found = False
    for line in lines:
        if line.startswith("SHOPIFY_ACCESS_TOKEN="):
            updated.append(f"SHOPIFY_ACCESS_TOKEN={token}")
            found = True
        else:
            updated.append(line)
    if not found:
        updated.append(f"SHOPIFY_ACCESS_TOKEN={token}")
    env_path.write_text("\n".join(updated) + "\n")


def main():
    print(f"\nOpening browser for Shopify authorization...")
    print(f"If it doesn't open automatically, visit:\n  {auth_url}\n")
    webbrowser.open(auth_url)

    print(f"Waiting for callback on http://localhost:{PORT}/callback ...")
    server = HTTPServer(("localhost", PORT), CallbackHandler)
    server.handle_request()  # handle exactly one request then stop

    if not received.get("code"):
        print("ERROR: No authorization code received.")
        sys.exit(1)

    if received.get("state") != state:
        print("ERROR: State mismatch — possible CSRF. Aborting.")
        sys.exit(1)

    print("Authorization code received. Exchanging for access token...")
    try:
        token = exchange_code_for_token(received["shop"], received["code"])
    except Exception as e:
        print(f"ERROR: Failed to exchange code: {e}")
        sys.exit(1)

    print(f"\n✓ Access token obtained: {token}")
    write_token_to_env(token)
    print(f"✓ Written to .env as SHOPIFY_ACCESS_TOKEN")
    print(f"\nNext: set SHOPIFY_ACCESS_TOKEN={token} in Railway dashboard, then deploy.")


if __name__ == "__main__":
    main()
