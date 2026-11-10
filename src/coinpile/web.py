"""Coinpile: a simple, friendly view of your Firefly III, with an agent you can ask about your money.

Sign in with your Firefly III account (its "Sign in with Firefly III" OAuth page, or a Personal Access Token). Each Firefly III user gets their own
local copy and their own Coinpile data (goals, notes, chats). Firefly III stays the source of truth: Coinpile reads it every few minutes.
The Plaid connector, if you use one, can be driven from here too (see your banks, pull now, link a bank)."""
import base64
import hashlib
import json
import os
import re
import secrets
import threading
import time
from datetime import date
from pathlib import Path
from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from coinpile import agent, api_money, api_plan, connector as conn, feed, firefly, notify, settings as cfg
from coinpile.db import DB
from coinpile.oplog import log

SESSION_DAYS = 30
COOKIE = "coinpile"
USER_KEYS = {"refresh_minutes", "connector_url", "connector_name", "connector_token", "caldav_url", "caldav_user", "caldav_password",
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
        f.write_text(Fernet.generate_key().decode())
        f.chmod(0o600)
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
        self.db.conn.row_factory = sqlite3.Row
        self.db.lock = threading.RLock()
        with self.db.tx() as c:
            c.executescript("""CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY, email TEXT, kind TEXT, token_enc TEXT, refresh_enc TEXT, expires REAL, created TEXT, last_seen TEXT, problem TEXT);
                               CREATE TABLE IF NOT EXISTS sessions(hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, expires REAL NOT NULL);""")

    def save(self, uid: str, email: str, kind: str, token: str, refresh: str | None = None, expires: float | None = None) -> None:
        from coinpile import crypto
        with self.db.tx() as c:
            c.execute("""INSERT INTO users(id,email,kind,token_enc,refresh_enc,expires,created,last_seen,problem) VALUES(?,?,?,?,?,?,datetime('now'),datetime('now'),NULL)
                         ON CONFLICT(id) DO UPDATE SET email=excluded.email,kind=excluded.kind,token_enc=excluded.token_enc,refresh_enc=excluded.refresh_enc,
                         expires=excluded.expires,last_seen=excluded.last_seen,problem=NULL""",
                      (uid, email, kind, crypto.encrypt(token), crypto.encrypt(refresh) if refresh else None, expires))

    def get(self, uid: str) -> dict | None:
        return self.db.one("SELECT * FROM users WHERE id=?", (uid,))

    def all(self) -> list[dict]:
        return self.db.all("SELECT * FROM users WHERE token_enc IS NOT NULL")

    def problem(self, uid: str, text: str | None) -> None:
        with self.db.tx() as c:
            c.execute("UPDATE users SET problem=? WHERE id=?", (text, uid))

    def forget(self, uid: str) -> None:
        with self.db.tx() as c:
            c.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
            c.execute("DELETE FROM users WHERE id=?", (uid,))

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

    def __init__(self, front, users: Users, person_app):
        self.front, self.users, self.person_app = front, users, person_app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] == "http" and (path.startswith("/api/") or path == "/subscriptions.ics"):
            uid = self.users.session_user(cookie_of(scope))
            if not uid:
                return await JSONResponse({"detail": "sign in first"}, 401)(scope, receive, send)
            if scope["method"] not in ("GET", "HEAD") and not same_origin(scope):
                return await JSONResponse({"detail": "that request did not come from this site"}, 403)(scope, receive, send)
            return await self.person_app(uid)(scope, receive, send)
        return await self.front(scope, receive, send)


def same_origin(scope) -> bool:
    """Changes must come from Coinpile's own page (the cookie is SameSite=Lax as well; this also stops other sites' forms)."""
    h = {k.decode().lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
    origin = h.get("origin")
    if not origin:
        return True                                   # not a browser cross-site request (curl, tests): those carry no cookie of ours anyway unless sent on purpose
    host = h.get("x-forwarded-host") or h.get("host", "")
    return origin.split("://", 1)[-1].rstrip("/") == host


def create_app(firefly_url: str | None = None, data_dir: str | None = None, firefly_transport=None, connector_transport=None, llm_transport=None,
               caldav_transport=None, notify_transport=None, today=None, background: bool = False, client_id: str | None = None,
               public_firefly_url: str | None = None, public_url: str | None = None):
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

    made = threading.RLock()                 # one database object (one connection, one lock) per person, however many threads ask at once

    def person_db(uid: str) -> DB:
        with made:
            if uid not in dbs:
                safe = re.sub(r"[^A-Za-z0-9_-]", "_", uid)
                dbs[uid] = DB(os.path.join(data_dir, "people", f"{safe}.db"))
            return dbs[uid]

    # ---------------- Firefly III tokens
    def token_for(uid: str) -> str:
        from coinpile import crypto
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
        db = person_db(uid)
        try:
            out = firefly.refresh(db, ff_for(uid), today(), full)
            cfg.set_value(db, "ff_last_error", "")
            users.problem(uid, None)
            try:
                if cfg.get(db, "notify_url"):
                    out["notify"] = notify.run(db, today(), (users.get(uid) or {}).get("email", ""), notify_transport)
            except Exception:
                pass
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

    def loop():
        while True:
            time.sleep(60)
            for u in users.all():
                if u.get("problem"):
                    continue
                try:
                    if due(u["id"]):
                        refresh(u["id"])
                except Exception:
                    pass

    # ---------------- each person's own app
    def person_app(uid: str) -> FastAPI:
        with made:
            if uid not in apps:
                apps[uid] = build_person_app(uid)
            return apps[uid]

    def build_person_app(uid: str) -> FastAPI:
        db = person_db(uid)
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        who = lambda: (users.get(uid) or {}).get("email", "")
        ctx = api_money.make_ctx(db, lambda request: None, today, name=who(), caldav_transport=caldav_transport, shared_changed=lambda *a, **k: None,
                                 link_flush=lambda: 0, link_people=lambda: [])
        ctx.llm_transport = llm_transport
        api_money.register(app, ctx)
        api_plan.register(app, ctx)
        agent.register(app, ctx)

        @app.get("/api/status")
        def status():
            u = users.get(uid) or {}
            return {"firefly": {"address": ff_public, "email": u.get("email"), "signed_in_with": u.get("kind"), "last_refresh": cfg.get(db, "ff_last_refresh") or None,
                                "last_error": cfg.get(db, "ff_last_error") or None, "refresh_minutes": int(float(cfg.get(db, "refresh_minutes") or 10)),
                                "accounts": db.one("SELECT COUNT(*) n FROM accounts WHERE source='firefly'")["n"], "transactions": db.one("SELECT COUNT(*) n FROM transactions WHERE source='firefly'")["n"]},
                    "connector": {"configured": bool(cfg.get(db, "connector_url") and cfg.get(db, "connector_name") and cfg.get(db, "connector_token"))},
                    "caldav_configured": bool(cfg.get(db, "caldav_url"))}

        @app.post("/api/refresh")
        async def refresh_now(full: bool = False):
            try:
                return await run_in_threadpool(refresh, uid, full)
            except firefly.TokenRefused as e:
                raise HTTPException(409, str(e))
            except firefly.FireflyError as e:
                raise HTTPException(502, str(e))

        @app.get("/api/links")                     # the old screens ask about family links; Coinpile has none
        def links_none():
            return {"me": who(), "links": [], "waiting": 0}

        @app.get("/api/sharing")
        def sharing_none():
            return []

        @app.get("/api/piggy-banks")
        def piggy_banks():
            return firefly.piggies(db)

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
                for k, v in body.items():
                    cfg.put(db, k, v)
            except ValueError as e:
                raise HTTPException(422, str(e))
            return settings_get()

        # calendar and notifications, as in the old app
        @app.post("/api/subscriptions/export/caldav")
        def subs_caldav():
            url, user, pw = (cfg.get(db, k) for k in ("caldav_url", "caldav_user", "caldav_password"))
            if not url:
                raise HTTPException(503, "CalDAV is not set up yet (Settings, Calendar)")
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
                r = httpx.Client(timeout=15, auth=(user, pw), transport=caldav_transport).request("PROPFIND", url, headers={"Depth": "0"})
            except httpx.HTTPError as e:
                return {"ok": False, "detail": type(e).__name__}
            return {"ok": r.status_code in (200, 207), "status": r.status_code}

        @app.get("/api/notify/status")
        def notify_status():
            last = db.one("SELECT MAX(sent) s, COUNT(*) n FROM notified")
            return {"configured": bool(cfg.get(db, "notify_url")), "main": bool(cfg.get(db, "notify_url")), "cds": False,
                    "min_severity": cfg.get(db, "notify_min_severity") or "warning", "last_sent": last["s"], "tracked": last["n"]}

        @app.post("/api/notify/test")
        def notify_test():
            url = cfg.get(db, "notify_url")
            if not url:
                raise HTTPException(503, "Add your Mattermost webhook address first")
            try:
                notify.post(url, {"text": ":coin: **Coinpile**: notifications are working.", "username": "Coinpile", "icon_emoji": ":coin:"}, notify_transport)
            except httpx.HTTPStatusError as e:
                return {"ok": False, "detail": f"the webhook answered {e.response.status_code}"}
            except httpx.HTTPError as e:
                return {"ok": False, "detail": f"could not reach it ({type(e).__name__})"}
            return {"ok": True}

        @app.post("/api/notify/run")
        def notify_run():
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
                return fn()
            except conn.ConnectorError as e:
                raise HTTPException(502, str(e))

        @app.get("/api/banks")
        async def banks():
            return await run_in_threadpool(conn_call, lambda: connector().banks())

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
            users.forget(uid)
            apps.pop(uid, None)
            d = dbs.pop(uid, None)
            if d:
                d.conn.close()
            safe = re.sub(r"[^A-Za-z0-9_-]", "_", uid)
            try:
                os.remove(os.path.join(data_dir, "people", f"{safe}.db"))
            except FileNotFoundError:
                pass
            return {"forgotten": True}

        return app

    # ---------------- signing in
    def signed_in(response: Response, request: Request, uid: str) -> None:
        tok = users.new_session(uid)
        secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
        response.set_cookie(COOKIE, tok, max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax", secure=secure, path="/")

    @front.get("/health")
    def health():
        return {"ok": True, "firefly_configured": bool(ff_url)}

    @front.get("/auth/me")
    def me(request: Request):
        uid = users.session_user(request.cookies.get(COOKIE))
        u = users.get(uid) if uid else None
        return {"signed_in": bool(u), "email": (u or {}).get("email"), "oauth": bool(client_id and ff_url), "firefly": ff_public, "problem": (u or {}).get("problem"),
                "configured": bool(ff_url)}

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
        signed_in(response, request, me_["id"])
        refresh_soon(me_["id"])
        log.info("someone signed in with a token")
        return {"signed_in": True, "email": me_["email"]}

    @front.get("/auth/firefly")
    def auth_firefly(request: Request):
        """Sign in with Firefly III: off to Firefly III's own login page, and back."""
        if not (client_id and ff_url):
            raise HTTPException(404, "signing in through Firefly III is not set up (FIREFLY_CLIENT_ID)")
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(24)
        now = time.time()
        for k in [k for k, (_, t) in pending_oauth.items() if now - t > 900]:
            del pending_oauth[k]
        pending_oauth[state] = (verifier, now)
        back = (my_url or str(request.base_url).rstrip("/")) + "/auth/callback"
        q = urlencode({"client_id": client_id, "redirect_uri": back, "response_type": "code", "scope": "", "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})
        return RedirectResponse(f"{ff_public}/oauth/authorize?{q}", 302)

    @front.get("/auth/callback")
    async def auth_callback(request: Request, code: str = "", state: str = "", error: str = ""):
        if error or state not in pending_oauth:
            return RedirectResponse("/?signin=failed", 302)
        verifier, _ = pending_oauth.pop(state)
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
        resp = RedirectResponse("/", 302)
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

    app = Dispatch(front, users, person_app)
    app.refresh = refresh            # for tests
    app.users = users
    app.person_db = person_db
    return app


def main():
    return create_app(background=True)
