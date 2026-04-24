"""
Shopify OAuth server.
Runs as a Flask app (in a background thread alongside the scheduler).

Endpoints:
  GET /          → status page (shows whether a token is installed)
  GET /install   → redirects browser to Shopify OAuth consent screen
  GET /callback  → Shopify redirects here after approval; exchanges code for token
"""

import hashlib
import hmac
import json
import logging
import os
import secrets
from pathlib import Path

from flask import Flask, redirect, request, jsonify
from config import settings

logger = logging.getLogger(__name__)

TOKEN_FILE = Path(__file__).parent / ".shopify_token"

app = Flask(__name__)

SCOPES = "read_products,write_products,read_inventory,write_inventory,read_locations"

# In-memory store for pending OAuth states (nonce → True)
_pending_states: dict[str, bool] = {}


# ── Token helpers ────────────────────────────────────────────────────────────

def load_token() -> str | None:
    """Return the Shopify access token from env var or token file."""
    token = os.environ.get("SHOPIFY_ACCESS_TOKEN", "").strip()
    if token:
        return token
    if TOKEN_FILE.exists():
        token = TOKEN_FILE.read_text().strip()
        if token:
            return token
    return None


def save_token(token: str):
    """Persist token to file and inject into current process env."""
    TOKEN_FILE.write_text(token)
    os.environ["SHOPIFY_ACCESS_TOKEN"] = token
    logger.info("Shopify access token saved.")


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    token = load_token()
    if token:
        return jsonify({
            "status": "ready",
            "message": "Shopify token is installed. Sync engine is active.",
            "token_preview": f"{token[:10]}...",
        })
    return jsonify({
        "status": "not_installed",
        "message": "No Shopify token yet. Visit /install to authorize.",
        "install_url": f"{_app_url()}/install",
    }), 200


@app.route("/install")
def install():
    state = secrets.token_hex(16)
    _pending_states[state] = True

    auth_url = (
        f"https://{settings.SHOPIFY_STORE_URL}/admin/oauth/authorize"
        f"?client_id={settings.SHOPIFY_CLIENT_ID}"
        f"&scope={SCOPES}"
        f"&redirect_uri={_app_url()}/callback"
        f"&state={state}"
    )
    logger.info(f"Starting OAuth flow → {auth_url}")
    return redirect(auth_url)


@app.route("/callback")
def callback():
    params = request.args.to_dict()

    # ── State check ──────────────────────────────────────────────
    state = params.get("state")
    if not state or state not in _pending_states:
        logger.warning("OAuth callback: invalid or missing state")
        return "Invalid state parameter.", 403
    _pending_states.pop(state)

    # ── HMAC verification ────────────────────────────────────────
    if not _verify_hmac(params, settings.SHOPIFY_CLIENT_SECRET):
        logger.warning("OAuth callback: HMAC verification failed")
        return "HMAC verification failed.", 403

    # ── Exchange code for token ──────────────────────────────────
    code = params.get("code")
    if not code:
        return "Missing authorization code.", 400

    import requests as req
    try:
        r = req.post(
            f"https://{settings.SHOPIFY_STORE_URL}/admin/oauth/access_token",
            json={
                "client_id":     settings.SHOPIFY_CLIENT_ID,
                "client_secret": settings.SHOPIFY_CLIENT_SECRET,
                "code":          code,
            },
            timeout=15,
        )
        r.raise_for_status()
        token = r.json()["access_token"]
    except Exception as e:
        logger.error(f"Token exchange failed: {e}")
        return f"Token exchange failed: {e}", 500

    save_token(token)
    logger.info(f"OAuth complete. Token: {token[:10]}...")

    return f"""
    <html><body style="font-family:sans-serif;padding:40px">
    <h2>✅ Shopify connected!</h2>
    <p>Access token obtained and active.</p>
    <p><strong>Copy this token into Railway as <code>SHOPIFY_ACCESS_TOKEN</code>
    so it survives redeploys:</strong></p>
    <code style="background:#f0f0f0;padding:10px;display:block;word-break:break-all">{token}</code>
    <p>You can close this tab.</p>
    </body></html>
    """


# ── HMAC helper ──────────────────────────────────────────────────────────────

def _verify_hmac(params: dict, secret: str) -> bool:
    hmac_value = params.pop("hmac", None)
    if not hmac_value:
        return False
    message = "&".join(
        f"{k}={v}" for k, v in sorted(params.items()) if k != "hmac"
    )
    digest = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, hmac_value)


def _app_url() -> str:
    url = os.environ.get("APP_URL", "").rstrip("/")
    if not url:
        port = int(os.environ.get("PORT", 8080))
        url = f"http://localhost:{port}"
    return url
