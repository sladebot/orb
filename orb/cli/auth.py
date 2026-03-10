from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Event, Thread

import httpx

# ── OAuth constants (OpenAI Codex CLI — same as pi-ai / openclaw) ─────────────
_AUTH_URL     = "https://auth.openai.com/oauth/authorize"
_TOKEN_URL    = "https://auth.openai.com/oauth/token"
_CLIENT_ID    = "app_EMoamEEZ73f0CkXaXp7hrann"
_REDIRECT_URI = "http://localhost:1455/auth/callback"
_SCOPE        = "openid profile email offline_access"

CREDS_PATH = Path.home() / ".orb" / "credentials.json"


# ── PKCE ──────────────────────────────────────────────────────────────────────

def _pkce_pair() -> tuple[str, str]:
    verifier  = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    digest    = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def _build_auth_url(code_challenge: str, state: str) -> str:
    params = {
        "client_id":                  _CLIENT_ID,
        "response_type":              "code",
        "redirect_uri":               _REDIRECT_URI,
        "scope":                      _SCOPE,
        "code_challenge":             code_challenge,
        "code_challenge_method":      "S256",
        "state":                      state,
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow":  "true",
    }
    return _AUTH_URL + "?" + urllib.parse.urlencode(params)


# ── TLS preflight ─────────────────────────────────────────────────────────────

def _tls_preflight() -> str | None:
    """Return an error string if OpenAI auth TLS is broken, else None."""
    try:
        httpx.get(_AUTH_URL, timeout=5, follow_redirects=False)
        return None
    except httpx.ConnectError as e:
        msg = str(e).lower()
        tls_keywords = (
            "certificate", "cert", "ssl", "tls",
            "unable to verify", "self-signed", "expired",
        )
        if any(k in msg for k in tls_keywords):
            return f"TLS error connecting to OpenAI auth: {e}"
        return None  # non-TLS connect error is fine (redirect expected)
    except Exception:
        return None


# ── Token exchange / refresh ──────────────────────────────────────────────────

def _exchange_code(code: str, verifier: str) -> dict:
    resp = httpx.post(
        _TOKEN_URL,
        data={                          # form-encoded, not JSON
            "grant_type":    "authorization_code",
            "client_id":     _CLIENT_ID,
            "code":          code,
            "redirect_uri":  _REDIRECT_URI,
            "code_verifier": verifier,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _decode_jwt_payload(token: str) -> dict:
    """Decode JWT payload without verifying signature (info only)."""
    try:
        payload_b64 = token.split(".")[1]
        # Add padding
        payload_b64 += "=" * (-len(payload_b64) % 4)
        return json.loads(base64.urlsafe_b64decode(payload_b64))
    except Exception:
        return {}


def refresh_openai_token(creds: dict) -> dict | None:
    """Refresh using stored refresh token. Returns updated creds or None."""
    refresh_token = creds.get("refresh_token")
    if not refresh_token:
        # API-key style credentials don't expire
        return creds if creds.get("api_key") else None
    try:
        resp = httpx.post(
            _TOKEN_URL,
            data={
                "grant_type":    "refresh_token",
                "client_id":     _CLIENT_ID,
                "refresh_token": refresh_token,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        updated = dict(creds)
        updated["access_token"]  = data["access_token"]
        updated["expires_at"]    = int(time.time()) + int(data.get("expires_in", 3600))
        if "refresh_token" in data:
            updated["refresh_token"] = data["refresh_token"]
        _save_credentials("openai", updated)
        return updated
    except Exception:
        return None


# ── Credential store ──────────────────────────────────────────────────────────

def _save_credentials(provider: str, data: dict) -> None:
    CREDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    existing: dict = {}
    if CREDS_PATH.exists():
        try:
            existing = json.loads(CREDS_PATH.read_text())
        except Exception:
            pass
    existing[provider] = data
    CREDS_PATH.write_text(json.dumps(existing, indent=2))
    CREDS_PATH.chmod(0o600)


def load_credentials(provider: str) -> dict | None:
    if not CREDS_PATH.exists():
        return None
    try:
        return json.loads(CREDS_PATH.read_text()).get(provider)
    except Exception:
        return None


def get_openai_token() -> str | None:
    """Return a valid OpenAI token, refreshing if needed. Falls back to API key."""
    creds = load_credentials("openai")
    if not creds:
        return None
    # Stored API key (non-OAuth)
    if creds.get("api_key"):
        return creds["api_key"]
    # OAuth token — still valid?
    if creds.get("expires_at", 0) > time.time() + 60:
        return creds.get("access_token")
    # Refresh
    refreshed = refresh_openai_token(creds)
    return refreshed.get("access_token") if refreshed else None


def revoke_openai_token() -> None:
    existing: dict = {}
    if CREDS_PATH.exists():
        try:
            existing = json.loads(CREDS_PATH.read_text())
        except Exception:
            pass
    existing.pop("openai", None)
    if existing:
        CREDS_PATH.write_text(json.dumps(existing, indent=2))
    else:
        CREDS_PATH.unlink(missing_ok=True)


# ── Auth flow ─────────────────────────────────────────────────────────────────

def _is_remote() -> bool:
    # Remote if SSH env vars are set, or if there's no display available
    if os.environ.get("SSH_CLIENT") or os.environ.get("SSH_TTY"):
        return True
    # No DISPLAY on Linux = headless/server
    if os.name != "nt" and not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        return True
    return False


async def auth_openai() -> None:
    """Authenticate with OpenAI using the Codex CLI OAuth flow."""
    # TLS preflight
    tls_err = _tls_preflight()
    if tls_err:
        print(f"\nTLS preflight failed: {tls_err}")
        print("Check your system's CA certificates and try again.")
        return

    verifier, challenge = _pkce_pair()
    state    = secrets.token_urlsafe(16)
    auth_url = _build_auth_url(challenge, state)

    remote = _is_remote()

    print(f"\nOpen this URL in your {'local ' if remote else ''}browser:\n\n  {auth_url}\n")

    if remote:
        print(
            "After you approve access your browser will redirect to\n"
            f"  {_REDIRECT_URI}?code=...\n"
            "That page won't load (expected — server is remote).\n"
            "Copy the full URL from your browser's address bar and paste it below.\n"
        )
        try:
            redirect_url = input("Paste redirect URL: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            return

        parsed    = urllib.parse.urlparse(redirect_url)
        qs        = urllib.parse.parse_qs(parsed.query)
        error     = qs.get("error", [""])[0]
        code      = qs.get("code",  [""])[0]
        got_state = qs.get("state", [""])[0]
    else:
        # Local: start callback server on port 1455
        code_event = Event()
        received: dict = {}

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                p  = urllib.parse.urlparse(self.path)
                qs = urllib.parse.parse_qs(p.query)
                received["code"]  = qs.get("code",  [""])[0]
                received["state"] = qs.get("state", [""])[0]
                received["error"] = qs.get("error", [""])[0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                body = (b"<h2>Authenticated! You can close this tab.</h2>"
                        if not received["error"]
                        else f"<h2>Error: {received['error']}</h2>".encode())
                self.wfile.write(body)
                code_event.set()
            def log_message(self, *args): pass

        server = HTTPServer(("127.0.0.1", 1455), _Handler)
        Thread(target=server.serve_forever, daemon=True).start()
        print("  Waiting for browser callback on port 1455…")

        if not code_event.wait(timeout=120):
            server.shutdown()
            print("\nTimed out (120 s). Aborted.")
            return
        server.shutdown()

        error     = received.get("error", "")
        code      = received.get("code", "")
        got_state = received.get("state", "")

    # Validate
    if error:
        print(f"\nOAuth error: {error}")
        return
    if not code:
        print("\nNo authorization code received.")
        return
    if got_state != state:
        print("\nState mismatch — possible CSRF. Aborted.")
        return

    # Exchange code for tokens
    print("Exchanging code for tokens…")
    try:
        tokens = _exchange_code(code, verifier)
    except Exception as exc:
        print(f"\nToken exchange failed: {exc}")
        return

    access_token = tokens.get("access_token", "")
    payload      = _decode_jwt_payload(access_token)
    email        = payload.get("email") or tokens.get("email", "")

    creds = {
        "access_token":  access_token,
        "refresh_token": tokens.get("refresh_token"),
        "expires_at":    int(time.time()) + int(tokens.get("expires_in", 3600)),
        "email":         email,
    }
    _save_credentials("openai", creds)

    who = f" as {email}" if email else ""
    print(f"\nAuthenticated{who}! Credentials stored at {CREDS_PATH}")


def save_anthropic_key(api_key: str) -> None:
    """Store an Anthropic API key in the credentials file."""
    _save_credentials("anthropic", {"api_key": api_key})


def get_anthropic_key() -> str | None:
    """Return stored Anthropic API key, or None if not stored."""
    creds = load_credentials("anthropic")
    return creds.get("api_key") if creds else None


def revoke_anthropic_key() -> None:
    """Remove stored Anthropic credentials."""
    existing: dict = {}
    if CREDS_PATH.exists():
        try:
            existing = json.loads(CREDS_PATH.read_text())
        except Exception:
            pass
    existing.pop("anthropic", None)
    if existing:
        CREDS_PATH.write_text(json.dumps(existing, indent=2))
    else:
        CREDS_PATH.unlink(missing_ok=True)


async def auth_status() -> None:
    """Print current auth status for all providers."""
    # OpenAI
    creds = load_credentials("openai")
    if creds and creds.get("api_key"):
        k = creds["api_key"]
        print(f"  openai     API key stored  (****{k[-4:]})")
    elif creds and creds.get("access_token"):
        exp       = creds.get("expires_at", 0)
        remaining = exp - time.time()
        email     = creds.get("email", "")
        who       = f"  {email}" if email else ""
        if remaining > 60:
            print(f"  openai     OAuth token{who}  (expires in {int(remaining // 60)}m)")
        else:
            print(f"  openai     OAuth token expired{who}  — run: orb auth openai")
    elif os.environ.get("OPENAI_API_KEY"):
        k = os.environ["OPENAI_API_KEY"]
        print(f"  openai     OPENAI_API_KEY env var  (****{k[-4:]})")
    else:
        print("  openai     not authenticated  (run: orb auth openai)")

    # Anthropic — check stored key first, then env var
    stored_ant = get_anthropic_key()
    env_ant    = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_OAUTH_TOKEN")
    if stored_ant:
        print(f"  anthropic  API key stored  (****{stored_ant[-4:]})")
    elif env_ant:
        print(f"  anthropic  ANTHROPIC_API_KEY env var  (****{env_ant[-4:]})")
    else:
        print("  anthropic  not authenticated  (run: orb auth anthropic --api-key sk-ant-...)")
