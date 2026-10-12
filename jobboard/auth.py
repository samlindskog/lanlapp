"""Password login, signed session cookies and bearer API tokens for jobboard.

Enabled when JOBBOARD_AUTH=1. Config (env):
  JOBBOARD_PASSWORD_HASH   pbkdf2_sha256$<iters>$<salt>$<hash>   (make one: python3 server.py hash-password)
  JOBBOARD_SECRET          random string used to sign session cookies
Tokens live in the auth_tokens table (hashed); a token is shown once when issued.
"""
import base64
import hashlib
import hmac
import os
import secrets
import time

AUTH_ON = os.environ.get("JOBBOARD_AUTH") == "1"
PASSWORD_HASH = os.environ.get("JOBBOARD_PASSWORD_HASH", "")
SECRET = os.environ.get("JOBBOARD_SECRET", "").encode()
SESSION_DAYS = int(os.environ.get("JOBBOARD_SESSION_DAYS", "30"))
COOKIE = "jobboard_session"

SCHEMA = """
CREATE TABLE IF NOT EXISTS auth_tokens (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    revoked_at TEXT
);
"""


# ----------------------------------------------------------------- passwords
def hash_password(password, iterations=600_000):
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def check_password(password):
    try:
        algo, iters, salt, dk = PASSWORD_HASH.split("$")
        if algo != "pbkdf2_sha256":
            return False
        cand = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt), int(iters))
        return hmac.compare_digest(cand, base64.b64decode(dk))
    except Exception:  # noqa: BLE001
        return False


# ----------------------------------------------------------------- sessions
def _sign(msg):
    return hmac.new(SECRET, msg.encode(), hashlib.sha256).hexdigest()


def new_session_cookie(secure):
    exp = int(time.time()) + SESSION_DAYS * 86400
    nonce = secrets.token_hex(8)
    body = f"{exp}.{nonce}"
    val = f"{body}.{_sign(body)}"
    attrs = f"{COOKIE}={val}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_DAYS * 86400}"
    return attrs + ("; Secure" if secure else "")


def clear_session_cookie():
    return f"{COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"


def session_valid(cookie_header):
    if not cookie_header or not SECRET:
        return False
    for part in cookie_header.split(";"):
        k, _, v = part.strip().partition("=")
        if k != COOKIE:
            continue
        try:
            exp, nonce, sig = v.split(".")
        except ValueError:
            return False
        if not hmac.compare_digest(sig, _sign(f"{exp}.{nonce}")):
            return False
        return int(exp) > time.time()
    return False


# ----------------------------------------------------------------- tokens
def _h(token):
    return hashlib.sha256(token.encode()).hexdigest()


def issue_token(conn, name, now):
    token = "jb_" + secrets.token_urlsafe(32)
    row = conn.execute("INSERT INTO auth_tokens (name, token_hash, created_at) VALUES (%s,%s,%s) RETURNING id",
                       (name, _h(token), now)).fetchone()
    conn.commit()
    return row["id"], token


def token_valid(conn, auth_header, now):
    if not auth_header or not auth_header.lower().startswith("bearer "):
        return None
    tok = auth_header.split(None, 1)[1].strip()
    row = conn.execute("SELECT id, name FROM auth_tokens WHERE token_hash=%s AND revoked_at IS NULL",
                       (_h(tok),)).fetchone()
    if row:
        conn.execute("UPDATE auth_tokens SET last_used_at=%s WHERE id=%s", (now, row["id"]))
        conn.commit()
    return row


def list_tokens(conn):
    return [dict(r) for r in conn.execute(
        "SELECT id, name, created_at, last_used_at, revoked_at FROM auth_tokens ORDER BY id")]


def revoke_token(conn, token_id, now):
    n = conn.execute("UPDATE auth_tokens SET revoked_at=%s WHERE id=%s AND revoked_at IS NULL",
                     (now, token_id)).rowcount
    conn.commit()
    return n
