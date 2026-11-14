"""Coinpile: a simple, friendly view of your Firefly III, with an agent you can ask about your money.

Sign in with your Firefly III account (its "Sign in with Firefly III" OAuth page, or a Personal Access Token). Each Firefly III user gets their own
local copy and their own Coinpile data (goals, notes, chats). Firefly III stays the source of truth: Coinpile reads it every few minutes.
The Plaid connector, if you use one, can be driven from here too (see your banks, pull now, link a bank)."""
import base64
import hmac
import hashlib
import json
import os
import re
import secrets
import threading
import time
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from coinpile import agent, api_links, api_money, ffwrite, importers, investments, mcp, netguard, robinhood, webhooks, api_plan, connector as conn, links, external, feed, firefly, ledger, notify, plan, settings as cfg, sync
from coinpile.db import DB
from coinpile.oplog import log
from coinpile.security import Throttle, check_password_strength, hash_password, verify_password

SESSION_DAYS = 30
COOKIE = "coinpile"
USER_KEYS = {"share", "rh_main_source", "rh_mcp_url", "refresh_minutes", "ff_webhook_secret", "ff_send_goals", "ff_send_piggies", "ff_send_bills", "ff_send_categories", "ff_send_accounts", "connector_url", "connector_name", "connector_token", "caldav_url", "caldav_user", "caldav_password",
             "notify_url", "notify_min_severity", "alert_missed", "alert_untracked", "alert_maturity_days", "agent_provider"}


def ui_dir() -> Path:
    for p in (os.environ.get("UI_DIR", ""), "/app/ui", str(Path(__file__).resolve().parents[2] / "ui")):
        if p and (Path(p) / "index.html").exists():
            return Path(p)
    raise RuntimeError("the ui folder was not found (set UI_DIR)")


def ensure_field_key(data_dir: str) -> None:
    """Secrets (Firefly III tokens, model keys) are encrypted with FIELD_KEY. If none was given, make one and keep it next to the data."""
    if os.environ.get("FIELD_KEY"):
        return
    f = Path(data_dir) / ".field_key"
    if not f.exists():
        f.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)          # owner-only from the first byte
        with os.fdopen(fd, "w") as fh:
            fh.write(Fernet.generate_key().decode())
    os.environ["FIELD_KEY"] = f.read_text().strip()


def sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


class Users:
    """Who has signed in: their Firefly III user id, email and (encrypted) token, plus browser sessions (only hashes are kept)."""

    def __init__(self, path: str):
        self.db = DB.__new__(DB)
        import sqlite3
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.db.conn = sqlite3.connect(path, check_same_thread=False)
        try:
            os.chmod(path, 0o600)                            # password hashes and encrypted tokens: readable by Coinpile only
        except OSError:
            pass
        self.db.conn.row_factory = sqlite3.Row
        self.db.lock = threading.RLock()
        with self.db.tx() as c:
            c.executescript("""CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY, email TEXT, kind TEXT, token_enc TEXT, refresh_enc TEXT, expires REAL, created TEXT, last_seen TEXT, problem TEXT);
                               CREATE TABLE IF NOT EXISTS sessions(hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, expires REAL NOT NULL);""")
            cols = [r[1] for r in c.execute("PRAGMA table_info(users)")]
            if "handle" not in cols:
                c.execute("ALTER TABLE users ADD COLUMN handle TEXT")
            if "pw_hash" not in cols:
                c.execute("ALTER TABLE users ADD COLUMN pw_hash TEXT")       # a Coinpile-only password, as a salted scrypt hash (never the password)

    def save(self, uid: str, email: str, kind: str, token: str, refresh: str | None = None, expires: float | None = None) -> None:
        from coinpile import crypto
        with self.db.tx() as c:
            c.execute("""INSERT INTO users(id,email,kind,token_enc,refresh_enc,expires,created,last_seen,problem) VALUES(?,?,?,?,?,?,datetime('now'),datetime('now'),NULL)
                         ON CONFLICT(id) DO UPDATE SET email=excluded.email,kind=excluded.kind,token_enc=excluded.token_enc,refresh_enc=excluded.refresh_enc,
                         expires=excluded.expires,last_seen=excluded.last_seen,problem=NULL""",
                      (uid, email, kind, crypto.encrypt(token), crypto.encrypt(refresh) if refresh else None, expires))

    def ensure_handle(self, uid: str) -> str:
        """The name other people see and link to: the start of the Firefly III email, made unique."""
        u = self.get(uid) or {}
        if u.get("handle"):
            return u["handle"]
        base = re.sub(r"[^a-z0-9_-]", "", (u.get("email") or "").split("@")[0].lower())[:24] or "person"
        if not base[0].isalnum():
            base = "p" + base
        h, taken = base, {r["handle"] for r in self.db.all("SELECT handle FROM users WHERE handle IS NOT NULL AND id<>?", (uid,))}
        if h in taken:
            h = f"{base[:20]}-{re.sub(r'[^a-z0-9]', '', uid.lower())[:8] or secrets.token_hex(2)}"
        with self.db.tx() as c:
            c.execute("UPDATE users SET handle=? WHERE id=?", (h, uid))
        return h

    def by_handle(self, handle: str) -> str | None:
        r = self.db.one("SELECT id FROM users WHERE handle=? AND token_enc IS NOT NULL", (handle,))
        return r["id"] if r else None

    def handles(self) -> list[str]:
        return [r["handle"] for r in self.db.all("SELECT handle FROM users WHERE handle IS NOT NULL AND token_enc IS NOT NULL ORDER BY handle")]

    def get(self, uid: str) -> dict | None:
        return self.db.one("SELECT * FROM users WHERE id=?", (uid,))

    def all(self) -> list[dict]:
        return self.db.all("SELECT * FROM users WHERE token_enc IS NOT NULL")

    def problem(self, uid: str, text: str | None) -> None:
        with self.db.tx() as c:
            c.execute("UPDATE users SET problem=? WHERE id=?", (text, uid))

    def forget(self, uid: str) -> None:
        """Everything about the person goes, except a tombstone row that keeps their handle: a handle is never given to someone else,
        so nobody can sign up later as 'alice' and inherit alice's links."""
        with self.db.tx() as c:
            c.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
            c.execute("UPDATE users SET email=NULL, kind=NULL, token_enc=NULL, refresh_enc=NULL, expires=NULL, problem=NULL, pw_hash=NULL WHERE id=?", (uid,))

    def new_session(self, uid: str) -> str:
        tok = secrets.token_urlsafe(32)
        with self.db.tx() as c:
            c.execute("DELETE FROM sessions WHERE expires<?", (time.time(),))
            c.execute("INSERT INTO sessions VALUES(?,?,?)", (sha(tok), uid, time.time() + SESSION_DAYS * 86400))
        return tok

    def session_user(self, tok: str | None) -> str | None:
        if not tok:
            return None
        r = self.db.one("SELECT user_id FROM sessions WHERE hash=? AND expires>?", (sha(tok), time.time()))
        return r["user_id"] if r else None

    def set_password(self, uid: str, pw_hash: str | None, keep_session: str | None = None) -> None:
        """A new (or removed) password signs out every other browser of this person."""
        with self.db.tx() as c:
            c.execute("UPDATE users SET pw_hash=? WHERE id=?", (pw_hash, uid))
            c.execute("DELETE FROM sessions WHERE user_id=? AND hash<>?", (uid, sha(keep_session) if keep_session else ""))

    def by_email(self, email: str) -> dict | None:
        return self.db.one("SELECT * FROM users WHERE lower(email)=? AND token_enc IS NOT NULL", ((email or "").strip().lower(),))

    def end_session(self, tok: str | None) -> None:
        if tok:
            with self.db.tx() as c:
                c.execute("DELETE FROM sessions WHERE hash=?", (sha(tok),))


def cookie_of(scope) -> str | None:
    for k, v in scope.get("headers", []):
        if k == b"cookie":
            for part in v.decode("latin-1").split(";"):
                name, _, val = part.strip().partition("=")
                if name == COOKIE:
                    return val
    return None


class Dispatch:
    """Everything under /api/ (and the calendar file) belongs to the signed-in person: it is handed to that person's own app, which only sees their database."""

    def __init__(self, front, users: Users, person_app, public_url: str = ""):
        self.front, self.users, self.person_app, self.public_url = front, users, person_app, public_url

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.front(scope, receive, send)
        send = with_headers(send)
        size = next((int(v) for k, v in scope.get("headers", []) if k == b"content-length" and v.isdigit()), None)
        if size is not None and size > MAX_BODY:
            return await JSONResponse({"detail": "too large"}, 413)(scope, receive, send)
        started = False

        async def tracked(msg):
            nonlocal started
            started = started or msg["type"] == "http.response.start"
            await send(msg)
        try:
            return await self.route(scope, capped(receive), tracked)
        except TooLarge:
            if not started:
                return await JSONResponse({"detail": "too large"}, 413)(scope, receive, send)

    async def route(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] == "http" and (path.startswith("/api/") and not path.startswith("/api/v1/") or path == "/subscriptions.ics"):
            uid = self.users.session_user(cookie_of(scope))
            if not uid:
                return await JSONResponse({"detail": "sign in first"}, 401)(scope, receive, send)
            if scope["method"] not in ("GET", "HEAD") and not same_origin(scope, self.public_url):
                return await JSONResponse({"detail": "that request did not come from this site"}, 403)(scope, receive, send)
            return await self.person_app(uid)(scope, receive, send)
        return await self.front(scope, receive, send)


MAX_BODY = 6_000_000          # the largest CSV upload plus room; nothing else comes close


class TooLarge(HTTPException):
    def __init__(self):
        super().__init__(413, "too large")


def capped(receive):
    """Stops reading a body (also one sent without a Content-Length) once it passes MAX_BODY, instead of holding it all in memory."""
    seen = 0

    async def r():
        nonlocal seen
        msg = await receive()
        if msg["type"] == "http.request":
            seen += len(msg.get("body", b""))
            if seen > MAX_BODY:
                raise TooLarge()
        return msg
    return r


SECURITY_HEADERS = [(b"x-frame-options", b"DENY"), (b"content-security-policy", b"frame-ancestors 'none'"), (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"same-origin")]


def with_headers(send):
    async def s(msg):
        if msg["type"] == "http.response.start":
            msg = {**msg, "headers": list(msg.get("headers", [])) + SECURITY_HEADERS}
        await send(msg)
    return s


def same_origin(scope, public_url: str = "") -> bool:
    """Changes must come from Coinpile's own page (the cookie is SameSite=Lax as well; this also stops other sites' forms).

    Host and scheme must both match. The scheme of this request is known when a proxy says it (X-Forwarded-Proto), when COINPILE_PUBLIC_URL names
    this host, or when the connection itself is https; a plain-http connection may be a TLS proxy that said nothing, so then either is accepted."""
    h = {k.decode().lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
    origin = h.get("origin")
    if not origin:
        return True                                   # not a browser cross-site request (curl, tests): those carry no cookie of ours anyway unless sent on purpose
    o_scheme, sep, o_host = origin.partition("://")
    if not sep:
        return False                                  # "null" (sandboxed frames, file:) and other oddities
    o_scheme, o_host = o_scheme.lower(), o_host.rstrip("/").lower()
    host = (h.get("x-forwarded-host") or h.get("host", "")).split(",")[0].strip().lower()
    if o_host != host:
        return False
    pub_scheme, _, pub_host = (public_url or "").lower().partition("://")
    known = (h.get("x-forwarded-proto", "").split(",")[0].strip().lower()
             or (pub_scheme if pub_host.split("/")[0] == host else "")
             or ("https" if scope.get("scheme") == "https" else ""))
    return o_scheme == known if known else o_scheme in ("http", "https")


def create_app(firefly_url: str | None = None, data_dir: str | None = None, firefly_transport=None, connector_transport=None, llm_transport=None,
               caldav_transport=None, notify_transport=None, today=None, background: bool = False, client_id: str | None = None,
               public_firefly_url: str | None = None, public_url: str | None = None, robinhood_transport=None, webhook_transport=None, mcp_transport=None):
    ff_url = (firefly_url or os.environ.get("FIREFLY_URL", "")).rstrip("/")
    ff_public = (public_firefly_url or os.environ.get("FIREFLY_PUBLIC_URL", "") or ff_url).rstrip("/")
    client_id = client_id or os.environ.get("FIREFLY_CLIENT_ID", "")
    my_url = (public_url or os.environ.get("COINPILE_PUBLIC_URL", "")).rstrip("/")
    data_dir = data_dir or os.environ.get("DATA_DIR", "/data")
    ensure_field_key(data_dir)
    today = today or date.today
    users = Users(os.path.join(data_dir, "users.db"))
    apps: dict[str, FastAPI] = {}
    dbs: dict[str, DB] = {}
    locks: dict[str, threading.Lock] = {}
    pending_oauth: dict[str, tuple[str, float]] = {}
    oauth_lock = threading.Lock()
    rh_pending: dict[str, tuple] = {}
    ui = ui_dir()
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(_):
        log.info("coinpile started: Firefly III %s, sign in with Firefly III %s, %d people", "set" if ff_url else "NOT set", "on" if client_id else "off", len(users.all()))
        if background:
            threading.Thread(target=loop, daemon=True).start()
        yield

    front = FastAPI(title="Coinpile", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    http = httpx.Client(timeout=30, transport=firefly_transport)

    def safe_id(uid: str) -> str:
        return re.sub(r"[^A-Za-z0-9-]", "-", uid)

    flushers: dict = {}

    def flush_for(other: str) -> None:
        try:
            person_app(other)                     # builds their side if nobody opened it since the start
            flushers[other]()
        except Exception:
            pass

    made = threading.RLock()                 # one database object (one connection, one lock) per person, however many threads ask at once

    class Forgotten(KeyError):
        """The person used Forget me: nothing may bring their data back (a late background refresh, a link message, a webhook)."""

    def gone(uid: str) -> bool:
        u = users.get(uid)
        return not u or not u["token_enc"]

    def person_db(uid: str) -> DB:
        with made:
            if uid not in dbs:
                if gone(uid):
                    raise Forgotten(uid)
                dbs[uid] = DB(os.path.join(data_dir, "people", f"{safe_id(uid)}.db"))
            return dbs[uid]

    # ---------------- Firefly III tokens
    renewing: dict[str, threading.Lock] = {}

    def token_for(uid: str) -> str:
        from coinpile import crypto
        with renewing.setdefault(uid, threading.Lock()):            # Firefly III rotates refresh tokens: two renewals at once would sign the person out
            return _token_for(uid, crypto)

    def _token_for(uid: str, crypto) -> str:
        u = users.get(uid)
        if not u or not u["token_enc"]:
            raise firefly.TokenRefused("sign in again")
        if u["kind"] == "oauth" and u["expires"] and u["expires"] - time.time() < 120 and u["refresh_enc"]:
            try:
                r = http.post(f"{ff_url}/oauth/token", data={"grant_type": "refresh_token", "refresh_token": crypto.decrypt(u["refresh_enc"]), "client_id": client_id})
            except httpx.HTTPError:
                raise firefly.FireflyError("could not reach Firefly III to renew the sign-in")
            if r.status_code != 200:
                users.problem(uid, "signed out by Firefly III")
                raise firefly.TokenRefused("Firefly III ended the sign-in. Sign in again.")
            d = r.json()
            users.save(uid, u["email"], "oauth", d["access_token"], d.get("refresh_token") or crypto.decrypt(u["refresh_enc"]), time.time() + int(d.get("expires_in", 3600)))
            return d["access_token"]
        return crypto.decrypt(u["token_enc"])

    def ff_for(uid: str) -> firefly.Firefly:
        return firefly.Firefly(ff_url, token_for(uid), firefly_transport)

    def refresh(uid: str, full: bool = False) -> dict:
        lock = locks.setdefault(uid, threading.Lock())
        if not lock.acquire(timeout=1):
            return {"busy": True}
        try:
            if gone(uid):
                return {"forgotten": True}
            db = person_db(uid)
        except Forgotten:
            lock.release()
            return {"forgotten": True}
        except BaseException:
            lock.release()
            raise
        try:
            out = firefly.refresh(db, ff_for(uid), today(), full)
            try:
                person_app(uid)                                     # makes sure this person's Robinhood reader exists
                rh_ = robinhood.Robinhood(db, os.path.join(data_dir, "people", f"{safe_id(uid)}-robinhood"), robinhood_transport, (users.get(uid) or {}).get("email", ""), mcp_transport)
                out["robinhood"] = rh_.snapshot(cfg.get(db, "rh_main_source") or "auto")
            except Exception:
                pass
            cfg.set_value(db, "ff_last_error", "")
            if links.active_peers(db):
                flush_for(uid)                    # balances of accounts shared with someone go to them
            users.problem(uid, None)
            try:
                if cfg.get(db, "notify_url"):
                    out["notify"] = notify.run(db, today(), (users.get(uid) or {}).get("email", ""), notify_transport)
            except Exception:
                pass
            send_hooks(uid, out)
            return out
        except firefly.TokenRefused as e:
            cfg.set_value(db, "ff_last_error", str(e))
            users.problem(uid, "sign in again")
            raise
        except firefly.FireflyError as e:
            cfg.set_value(db, "ff_last_error", str(e))
            raise
        finally:
            lock.release()

    def refresh_soon(uid: str, full: bool = False) -> None:
        def go():
            try:
                refresh(uid, full)
            except firefly.FireflyError:
                pass
        threading.Thread(target=go, daemon=True).start()

    def due(uid: str) -> bool:
        db = person_db(uid)
        last = cfg.get(db, "ff_last_refresh")
        try:
            mins = float(cfg.get(db, "refresh_minutes") or 10)
        except ValueError:
            mins = 10.0
        if mins <= 0:
            return False
        if not last:
            return True
        from datetime import datetime
        return (time.time() - datetime.fromisoformat(last).timestamp()) >= mins * 60

    def scan_drop_folder(uid: str) -> int:
        """PayPal, Venmo and Cash App CSVs put in DATA_DIR/people/<id>-imports/ are imported into Coinpile accounts, then moved to done/."""
        folder = os.path.join(data_dir, "people", f"{safe_id(uid)}-imports")
        if not os.path.isdir(folder):
            return 0
        db, n = person_db(uid), 0
        done = os.path.join(folder, "done")
        for f in sorted(os.listdir(folder)):
            path = os.path.join(folder, f)
            if not (f.lower().endswith(".csv") and os.path.isfile(path)) or os.path.getsize(path) > 5_000_000:
                continue
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    prov, rows = importers.parse(fh.read())
                n += importers.store(db, prov, rows)
                os.makedirs(done, exist_ok=True)
                os.replace(path, os.path.join(done, f))
            except Exception:                              # any file that cannot be read is set aside, so it never blocks the folder
                os.makedirs(os.path.join(folder, "failed"), exist_ok=True)
                os.replace(path, os.path.join(folder, "failed", f))
        return n

    def send_hooks(uid: str, refreshed: dict | None = None) -> None:
        """Queue what changed for this person's webhooks and send what is due. Never breaks the caller."""
        try:
            db = person_db(uid)
            webhooks.collect(db, users.ensure_handle(uid), today(), refreshed)
            webhooks.deliver(db, webhook_transport)
        except Exception:
            pass

    from concurrent.futures import ThreadPoolExecutor
    workers = ThreadPoolExecutor(max_workers=4, thread_name_prefix="coinpile-round")
    running: set[str] = set()
    running_lock = threading.Lock()

    def one_round(u: dict) -> None:
        """One person's background work. People are handled side by side, so one person's slow servers never hold up anyone else."""
        try:
            try:
                scan_drop_folder(u["id"])
            except Exception:
                pass
            if not u.get("problem") and due(u["id"]):
                refresh(u["id"])                          # also sends webhooks
            else:
                send_hooks(u["id"])                       # CSV imports, goals, retries
        except Exception:
            pass
        finally:
            with running_lock:
                running.discard(u["id"])

    def loop():
        while True:
            time.sleep(60)
            for u in users.all():
                if gone(u["id"]):                               # forgotten since the list was read
                    continue
                with running_lock:
                    if u["id"] in running:                      # last round for this person is still going
                        continue
                    running.add(u["id"])
                workers.submit(one_round, u)

    # ---------------- each person's own app
    def person_app(uid: str) -> FastAPI:
        with made:
            if uid not in apps:
                if gone(uid):
                    raise Forgotten(uid)
                apps[uid] = build_person_app(uid)
            return apps[uid]

    def build_person_app(uid: str) -> FastAPI:
        db = person_db(uid)
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        who = lambda: (users.get(uid) or {}).get("email", "")
        me = users.ensure_handle(uid)

        def link_send(peer: str, kind: str, payload: dict) -> bool:
            """Linked people live in this same Coinpile, so a message is handed straight to the other person's data. Whether the two are linked is that side's call."""
            other = users.by_handle(peer)
            if not other or other == uid:
                return False
            r = links.receive(person_db(other), peer, me, kind, payload)
            if r.get("ok"):
                threading.Thread(target=lambda: flush_for(other), daemon=True).start()       # their answer (if any) goes back on its own
            return True

        def link_flush() -> int:
            return links.flush(db, link_send)

        def shared_changed(gid: int | None = None, edited: bool = False) -> None:
            """Something about goals changed: tell linked people, and (if switched on) keep the goals' Firefly III piggy banks in step."""
            try:
                links.bump(db, me, gid) if edited and gid else links.push(db, me)
                links.push_accounts(db)
                link_flush()
            except Exception:
                pass
            threading.Thread(target=lambda: send_hooks(uid), daemon=True).start()
            if ctx.writer.on("piggies"):
                try:
                    ctx.writer.sync_goals()
                    cfg.set_value(db, "ff_last_error", "")
                except firefly.FireflyError as e:
                    cfg.set_value(db, "ff_last_error", f"piggy banks: {e}")

        flushers[uid] = shared_changed
        ctx = api_money.make_ctx(db, lambda request: None, today, name=me, caldav_transport=caldav_transport, shared_changed=shared_changed,
                                 link_flush=link_flush, link_people=users.handles)
        ctx.llm_transport = llm_transport
        outbound = netguard.Slots()                 # this person's share of the worker threads for calls to servers they typed in
        ctx.outbound = outbound

        @app.exception_handler(netguard.Busy)
        async def busy(_request, e):
            return JSONResponse({"detail": str(e)}, 429, headers={"Retry-After": "5"})

        def read_back() -> None:
            """After a change in Firefly III: read the recent part of it again so the screens show what Firefly III now holds."""
            with locks.setdefault(uid, threading.Lock()):
                f = ff_for(uid)
                accts = firefly.sync_accounts(db, f)
                firefly.sync_transactions(db, f, accts, today() - timedelta(days=120), today() + timedelta(days=30))
                firefly.sync_bills(db, f, today())
                firefly.sync_piggies(db, f)

        ctx.writer = ffwrite.Writer(db, lambda: ff_for(uid), read_back, today)

        rh = robinhood.Robinhood(db, os.path.join(data_dir, "people", f"{safe_id(uid)}-robinhood"), robinhood_transport, who(), mcp_transport)

        def forget_rh_signin() -> None:
            for k in ("rh_mcp_token", "rh_mcp_refresh", "rh_mcp_expires", "rh_mcp_client", "rh_mcp_meta"):
                cfg.set_value(db, k, "")

        def rh_connect_work(url: str, back: str) -> tuple[dict, str]:
            with outbound.hold():
                meta = mcp.discover(url, mcp_transport)
                old = json.loads(cfg.get(db, "rh_mcp_meta") or "{}")
                same = old.get("redirect") == back and old.get("issuer") == meta.get("issuer") and old.get("url") == url
                client = cfg.get(db, "rh_mcp_client") if same else ""                   # an app registration belongs to one sign-in server
                if not client:
                    client = mcp.register(meta, back, mcp_transport)
            return meta, client

        @app.post("/api/robinhood/connect")
        async def rh_connect(request: Request):
            """Start signing in to Robinhood's MCP on Robinhood's own site. Coinpile only ever asks it for reads."""
            url = cfg.get(db, "rh_mcp_url") or robinhood.MCP_URL
            back = (my_url or str(request.base_url).rstrip("/")) + "/auth/robinhood/callback"
            try:
                meta, client = await run_in_threadpool(rh_connect_work, url, back)
            except (mcp.MCPError, netguard.Blocked) as e:
                raise HTTPException(502, str(e))
            meta = {**meta, "redirect": back, "url": url}
            verifier, challenge = mcp.pkce()
            state = secrets.token_urlsafe(24)
            with oauth_lock:
                for k in [k for k, v in rh_pending.items() if time.time() - v[2] > 900]:
                    del rh_pending[k]
                if len(rh_pending) >= 1000:
                    raise HTTPException(429, "too many sign-ins in progress; try again in a few minutes")
                rh_pending[state] = (uid, verifier, time.time(), meta, client)       # what the callback uses, fixed now
            return {"url": mcp.authorize_url(meta, client, back, state, challenge)}

        class RhToken(BaseModel):
            token: str
            url: str | None = None

        @app.post("/api/robinhood/token")
        def rh_paste_token(b: RhToken):
            """If signing in from Coinpile does not work for your account: paste an access token for Robinhood's MCP instead."""
            if len(b.token.strip()) < 16:
                raise HTTPException(422, "that does not look like a token")
            if b.url:
                try:
                    cfg.put(db, "rh_mcp_url", b.url.strip())
                except ValueError as e:
                    raise HTTPException(422, str(e))
            forget_rh_signin()
            cfg.set_value(db, "rh_mcp_token", b.token.strip())
            return {"connected": True}

        @app.post("/api/robinhood/test")
        async def rh_test():
            def work():
                with outbound.hold():
                    c = rh.mcp_client()
                    if not c:
                        raise HTTPException(409, "not connected to Robinhood yet")
                    return c.tools()
            try:
                tools = await run_in_threadpool(work)
            except mcp.MCPError as e:
                raise HTTPException(502, str(e))
            return {"ok": True, "tools": len(tools), "reads_used": sorted(t for t in tools if t in robinhood.READ_TOOLS),
                    "never_called": sorted(t for t in tools if t not in robinhood.READ_TOOLS)[:40]}

        @app.post("/api/robinhood/disconnect")
        def rh_disconnect():
            forget_rh_signin()
            with db.tx() as c:
                c.execute("DELETE FROM accounts WHERE id='rh:agent'")
            return {"connected": False}

        @app.get("/api/investments")
        def investments_view():
            return investments_data()

        def investments_data():
            accts = investments.accounts(db)
            return {"accounts": accts, "total": round(sum(a["balance"] for a in accts), 2), "currency": cfg.get(db, "currency") or "USD",
                    "candidates": investments.candidates(db), "providers": investments.providers(rh), "monthly": investments.monthly(db, [a["id"] for a in accts])}

        ctx.investments = investments_data

        class TrackIn(BaseModel):
            investment: bool
            category: str = "Investment"           # Investment or Retirement

        @app.put("/api/investments/{aid}")
        def investments_track(aid: str, b: TrackIn):
            """Track an account as an investment (or stop). This is Coinpile's own label: Firefly III is not changed."""
            a = db.one("SELECT * FROM accounts WHERE id=?", (aid,))
            if not a:
                raise HTTPException(404, "account not found")
            if b.investment and b.category not in investments.INVEST_CATEGORIES:
                raise HTTPException(422, "category must be Investment or Retirement")
            if not b.investment and a["kind"] == "investment":
                raise HTTPException(409, "this account is an investment account by type; it cannot be untracked")
            with db.tx() as c:
                c.execute("UPDATE accounts SET category=? WHERE id=?", (b.category if b.investment else None, aid))
            return investments_view()

        @app.get("/api/robinhood/main")
        def rh_main():
            return rh.main(cfg.get(db, "rh_main_source") or "auto") or {"error": "no Robinhood data yet: upload an activity report on the Robinhood tab"}

        @app.get("/api/robinhood/agent")
        def rh_agent():
            return rh.fetch("agent") or {"error": "the rh-agent connector is not set up (RH_AGENT_URL)"}

        @app.post("/api/robinhood/csv-upload")
        async def rh_csv(request: Request):
            raw = await request.body()
            if len(raw) > 5_000_000:
                raise HTTPException(413, "file too large")
            try:
                saved = rh.save_csv(raw)
            except ValueError as e:
                raise HTTPException(422, str(e))
            rh.snapshot(cfg.get(db, "rh_main_source") or "auto")
            return {"saved": saved}

        ctx.robinhood = rh

        # CSV imports: into accounts you keep in Coinpile only. Firefly III is never written to.
        @app.post("/api/import/bank-csv")
        async def import_bank_csv(request: Request, account_id: str, flip: bool = False):
            """A CSV from a bank's website, into an account you added in Coinpile (an account from Firefly III gets its transactions from Firefly III)."""
            a = db.one("SELECT source FROM accounts WHERE id=?", (account_id,))
            if not a:
                raise HTTPException(404, "account not found")
            raw = await request.body()
            if len(raw) > 5_000_000:
                raise HTTPException(413, "file too large")
            if a["source"] == "firefly":
                if not ctx.writer.on("accounts"):
                    raise HTTPException(409, "that account comes from Firefly III: import into it there, switch on sending to Firefly III in Settings, Firefly III, or add an account in Coinpile for this")
                try:
                    return await run_in_threadpool(ctx.writer.import_csv, account_id, raw.decode("utf-8", "replace"), flip)
                except ValueError as e:
                    raise HTTPException(422, str(e))
                except firefly.FireflyError as e:
                    raise HTTPException(502, str(e))
            if a["source"] != "manual":
                raise HTTPException(409, "import into an account you added in Coinpile")
            try:
                rows, bal = importers.parse_bank(raw.decode("utf-8", "replace"), flip)
            except ValueError as e:
                raise HTTPException(422, str(e))
            return {"rows": len(rows), "added": importers.store_bank(db, account_id, rows, bal), "balance": bal}

        @app.post("/api/import/csv")
        async def import_csv(request: Request, provider: str | None = None):
            """A PayPal, Venmo or Cash App export: each becomes its own account in Coinpile. Importing an overlapping file again never doubles anything."""
            raw = await request.body()
            if len(raw) > 5_000_000:
                raise HTTPException(413, "file too large")
            try:
                prov, rows = importers.parse(raw.decode("utf-8", "replace"), provider)
            except ValueError as e:
                raise HTTPException(422, str(e))
            return {"provider": prov, "rows": len(rows), "added": importers.store(db, prov, rows)}

        api_money.register(app, ctx)
        api_plan.register(app, ctx)
        agent.register(app, ctx)
        api_links.register(app, ctx)

        @app.get("/api/status")
        def status():
            u = users.get(uid) or {}
            return {"currency": cfg.get(db, "currency") or "USD", "firefly": {"address": ff_public, "email": u.get("email"), "signed_in_with": u.get("kind"), "has_password": bool(u.get("pw_hash")), "last_refresh": cfg.get(db, "ff_last_refresh") or None,
                                "last_error": cfg.get(db, "ff_last_error") or None, "refresh_minutes": int(float(cfg.get(db, "refresh_minutes") or 10)),
                                "accounts": db.one("SELECT COUNT(*) n FROM accounts WHERE source='firefly'")["n"], "transactions": db.one("SELECT COUNT(*) n FROM transactions WHERE source='firefly'")["n"]},
                    "sends": {k: cfg.get(db, v) == "on" for k, v in ffwrite.PARTS.items()},
                    "connector": {"configured": bool(cfg.get(db, "connector_url") and cfg.get(db, "connector_name") and cfg.get(db, "connector_token"))},
                    "caldav_configured": bool(cfg.get(db, "caldav_url")),
                    "robinhood": {"mcp": bool(cfg.get(db, "rh_mcp_token")), "agent": ctx.robinhood.probe("agent"), "csv": "built-in", "live": ctx.robinhood.probe("live"),
                                  "csv_files": sorted(os.listdir(ctx.robinhood.csv_dir)) if os.path.isdir(ctx.robinhood.csv_dir) else []}}

        @app.post("/api/refresh")
        async def refresh_now(full: bool = False):
            try:
                return await run_in_threadpool(refresh, uid, full)
            except firefly.TokenRefused as e:
                raise HTTPException(409, str(e))
            except firefly.FireflyError as e:
                raise HTTPException(502, str(e))

        class TokenNew(BaseModel):
            name: str
            scopes: list[str]

        @app.get("/api/tokens")
        def tokens():
            return {"items": [{**t, "scopes": t["scopes"].split(",")} for t in db.all("SELECT id,name,scopes,created,last_used,revoked FROM api_tokens ORDER BY revoked, id DESC")],
                    "scopes": external.SCOPES}

        @app.post("/api/tokens")
        def token_new(b: TokenNew):
            bad = [x for x in b.scopes if x not in external.SCOPES]
            if not b.name.strip() or not b.scopes or bad:
                raise HTTPException(422, "a name and at least one valid scope are required" + (f" (unknown: {', '.join(bad)})" if bad else ""))
            tok = external.new_token(safe_id(uid))
            with db.tx() as c:
                cur = c.execute("INSERT INTO api_tokens(name,token_hash,scopes,created) VALUES(?,?,?,?)", (b.name.strip()[:60], sha(tok), ",".join(sorted(set(b.scopes))), sync.now()))
            return {"id": cur.lastrowid, "name": b.name.strip(), "scopes": sorted(set(b.scopes)), "token": tok, "note": "Copy it now. It is not shown again."}

        @app.delete("/api/tokens/{tid}")
        def token_revoke(tid: int):
            with db.tx() as c:
                cur = c.execute("UPDATE api_tokens SET revoked=1 WHERE id=?", (tid,))
            if not cur.rowcount:
                raise HTTPException(404, "not found")
            return {"revoked": tid}

        @app.get("/api/summary")
        def summary_preview():
            """What the Pondering Orb would see (Settings, Home Assistant & API, Preview)."""
            return orb_summary(uid)

        webhooks.init(db)

        class HookIn(BaseModel):
            name: str | None = None
            url: str | None = None
            events: list[str] | None = None
            detail: bool | None = None
            active: bool | None = None

        def hook_err(fn):
            try:
                return fn()
            except KeyError:
                raise HTTPException(404, "no such webhook")
            except (ValueError, netguard.Blocked) as e:
                raise HTTPException(422, str(e))

        @app.get("/api/webhooks")
        def hooks_list():
            return {"items": [webhooks.view(r) for r in db.all("SELECT * FROM webhooks ORDER BY id")], "events": webhooks.EVENTS}

        @app.post("/api/webhooks")
        def hooks_add(b: HookIn):
            if not b.url or not b.events:
                raise HTTPException(422, "an address and at least one event are required")
            r = hook_err(lambda: webhooks.add(db, b.name or "Webhook", b.url, b.events, True if b.detail is None else b.detail))
            webhooks.collect(db, users.ensure_handle(uid), today())          # from now on: what already happened is not sent
            return r

        @app.put("/api/webhooks/{hid}")
        def hooks_update(hid: int, b: HookIn):
            return hook_err(lambda: webhooks.update(db, hid, **b.model_dump(exclude_unset=True)))

        @app.delete("/api/webhooks/{hid}")
        def hooks_delete(hid: int):
            hook_err(lambda: webhooks.remove(db, hid))
            return {"deleted": hid}

        @app.post("/api/webhooks/{hid}/test")
        async def hooks_test(hid: int):
            if not db.one("SELECT 1 FROM webhooks WHERE id=? AND active=1", (hid,)):
                raise HTTPException(404, "no such webhook (or it is switched off)")
            webhooks.queue(db, users.ensure_handle(uid), "test", {"message": "Coinpile webhooks are working."}, {"message": "Coinpile webhooks are working."}, only_hook=hid)

            def work():
                with outbound.hold():
                    return webhooks.deliver(db, webhook_transport, limit=5)
            r = await run_in_threadpool(work)
            last = db.one("SELECT status FROM webhook_outbox WHERE hook_id=? ORDER BY id DESC LIMIT 1", (hid,))
            return {"ok": r["sent"] > 0, "status": (last or {}).get("status")}

        @app.get("/api/webhooks/{hid}/deliveries")
        def hooks_log(hid: int):
            return hook_err(lambda: webhooks.deliveries(db, hid) if db.one("SELECT 1 FROM webhooks WHERE id=?", (hid,)) else (_ for _ in ()).throw(KeyError(hid)))

        class PasswordSet(BaseModel):
            new: str | None = None
            current: str | None = None

        @app.put("/api/password")
        async def password_set(b: PasswordSet, request: Request):
            """Set or change the Coinpile password. Changing one needs the current one (forgot it? sign out and use Forgot password with a token)."""
            u = users.get(uid) or {}
            if u.get("pw_hash") and not await run_in_threadpool(slow_hash, verify_password, b.current or "", u["pw_hash"]):
                raise HTTPException(403, "the current password is not right")
            try:
                check_password_strength(b.new or "", u.get("email") or "")
            except ValueError as e:
                raise HTTPException(422, str(e))
            users.set_password(uid, await run_in_threadpool(slow_hash, hash_password, b.new), keep_session=request.cookies.get(COOKIE))
            return {"password_set": True}

        @app.delete("/api/password")
        async def password_remove(b: PasswordSet, request: Request):
            u = users.get(uid) or {}
            if u.get("pw_hash") and not await run_in_threadpool(slow_hash, verify_password, b.current or "", u["pw_hash"]):
                raise HTTPException(403, "the current password is not right")
            users.set_password(uid, None, keep_session=request.cookies.get(COOKIE))
            return {"password_set": False}

        @app.get("/api/firefly/webhook")
        def webhook_info(request: Request):
            base = my_url or str(request.base_url).rstrip("/")
            return {"url": f"{base}/hooks/firefly/{hook_key(uid)}", "secret_set": bool(cfg.get(db, "ff_webhook_secret")), "last": cfg.get(db, "ff_last_hook") or None}

        @app.post("/api/firefly/sync-goals")
        async def sync_goals_now():
            if not ctx.writer.on("piggies"):
                raise HTTPException(409, "switch on sending goals to Firefly III first")
            try:
                r = await run_in_threadpool(ctx.writer.sync_goals)
                await run_in_threadpool(firefly.sync_piggies, db, ff_for(uid))
                return r
            except firefly.FireflyError as e:
                raise HTTPException(502, str(e))

        @app.get("/api/piggy-banks")
        def piggy_banks():
            return firefly.piggies(db)

        # ---- the Piggy banks page: Firefly III's piggy banks, read every refresh. Making and changing them is a separate opt-in switch.
        class PiggyIn(BaseModel):
            name: str | None = None
            target: float | None = None          # 0 clears the target
            target_date: str | None = None       # "" clears it
            notes: str | None = None
            account_id: str | None = None        # new piggy banks: the Firefly III account that holds the money ("ff:12")
            start_amount: float = 0

        class MoneyIn(BaseModel):
            account_id: str
            amount: float                        # positive puts money in, negative takes it out

        def ff_asset(aid: str | None) -> str:
            row = db.one("SELECT id FROM accounts WHERE id=? AND kind='cash'", (aid or "",)) if (aid or "").startswith("ff:") else None
            if not row:
                raise HTTPException(422, "choose one of your Firefly III asset accounts")
            return aid.split(":", 1)[1]

        def day(s: str) -> str:
            try:
                return date.fromisoformat(s).isoformat()
            except ValueError:
                raise HTTPException(422, "a date is YYYY-MM-DD")

        def goal_piggies() -> dict:
            return {r["piggy_id"]: r["name"] for r in db.all("SELECT p.piggy_id, g.name FROM ff_goal_piggy p JOIN goals g ON g.id=p.goal_id")}

        def piggy_changes_on():
            if not ctx.writer.on("piggy_banks"):
                raise HTTPException(409, "making and changing piggy banks from Coinpile is off. Switch it on in Settings, Firefly III, or do it in Firefly III")

        def piggy_or_404(pid: str) -> dict:
            p = next((x for x in firefly.piggies(db) if x["id"] == pid), None)
            if not p:
                raise HTTPException(404, "no such piggy bank")
            if pid in goal_piggies():
                raise HTTPException(409, f"this piggy bank follows your goal \"{goal_piggies()[pid]}\": change the goal on the Savings page instead")
            return p

        async def piggy_reread():
            try:
                await run_in_threadpool(firefly.sync_piggies, db, ff_for(uid))
            except firefly.FireflyError:
                pass

        def budgets_for(m: str) -> dict:
            try:
                first = date.fromisoformat(m + "-01")
            except ValueError:
                raise HTTPException(422, "month is YYYY-MM")
            last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
            rows = firefly.budgets(ff_for(uid), first, last)
            with_limit = [r for r in rows if r["limit"]]
            return {"month": m, "items": rows, "limit": round(sum(r["limit"] for r in with_limit), 2), "spent": round(sum(r["spent"] for r in rows), 2),
                    "left": round(sum(r["left"] for r in with_limit), 2), "link": f"{ff_public}/budgets" if ff_public else None}

        ctx.ff_budgets = budgets_for

        @app.get("/api/firefly/budgets")
        async def ff_budgets(month: str | None = None):
            """Firefly III's own budgets for a month (limits and spending), read live. Coinpile's per-category plan stays separate."""
            try:
                return await run_in_threadpool(budgets_for, month or today().isoformat()[:7])
            except firefly.FireflyError as e:
                raise HTTPException(502, str(e))

        @app.get("/api/piggy-banks/overview")
        def piggy_overview():
            items, goals = firefly.piggies(db), goal_piggies()
            notes = {}
            for n in db.all("SELECT id, title, author, updated, piggy_id FROM notes WHERE piggy_id IS NOT NULL ORDER BY pinned DESC, updated DESC"):
                notes.setdefault(n["piggy_id"], []).append({"id": n["id"], "title": n["title"], "author": n["author"], "updated": n["updated"]})
            for p in items:
                p["goal"] = goals.get(p["id"])
                p["my_notes"] = notes.get(p["id"], [])
                p["link"] = f"{ff_public}/piggy-banks/show/{p['id']}" if ff_public else None
                p["left"] = round(max(0.0, (p["target"] or 0) - (p["current"] or 0)), 2) if p["target"] else None
            act = [p for p in items if p["active"]]
            return {"items": items, "can_change": ctx.writer.on("piggy_banks"), "firefly": ff_public or None,
                    "total": round(sum(p["current"] or 0 for p in act), 2), "targets": round(sum(p["target"] or 0 for p in act), 2),
                    "still_needed": round(sum(p["left"] or 0 for p in act), 2),
                    "accounts": [{"id": a["id"], "name": a["name"], "institution": a["institution"]} for a in db.all("SELECT id, name, institution FROM accounts WHERE kind='cash' AND id LIKE 'ff:%' ORDER BY name")]}

        def piggy_events_for(pid: str) -> list[dict]:
            if not pid.isdigit():
                raise HTTPException(404, "no such piggy bank")
            d = ff_for(uid).get(f"/piggy-banks/{pid}/events", limit=50)
            out = []
            for e in d.get("data") or []:
                at = e.get("attributes") or {}
                try:
                    amt = float(at.get("amount") or 0)
                except ValueError:
                    amt = 0.0
                out.append({"date": str(at.get("created_at") or "")[:10], "amount": amt, "account": at.get("account_name") or ""})
            return out

        ctx.piggy_events = piggy_events_for

        @app.get("/api/piggy-banks/{pid}/events")
        async def piggy_events(pid: str):
            """The piggy bank's history (money put in and taken out), straight from Firefly III."""
            try:
                return await run_in_threadpool(piggy_events_for, pid)
            except firefly.FireflyError as e:
                raise HTTPException(502, str(e))

        @app.post("/api/piggy-banks")
        async def piggy_new(b: PiggyIn):
            piggy_changes_on()
            if not (b.name or "").strip():
                raise HTTPException(422, "a name is required")
            if (b.target is not None and b.target < 0) or b.start_amount < 0:
                raise HTTPException(422, "amounts cannot be negative")
            acct = ff_asset(b.account_id)
            own = (db.one("SELECT coalesce(native_currency, currency) c FROM accounts WHERE id=?", (b.account_id,)) or {}).get("c")      # Firefly III wants the account's own currency
            body = {"name": b.name.strip()[:255], "start_date": today().isoformat(), "transaction_currency_code": own or cfg.get(db, "currency") or "USD",
                    "accounts": [{"account_id": acct, "current_amount": f"{b.start_amount:.2f}"}]}
            if b.target:
                body["target_amount"] = f"{b.target:.2f}"
            if b.target_date:
                body["target_date"] = day(b.target_date)
            if b.notes:
                body["notes"] = b.notes[:2000]
            try:
                d = await run_in_threadpool(ff_for(uid).send, "POST", "/piggy-banks", body)
            except firefly.FireflyError as e:
                raise HTTPException(502, str(e))
            await piggy_reread()
            return {"id": str((d.get("data") or {}).get("id") or "")}

        @app.put("/api/piggy-banks/{pid}")
        async def piggy_edit(pid: str, b: PiggyIn):
            piggy_changes_on()
            piggy_or_404(pid)
            body = {}
            if b.name is not None:
                if not b.name.strip():
                    raise HTTPException(422, "a name is required")
                body["name"] = b.name.strip()[:255]
            if b.target is not None:
                if b.target < 0:
                    raise HTTPException(422, "the target cannot be negative")
                body["target_amount"] = f"{b.target:.2f}" if b.target else None
            if b.target_date is not None:
                body["target_date"] = day(b.target_date) if b.target_date else None
            if b.notes is not None:
                body["notes"] = b.notes[:2000]
            if not body:
                raise HTTPException(422, "nothing to change")
            try:
                await run_in_threadpool(ff_for(uid).send, "PUT", f"/piggy-banks/{pid}", body)
            except firefly.FireflyError as e:
                raise HTTPException(502, str(e))
            await piggy_reread()
            return {"changed": pid}

        @app.post("/api/piggy-banks/{pid}/money")
        async def piggy_money(pid: str, b: MoneyIn):
            """Put money in or take it out. Some Firefly III versions (6.7 among them) fail on this through their API; then say so and point to Firefly III."""
            piggy_changes_on()
            p = piggy_or_404(pid)
            acct = ff_asset(b.account_id)
            if not b.amount:
                raise HTTPException(422, "enter an amount")
            now = {x["account_id"]: x["amount"] or 0 for x in p["accounts"]}
            new = round(now.get(b.account_id, 0) + b.amount, 2)
            if new < 0:
                raise HTTPException(422, f"only {now.get(b.account_id, 0):,.2f} is in it on that account")
            if p["target"] and sum(now.values()) + b.amount > p["target"] + 0.005:
                raise HTTPException(422, f"that is more than the piggy bank still needs ({p['target'] - sum(now.values()):,.2f})")
            accts = [{"account_id": k.split(":", 1)[1], "current_amount": f"{(new if k == b.account_id else v):.2f}"} for k, v in now.items()]
            if b.account_id not in now:
                accts.append({"account_id": acct, "current_amount": f"{new:.2f}"})
            try:
                await run_in_threadpool(ff_for(uid).send, "PUT", f"/piggy-banks/{pid}", {"accounts": accts})
            except firefly.FireflyError as e:
                if "500" in str(e):
                    raise HTTPException(502, "your Firefly III could not change the amount through its API (a bug in some Firefly III versions). Add or take out the money in Firefly III itself: use the Firefly III link on this piggy bank")
                raise HTTPException(502, str(e))
            await piggy_reread()
            return {"current": round(sum(now.values()) + b.amount, 2)}

        @app.get("/api/settings")
        def settings_get():
            v = cfg.view(db)
            return {k: v[k] for k in v if k in USER_KEYS or k.startswith(("notify_", "alert_"))}

        @app.put("/api/settings")
        async def settings_put(request: Request):
            body = await request.json()
            if not isinstance(body, dict):
                raise HTTPException(422, "expected an object")
            bad = [k for k in body if k not in USER_KEYS and not k.startswith(("notify_", "alert_"))]
            if bad:
                raise HTTPException(422, f"{', '.join(bad)} cannot be set here")
            try:
                for k, v in body.items():
                    cfg.validate(k, v)
                moved = "rh_mcp_url" in body and str(body["rh_mcp_url"]).strip() != (cfg.get(db, "rh_mcp_url") or "")
                for k, v in body.items():
                    cfg.put(db, k, v)
                if moved:
                    forget_rh_signin()                     # never send this server's tokens to another one
            except ValueError as e:
                raise HTTPException(422, str(e))
            return settings_get()

        # calendar and notifications, as in the old app
        @app.post("/api/subscriptions/export/caldav")
        def subs_caldav():
            url, user, pw = (cfg.get(db, k) for k in ("caldav_url", "caldav_user", "caldav_password"))
            if not url:
                raise HTTPException(503, "CalDAV is not set up yet (Settings, Calendar)")
            with outbound.hold():
                return feed.push_caldav(db.all("SELECT * FROM subscriptions"), url, user, pw, caldav_transport)

        @app.get("/subscriptions.ics")
        def subs_ics():
            return Response(feed.ics(db.all("SELECT * FROM subscriptions")), media_type="text/calendar")

        @app.post("/api/settings/test-caldav")
        def test_caldav():
            url, user, pw = (cfg.get(db, k) for k in ("caldav_url", "caldav_user", "caldav_password"))
            if not url:
                raise HTTPException(503, "CalDAV is not set up yet")
            try:
                with outbound.hold(), netguard.client(caldav_transport, timeout=15, auth=(user, pw)) as c:
                    r = c.request("PROPFIND", url, headers={"Depth": "0"})
            except httpx.HTTPError as e:
                return {"ok": False, "detail": type(e).__name__}
            return {"ok": r.status_code in (200, 207), "status": r.status_code}

        @app.get("/api/notify/status")
        def notify_status():
            last = db.one("SELECT MAX(sent) s, COUNT(*) n FROM notified")
            return {"configured": bool(cfg.get(db, "notify_url") or cfg.get(db, "notify_url_cds")), "main": bool(cfg.get(db, "notify_url")), "cds": bool(cfg.get(db, "notify_url_cds")),
                    "min_severity": cfg.get(db, "notify_min_severity") or "warning", "min_severity_cds": cfg.get(db, "notify_min_severity_cds") or "info", "last_sent": last["s"], "tracked": last["n"]}

        @app.post("/api/notify/test")
        def notify_test(route: str = "main"):
            if route not in ("main", "cds"):
                raise HTTPException(422, "route must be main or cds")
            url = (cfg.get(db, "notify_url_cds") or cfg.get(db, "notify_url")) if route == "cds" else cfg.get(db, "notify_url")
            if not url:
                raise HTTPException(503, "Add your Mattermost webhook address first")
            try:
                with outbound.hold():
                    notify.post(url, {"text": ":coin: **Coinpile**: notifications are working" + (" for CDs and bonds." if route == "cds" else "."), "username": "Coinpile", "icon_emoji": ":coin:"}, notify_transport)
            except httpx.HTTPStatusError as e:
                return {"ok": False, "detail": f"the webhook answered {e.response.status_code}"}
            except httpx.HTTPError as e:
                return {"ok": False, "detail": f"could not reach it ({type(e).__name__})"}
            return {"ok": True}

        @app.post("/api/notify/run")
        def notify_run():
            with outbound.hold():
                return notify.run(db, today(), who(), notify_transport)

        # the Plaid connector, as this person
        held: dict = {}

        def connector() -> conn.Connector:
            """One per person, kept while the settings stay the same, so the connector session is reused instead of signing in on every click."""
            key = (cfg.get(db, "connector_url"), cfg.get(db, "connector_name"), sha(cfg.get(db, "connector_token")))
            if held.get("key") != key:
                held["c"], held["key"] = conn.Connector(cfg.get(db, "connector_url"), cfg.get(db, "connector_name"), cfg.get(db, "connector_token"), connector_transport), key
            return held["c"]

        def conn_call(fn):
            try:
                with outbound.hold():
                    return fn()
            except conn.ConnectorError as e:
                raise HTTPException(502, str(e))

        ctx.banks = lambda: conn_call(lambda: connector().banks())

        @app.get("/api/banks")
        async def banks():
            return await run_in_threadpool(conn_call, lambda: connector().banks())

        class BankPatch(BaseModel):
            schedule: str | None = None
            recurring: str | None = None
            category: str | None = None              # what this bank is for (Checking, Savings, Bills...); the connector asks savings-only banks less often

        @app.put("/api/banks/{item_id}")
        async def bank_set(item_id: str, b: BankPatch):
            """When the connector asks this bank, and whether Plaid's (paid) recurring detection runs for it."""
            patch = {k: v for k, v in b.model_dump().items() if v is not None}
            return await run_in_threadpool(conn_call, lambda: connector().set_item(item_id, patch))

        @app.delete("/api/banks/{item_id}")
        async def bank_unlink(item_id: str):
            await run_in_threadpool(conn_call, lambda: connector().unlink(item_id))
            return {"unlinked": item_id, "note": "What is already in Firefly III stays there."}

        @app.get("/api/banks/usage")
        async def bank_usage():
            return await run_in_threadpool(conn_call, lambda: connector().usage())

        @app.post("/api/banks/pull")
        async def banks_pull():
            r = await run_in_threadpool(conn_call, lambda: connector().pull())
            refresh_soon(uid)                     # the connector has sent the news on to Firefly III; read it back
            return {"pulled": True, "firefly": (r or {}).get("firefly")}

        class LinkIn(BaseModel):
            item_id: str | None = None

        @app.post("/api/banks/link-token")
        async def banks_link_token(b: LinkIn):
            return await run_in_threadpool(conn_call, lambda: connector().link_token(b.item_id))

        class ExchangeIn(BaseModel):
            public_token: str
            institution: str = ""

        @app.post("/api/banks/exchange")
        async def banks_exchange(b: ExchangeIn):
            r = await run_in_threadpool(conn_call, lambda: connector().exchange(b.public_token, b.institution))
            refresh_soon(uid)
            return {"linked": True, "institution": (r or {}).get("institution") or b.institution,
                    "note": "The new accounts wait for your admin's approval in the connector before Firefly III (and so Coinpile) gets them."}

        @app.post("/api/forget")
        def forget():
            """Stop reading this Firefly III account and delete everything Coinpile keeps for it (Firefly III itself is not touched)."""
            for peer in [r["peer"] for r in db.all("SELECT peer FROM links")]:
                try:
                    links.unlink(db, peer)                          # the others stop sharing with someone who is gone
                except Exception:
                    pass
            try:
                link_flush()
            except Exception:
                pass
            with made:                                      # from here on person_db/person_app refuse this uid, so no late thread recreates the files
                users.forget(uid)
                apps.pop(uid, None)
                d = dbs.pop(uid, None)
            flushers.pop(uid, None)
            for k in [k for k, v in hooks_known.items() if v == uid]:
                del hooks_known[k]
            import shutil
            shutil.rmtree(os.path.join(data_dir, "people", f"{safe_id(uid)}-robinhood"), ignore_errors=True)
            shutil.rmtree(os.path.join(data_dir, "people", f"{safe_id(uid)}-imports"), ignore_errors=True)
            if d:
                d.conn.close()
            try:
                os.remove(os.path.join(data_dir, "people", f"{safe_id(uid)}.db"))
            except FileNotFoundError:
                pass
            return {"forgotten": True}

        return app

    # ---------------- read-only API for Home Assistant, scripts and the Pondering Orb (scoped tokens, never a login)
    limiter = external.Limiter(int(os.environ.get("API_RATE_PER_MIN", "30")))
    last_api_refresh: dict[str, float] = {}

    def orb_summary(uid: str) -> dict:
        """Only what this person ticked under Settings, Home Assistant & API. Never transactions."""
        db, t = person_db(uid), today()
        share = set(cfg.get_list(db, "share"))
        out = {"name": ((users.get(uid) or {}).get("email") or "").split("@")[0]}
        ext = external.summary(db, t) if share & {"bills", "alerts", "savings", "goals"} else {}
        if "net_worth" in share:
            out["net_worth"] = ledger.net_worth(db)
        if "subscriptions" in share:
            out["subscriptions_monthly"] = round(sum(s["monthly"] for s in ledger.subscriptions(db, t)), 2)
        if "bills" in share:
            out["bills"] = {"next_7d": ext["bills_7d"], "next_30d": ext["bills_30d"], "next": ext["next_bill"]}
        if "alerts" in share:
            out["alerts"] = {"total": ext["alerts_total"], "critical": ext["alerts_critical"], "warning": ext["alerts_warning"]}
        if "bill_list" in share:
            out["bill_list"] = [{"date": i["date"], "name": i["name"], "amount": i["amount"], "category": i["category"]} for i in ledger.upcoming(db, 30, t)["items"][:60]]
        if "alert_list" in share:
            out["alert_list"] = [{"severity": a["severity"], "kind": a["kind"], "title": a["title"], "detail": a.get("detail", ""), **({"date": a["date"]} if a.get("date") else {})} for a in ledger.alerts(db, t)][:30]
        if "savings" in share:
            out["savings"] = {"total": ext["savings_total"], "free": ext["savings_free"], "earmarked": ext["savings_earmarked"]}
        if "goals" in share:
            out["goals"] = [{"name": g["name"], "target": g["target"], "allocated": g["allocated"], "pct": g["pct"]} for g in plan.goals_view(db, t)["items"] if g["status"] == "active"]
        for key, acct in (("robinhood_main", "rh:main"), ("robinhood_agent", "rh:agent")):
            if key in share:
                a = db.one("SELECT balance, updated FROM accounts WHERE id=?", (acct,))
                out[key] = {"value": a["balance"], "as_of": a["updated"]} if a else None
        if "spending" in share:
            fc = ledger.spending_forecast(db, t)
            out["spending"] = {"so_far": fc["spent_so_far"], "projected": fc["projected"], "income_avg": fc["income_avg"], "left_after": fc["left_after_projected"]}
        return out

    def token_holder(request: Request, resource: str):
        tok = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        key = external.person_of(tok)
        uid = next((u["id"] for u in users.all() if safe_id(u["id"]) == key), None) if key else None
        if not uid:
            raise HTTPException(401, "invalid token")                          # the same answer for an unknown person and a wrong secret
        db = person_db(uid)
        row, err = external.authenticate(db, tok, resource, sync.now())
        if err:
            raise HTTPException(*err)
        if not limiter.ok((uid, row["id"])):
            raise HTTPException(429, "too many requests: slow down (limit %d a minute)" % limiter.n)
        return uid, db, row

    @front.get("/api/v1/summary")
    def orb(request: Request):
        """For the Pondering Orb (its 'goblinbank' widget): a token with the 'orb' scope reads that person's shared summary.
        The 'goblins' key is the shape the Orb reads (the same as plaid-proxy's /api/v1/summary), so it keeps that name."""
        uid, _, _ = token_holder(request, "orb")
        s = orb_summary(uid)
        return {"goblins": {s["name"] or "me": s}}

    @front.get("/api/v1/me")
    @front.get("/api/v1/me/{resource:path}")
    async def me_api(request: Request, resource: str = ""):
        res = resource.strip("/")
        uid, db, row = token_holder(request, res)
        refreshed = False
        if request.query_params.get("refresh") in ("1", "true", "yes"):
            if "refresh" not in row["scopes"].split(","):
                raise HTTPException(403, "this token does not have the 'refresh' scope")
            if time.time() - last_api_refresh.get(uid, 0) >= external.REFRESH_MIN_SECONDS:
                last_api_refresh[uid] = time.time()
                try:
                    await run_in_threadpool(refresh, uid)
                    refreshed = True
                except firefly.FireflyError:
                    pass
        if not res:
            return {"person": (users.get(uid) or {}).get("email"), "token": row["name"], "resources": sorted(r for r, sc in external.RESOURCES.items() if sc in row["scopes"].split(","))}
        try:
            out = external.data(db, res, today(), {k: v for k, v in request.query_params.items() if k != "refresh"})
        except (ValueError, KeyError):
            raise HTTPException(422, "bad parameter")
        if isinstance(out, dict):
            out["read_from_firefly_at"], out["refreshed"] = cfg.get(db, "ff_last_refresh") or None, refreshed
        return out

    # ---------------- Firefly III webhooks: Firefly III tells Coinpile the moment a transaction is made, changed or deleted
    def hook_key(uid: str) -> str:
        db = person_db(uid)
        k = cfg.get(db, "ff_hook_key")
        if not k:
            k = secrets.token_urlsafe(18)
            cfg.set_value(db, "ff_hook_key", k)
        hook_index()[k] = uid
        return k

    hook_seen: dict[str, float] = {}
    hooks_known: dict[str, str] = {}

    def hook_index() -> dict[str, str]:
        if not hooks_known:
            for u in users.all():
                k = cfg.get(person_db(u["id"]), "ff_hook_key")
                if k:
                    hooks_known[k] = u["id"]
        return hooks_known

    @front.post("/hooks/firefly/{key}")
    async def firefly_hook(key: str, request: Request):
        """Signed by Firefly III with the webhook's secret: header Signature: t=<time>,v1=<HMAC-SHA3-256 of "<time>.<body>">. Anything else is ignored."""
        uid = hook_index().get(key)
        if not uid:
            raise HTTPException(404, "unknown hook")
        body = await request.body()
        db = person_db(uid)
        secret = cfg.get(db, "ff_webhook_secret")
        parts = dict(p.split("=", 1) for p in request.headers.get("signature", "").split(",") if "=" in p)
        try:
            t = int(parts.get("t", "0"))
        except ValueError:
            t = 0
        want = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha3_256).hexdigest() if secret else ""
        if not secret or not hmac.compare_digest(want, parts.get("v1", "")) or abs(time.time() - t) > 600:
            raise HTTPException(401, "bad signature")
        cfg.set_value(db, "ff_last_hook", sync.now())
        if time.time() - hook_seen.get(uid, 0) > 5:                   # a burst (a CSV import in Firefly III) becomes one read
            hook_seen[uid] = time.time()
            threading.Timer(3.0, lambda: refresh_soon(uid)).start()
        return {"ok": True}

    # ---------------- signing in
    def is_secure(request: Request) -> bool:
        return request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https" or my_url.startswith("https://")

    def signed_in(response: Response, request: Request, uid: str) -> None:
        tok = users.new_session(uid)
        response.set_cookie(COOKIE, tok, max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax", secure=is_secure(request), path="/")

    @front.get("/health")
    def health():
        return {"ok": True, "firefly_configured": bool(ff_url)}

    @front.get("/auth/me")
    def me(request: Request):
        uid = users.session_user(request.cookies.get(COOKIE))
        u = users.get(uid) if uid else None
        return {"signed_in": bool(u), "email": (u or {}).get("email"), "oauth": bool(client_id and ff_url), "firefly": ff_public, "problem": (u or {}).get("problem"),
                "configured": bool(ff_url), "has_password": bool((u or {}).get("pw_hash"))}

    class TokenIn(BaseModel):
        token: str

    @front.post("/auth/token")
    async def auth_token(b: TokenIn, request: Request, response: Response):
        """Sign in with a Firefly III Personal Access Token (Firefly III: Options, Profile, OAuth, Personal Access Tokens)."""
        if not ff_url:
            raise HTTPException(503, "Coinpile does not know your Firefly III address yet (set FIREFLY_URL)")
        tok = b.token.strip()
        if len(tok) < 20 or len(tok) > 5000:
            raise HTTPException(422, "that is not a Firefly III Personal Access Token (it is a very long string)")
        try:
            me_ = await run_in_threadpool(lambda: firefly.Firefly(ff_url, tok, firefly_transport).me())
        except firefly.TokenRefused:
            raise HTTPException(401, "Firefly III did not accept that token")
        except firefly.FireflyError as e:
            raise HTTPException(502, str(e))
        users.save(me_["id"], me_["email"], "pat", tok)
        users.ensure_handle(me_["id"])
        signed_in(response, request, me_["id"])
        refresh_soon(me_["id"])
        log.info("someone signed in with a token")
        return {"signed_in": True, "email": me_["email"]}

    pw_throttle = Throttle()
    ip_throttle = Throttle()
    ip_throttle.FREE = 30                                     # a household behind one address still gets plenty of tries
    dummy_hash = hash_password("not a real password, only for timing")
    scrypt_slots = threading.BoundedSemaphore(3)          # each password check uses ~128 MB on purpose: a few at a time, never dozens

    def slow_hash(fn, *a):
        if not scrypt_slots.acquire(timeout=5):
            raise HTTPException(429, "busy; try again in a moment", headers={"Retry-After": "5"})
        try:
            return fn(*a)
        finally:
            scrypt_slots.release()

    def client_ip(request: Request) -> str:
        return "ip:" + (request.client.host if request.client else "?")

    class PasswordIn(BaseModel):
        email: str
        password: str

    @front.post("/auth/password")
    async def auth_password(b: PasswordIn, request: Request, response: Response):
        """Sign in with your Firefly III email and the Coinpile password you set (Settings, Firefly III). It only opens Coinpile."""
        key, ip = "pw:" + b.email.strip().lower()[:200], client_ip(request)
        pw_throttle.check(key)
        ip_throttle.check(ip)                                  # many names from one place is guessing too
        u = users.by_email(b.email)
        ok = await run_in_threadpool(slow_hash, verify_password, b.password, (u or {}).get("pw_hash") or dummy_hash)    # the same work whether or not the person exists
        if not (u and u.get("pw_hash") and ok):
            pw_throttle.fail(key)
            ip_throttle.fail(ip)
            raise HTTPException(401, "wrong email or password")
        pw_throttle.ok(key)
        signed_in(response, request, u["id"])
        refresh_soon(u["id"])
        return {"signed_in": True, "email": u["email"]}

    class ResetIn(BaseModel):
        token: str
        new: str

    @front.post("/auth/password/reset")
    async def auth_password_reset(b: ResetIn, request: Request, response: Response):
        """Forgot the Coinpile password: a Firefly III Personal Access Token proves who you are again; then the new password is set.
        Every other browser is signed out, and the token becomes the one Coinpile reads Firefly III with."""
        ip_throttle.check(client_ip(request))
        if not ff_url:
            raise HTTPException(503, "Coinpile does not know your Firefly III address yet (set FIREFLY_URL)")
        try:
            check_password_strength(b.new)
        except ValueError as e:
            raise HTTPException(422, str(e))
        tok = b.token.strip()
        if len(tok) < 20 or len(tok) > 5000:
            raise HTTPException(422, "that is not a Firefly III Personal Access Token (it is a very long string)")
        try:
            me_ = await run_in_threadpool(lambda: firefly.Firefly(ff_url, tok, firefly_transport).me())
        except firefly.TokenRefused:
            ip_throttle.fail(client_ip(request))
            raise HTTPException(401, "Firefly III did not accept that token")
        except firefly.FireflyError as e:
            raise HTTPException(502, str(e))
        users.save(me_["id"], me_["email"], "pat", tok)
        users.ensure_handle(me_["id"])
        users.set_password(me_["id"], await run_in_threadpool(slow_hash, hash_password, b.new))
        try:                                                   # someone who had your session may have made API tokens: a reset ends those too
            with person_db(me_["id"]).tx() as c:
                c.execute("UPDATE api_tokens SET revoked=1 WHERE revoked=0")
        except KeyError:
            pass
        pw_throttle.ok("pw:" + (me_["email"] or "").lower())
        signed_in(response, request, me_["id"])
        refresh_soon(me_["id"])
        return {"signed_in": True, "email": me_["email"], "password_set": True}

    @front.get("/auth/robinhood/callback")
    async def rh_callback(request: Request, code: str = "", state: str = "", error: str = ""):
        """Back from Robinhood's sign-in. Only the browser (and person) that started it can finish it."""
        with oauth_lock:
            got = rh_pending.pop(state, None) if state else None
        uid = users.session_user(request.cookies.get(COOKIE))
        if error or not got or got[0] != uid:
            return RedirectResponse("/?robinhood=failed#settings", 302)
        try:
            db = person_db(uid)
        except KeyError:
            return RedirectResponse("/?robinhood=failed#settings", 302)
        _, verifier, _, meta, client = got
        try:
            d = await run_in_threadpool(mcp.exchange, meta, client,
                                        {"grant_type": "authorization_code", "code": code, "redirect_uri": meta["redirect"], "code_verifier": verifier}, mcp_transport)
        except (mcp.MCPError, ValueError, KeyError):
            return RedirectResponse("/?robinhood=refused#settings", 302)
        cfg.set_value(db, "rh_mcp_client", client)
        cfg.set_value(db, "rh_mcp_meta", json.dumps(meta))
        robinhood.Robinhood(db, "", None, "", mcp_transport).save_tokens(d)
        refresh_soon(uid)
        return RedirectResponse("/?robinhood=connected#investments", 302)

    @front.get("/auth/firefly")
    def auth_firefly(request: Request):
        """Sign in with Firefly III: off to Firefly III's own login page, and back."""
        if not (client_id and ff_url):
            raise HTTPException(404, "signing in through Firefly III is not set up (FIREFLY_CLIENT_ID)")
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(24)
        now = time.time()
        with oauth_lock:
            for k in [k for k, (_, t) in pending_oauth.items() if now - t > 900]:
                del pending_oauth[k]
            if len(pending_oauth) >= 1000:
                raise HTTPException(429, "too many sign-ins in progress; try again in a few minutes")
            pending_oauth[state] = (verifier, now)
        back = (my_url or str(request.base_url).rstrip("/")) + "/auth/callback"
        q = urlencode({"client_id": client_id, "redirect_uri": back, "response_type": "code", "scope": "", "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})
        resp = RedirectResponse(f"{ff_public}/oauth/authorize?{q}", 302)
        resp.set_cookie("coinpile_oauth", sha(state), max_age=900, httponly=True, samesite="lax", secure=is_secure(request), path="/auth/")   # only this browser can finish this sign-in
        return resp

    @front.get("/auth/callback")
    async def auth_callback(request: Request, code: str = "", state: str = "", error: str = ""):
        if error or not state or not hmac.compare_digest(request.cookies.get("coinpile_oauth", ""), sha(state)):
            return RedirectResponse("/?signin=failed", 302)             # not started in this browser: someone may be trying to sign you in as them
        with oauth_lock:
            got = pending_oauth.pop(state, None)
        if not got:
            return RedirectResponse("/?signin=failed", 302)
        verifier, _ = got
        back = (my_url or str(request.base_url).rstrip("/")) + "/auth/callback"
        try:
            r = await run_in_threadpool(http.post, f"{ff_url}/oauth/token", data={"grant_type": "authorization_code", "client_id": client_id, "redirect_uri": back, "code_verifier": verifier, "code": code})
        except httpx.HTTPError:
            return RedirectResponse("/?signin=unreachable", 302)
        if r.status_code != 200:
            return RedirectResponse("/?signin=refused", 302)
        d = r.json()
        try:
            me_ = await run_in_threadpool(lambda: firefly.Firefly(ff_url, d["access_token"], firefly_transport).me())
        except firefly.FireflyError:
            return RedirectResponse("/?signin=refused", 302)
        users.save(me_["id"], me_["email"], "oauth", d["access_token"], d.get("refresh_token"), time.time() + int(d.get("expires_in", 3600)))
        users.ensure_handle(me_["id"])
        resp = RedirectResponse("/", 302)
        resp.delete_cookie("coinpile_oauth", path="/auth/")
        signed_in(resp, request, me_["id"])
        refresh_soon(me_["id"])
        return resp

    @front.post("/auth/logout")
    def logout(request: Request, response: Response):
        users.end_session(request.cookies.get(COOKIE))
        response.delete_cookie(COOKIE, path="/")
        return {"signed_out": True}

    @front.get("/")
    def index():
        return FileResponse(ui / "index.html", headers={"Cache-Control": "no-cache"})

    app = Dispatch(front, users, person_app, my_url)
    app.refresh = refresh            # for tests
    app.users = users
    app.person_db = person_db
    app.scan_drop_folder = scan_drop_folder
    return app


def main():
    return create_app(background=True)
