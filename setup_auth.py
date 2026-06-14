"""
One-time OAuth setup — run this ONCE to get your Robinhood MCP access token.

Usage:
    python setup_auth.py

What it does:
    1. Discovers Robinhood's OAuth server endpoints automatically
    2. Opens your browser to the Robinhood authorization page
    3. Captures the redirect on localhost:8080
    4. Exchanges the auth code for an access token
    5. Saves the token to your .env file

After running this, the trading agent reads ROBINHOOD_MCP_TOKEN from .env
and uses it for all Robinhood MCP calls. Re-run this script if the token
ever expires or is revoked.
"""
from __future__ import annotations

import hashlib
import http.server
import json
import os
import secrets
import threading
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

MCP_URL    = "https://agent.robinhood.com/mcp/trading"
REDIRECT   = "http://localhost:3118/callback"
CLIENT_ID  = "LtLiNmbs9owbYfWgBlC68Z2V-claude"
ENV_FILE   = Path(".env")
TOKEN_KEY  = "ROBINHOOD_MCP_TOKEN"

# ---------------------------------------------------------------------------
# OAuth PKCE helpers
# ---------------------------------------------------------------------------

def _pkce_pair():
    verifier  = secrets.token_urlsafe(64)
    challenge = hashlib.sha256(verifier.encode()).digest()
    import base64
    challenge_b64 = base64.urlsafe_b64encode(challenge).rstrip(b"=").decode()
    return verifier, challenge_b64


def _discover_oauth_endpoints(mcp_url: str) -> dict:
    """
    Fetch OAuth server metadata from the MCP server's well-known endpoint.
    Falls back to Robinhood's known endpoints if discovery fails.
    """
    discovery_url = mcp_url.rstrip("/") + "/.well-known/oauth-authorization-server"
    try:
        with urllib.request.urlopen(discovery_url, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception:
        # Fallback to Robinhood's known OAuth endpoints
        return {
            "authorization_endpoint": "https://robinhood.com/oauth2/authorize/",
            "token_endpoint":         "https://api.robinhood.com/oauth2/token/",
        }


# ---------------------------------------------------------------------------
# Local callback server
# ---------------------------------------------------------------------------

_auth_code: list = []   # mutable container so the handler can write to it


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        code = params.get("code", [None])[0]
        error = params.get("error", [None])[0]

        if code:
            _auth_code.append(code)
            body = b"<h2>Authorization successful! You can close this tab.</h2>"
        else:
            body = f"<h2>Authorization failed: {error}</h2>".encode()

        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # suppress request logs


# ---------------------------------------------------------------------------
# Token exchange
# ---------------------------------------------------------------------------

def _exchange_code(token_endpoint: str, code: str, verifier: str, client_id: str) -> str:
    data = urllib.parse.urlencode({
        "grant_type":    "authorization_code",
        "code":          code,
        "redirect_uri":  REDIRECT,
        "code_verifier": verifier,
        "client_id":     client_id,
    }).encode()

    req = urllib.request.Request(token_endpoint, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")

    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read())

    token = payload.get("access_token") or payload.get("token")
    if not token:
        raise ValueError(f"No access_token in response: {payload}")
    return token


# ---------------------------------------------------------------------------
# .env writer
# ---------------------------------------------------------------------------

def _save_token(token: str):
    lines = []
    updated = False

    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{TOKEN_KEY}="):
                lines.append(f"{TOKEN_KEY}={token}")
                updated = True
            else:
                lines.append(line)

    if not updated:
        lines.append(f"{TOKEN_KEY}={token}")

    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n✓ Token saved to {ENV_FILE}")


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------

def main():
    print("Robinhood MCP — OAuth setup")
    print("=" * 40)

    endpoints  = _discover_oauth_endpoints(MCP_URL)
    auth_ep    = endpoints["authorization_endpoint"]
    token_ep   = endpoints["token_endpoint"]

    # Robinhood's OAuth client_id for Claude / third-party agents
    # This may need to be obtained from Robinhood's developer docs.
    # Check https://robinhood.com/us/en/support/articles/agentic-trading-overview/
    client_id = os.environ.get("ROBINHOOD_CLIENT_ID", CLIENT_ID)

    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(16)

    params = urllib.parse.urlencode({
        "response_type":         "code",
        "client_id":             client_id,
        "redirect_uri":          REDIRECT,
        "scope":                 "trading",
        "state":                 state,
        "code_challenge":        challenge,
        "code_challenge_method": "S256",
    })
    auth_url = f"{auth_ep}?{params}"

    # Start local callback server
    server = http.server.HTTPServer(("localhost", 8080), _CallbackHandler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    print(f"\nOpening browser for Robinhood authorization...")
    print(f"If the browser doesn't open, visit:\n  {auth_url}\n")
    webbrowser.open(auth_url)

    thread.join(timeout=300)

    if not _auth_code:
        print("\n✗ No authorization code received within 5 minutes. Try again.")
        return

    print("Authorization code received. Exchanging for access token...")
    try:
        token = _exchange_code(token_ep, _auth_code[0], verifier, client_id)
    except Exception as e:
        print(f"\n✗ Token exchange failed: {e}")
        print("This may mean Robinhood uses a different OAuth flow.")
        print("Check the support article for manual token instructions.")
        return

    _save_token(token)
    print("\n✓ Setup complete. You can now run the trading agent.")
    print("   Token is stored in .env as ROBINHOOD_MCP_TOKEN")


if __name__ == "__main__":
    main()
