"""Defence in depth: outbound calls cannot starve the server, typed addresses cannot be rebound, changes must come from this site (scheme too),
link messages cannot speak for someone else, and Forget me stays forgotten."""
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from coinpile import links, netguard, web
from coinpile.db import DB
from tests.test_coinpile import ALICE, BOB, TODAY, signed_in, world


# ---------------------------------------------------------------- A. one person cannot fill the worker threads
def test_outbound_calls_are_capped_per_person_and_the_extra_one_gets_429(tmp_path):
    release, inside, lock = threading.Event(), [0], threading.Lock()

    def handler(r: httpx.Request):
        if r.url.host == "slow.example":                    # a server that does not answer (until the test lets it)
            with lock:
                inside[0] += 1
            release.wait(10)
        return httpx.Response(207)

    a = web.create_app(firefly_url="https://firefly.example.com", data_dir=str(tmp_path), firefly_transport=world().transport(),
                       caldav_transport=httpx.MockTransport(handler), llm_transport=httpx.MockTransport(handler), today=lambda: TODAY)
    alice, bob = signed_in(a, ALICE), signed_in(a, BOB)
    alice.put("/api/settings", json={"caldav_url": "https://slow.example/cal"})
    bob.put("/api/settings", json={"caldav_url": "https://fast.example/cal"})
    results = []
    threads = [threading.Thread(target=lambda: results.append(alice.post("/api/settings/test-caldav").status_code)) for _ in range(netguard.PER_PERSON)]
    for t in threads:
        t.start()
    try:
        for _ in range(200):
            if inside[0] == netguard.PER_PERSON:
                break
            time.sleep(0.02)
        assert inside[0] == netguard.PER_PERSON
        t0 = time.time()
        r = alice.post("/api/settings/test-caldav")
        assert r.status_code == 429 and "busy" in r.json()["detail"] and time.time() - t0 < 5
        r = alice.post("/api/agent/models", json={"kind": "openai", "base_url": "https://slow.example/v1"})     # the same slots cover the model server
        assert r.status_code == 429
        assert bob.post("/api/settings/test-caldav").json() == {"ok": True, "status": 207}                     # everyone else is unaffected
    finally:
        release.set()
        for t in threads:
            t.join(10)
    assert results == [200] * netguard.PER_PERSON
    assert alice.post("/api/settings/test-caldav").status_code == 200                                         # the slots come back


def test_the_agent_chat_timeout_is_two_minutes():
    from coinpile import agent
    assert agent.CHAT_TIMEOUT <= 120


# ---------------------------------------------------------------- B. the connection goes to the address that was checked
class _Echo(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"host": self.headers.get("Host")}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def test_the_guarded_client_resolves_once_and_dials_the_checked_address(monkeypatch):
    srv = HTTPServer(("127.0.0.1", 0), _Echo)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    lookups = []

    def resolve(host, p):
        lookups.append(host)
        return ["127.0.0.1"]
    monkeypatch.setattr(netguard, "_resolve", resolve)
    try:
        monkeypatch.setenv("ALLOW_LOOPBACK_URLS", "1")
        with netguard.client(timeout=5) as c:
            r = c.get(f"http://pinned.test:{port}/x")       # not a real name: it can only work by dialling the address the guard resolved
        assert r.status_code == 200 and r.json()["host"] == f"pinned.test:{port}"           # the Host header keeps the name
        assert lookups == ["pinned.test"]                                                    # one lookup, used for both the check and the connection
        monkeypatch.delenv("ALLOW_LOOPBACK_URLS")
        lookups.clear()
        with netguard.client(timeout=5) as c, pytest.raises(netguard.Blocked):
            c.get(f"http://rebound.test:{port}/x")              # the name now answers loopback: refused at connect time
        assert lookups == ["rebound.test"]
    finally:
        srv.shutdown()


def test_a_name_that_rebinds_between_lookups_is_still_refused(monkeypatch):
    answers = iter([["203.0.113.9"], ["127.0.0.1"], ["127.0.0.1"]])         # first answer looks public, the next one is the server itself
    monkeypatch.setattr(netguard, "_resolve", lambda host, port: next(answers))
    netguard.check("http://rebind.test/")                                    # a pre-check alone would have passed
    with netguard.client(timeout=5) as c, pytest.raises(netguard.Blocked):
        c.get("http://rebind.test/")


def test_a_stand_in_transport_still_skips_the_guard(monkeypatch):
    monkeypatch.setattr(netguard, "_resolve", lambda host, port: pytest.fail("no DNS with a stand-in transport"))
    with netguard.client(httpx.MockTransport(lambda r: httpx.Response(204))) as c:
        assert c.get("http://127.0.0.1/").status_code == 204


# ---------------------------------------------------------------- C. same origin means the scheme too
def _scope(origin, host="coinpile.example", scheme="http", **extra):
    hdrs = [(b"host", host.encode()), (b"origin", origin.encode())] + [(k.replace("_", "-").encode(), v.encode()) for k, v in extra.items()]
    return {"type": "http", "scheme": scheme, "headers": hdrs}


def test_same_origin_compares_the_scheme_when_it_is_known():
    so = web.same_origin
    assert so(_scope("https://coinpile.example", x_forwarded_proto="https"))
    assert not so(_scope("http://coinpile.example", x_forwarded_proto="https"))         # a plain-http page cannot post to the https site
    assert not so(_scope("http://coinpile.example", scheme="https"))
    assert so(_scope("https://coinpile.example"), "https://coinpile.example")
    assert not so(_scope("http://coinpile.example"), "https://coinpile.example")
    assert so(_scope("http://coinpile.example")) and so(_scope("https://coinpile.example"))    # scheme unknown (a TLS proxy that says nothing): host decides
    assert not so(_scope("https://evil.example", x_forwarded_proto="https"))
    assert not so(_scope("null"))
    assert so({"type": "http", "scheme": "http", "headers": [(b"host", b"coinpile.example")]})          # no Origin at all: not a cross-site browser request


def test_a_change_from_the_http_page_of_an_https_coinpile_is_refused(tmp_path):
    a = web.create_app(firefly_url="https://firefly.example.com", data_dir=str(tmp_path), firefly_transport=world().transport(), today=lambda: TODAY,
                       public_url="https://testserver")
    c = TestClient(a, base_url="https://testserver")            # the session cookie is Secure here
    assert c.post("/auth/token", json={"token": ALICE}).status_code == 200
    assert c.post("/api/notes", json={"title": "x"}, headers={"Origin": "http://testserver"}).status_code == 403
    assert c.post("/api/notes", json={"title": "x"}, headers={"Origin": "https://testserver"}).status_code == 200


# ---------------------------------------------------------------- D. a link message speaks only for its sender
def _goal_msg(sender, sid, members, rev=1, name="Trip", notes=()):
    return {"shared_id": sid, "rev": rev, "edited_by": sender, "members": members, "goal": {"name": name, "target": 3000, "priority": 2, "status": "active"},
            "contrib": 10, "notes": list(notes)}


def test_link_messages_cannot_forge_authors_delete_others_notes_or_touch_goals_they_are_not_in():
    db, sid = DB(":memory:"), "s" * 32
    with db.tx() as c:
        for p in ("bob", "carol"):
            c.execute("INSERT INTO links(peer,status,created) VALUES(?,?,?)", (p, "active", "2026-01-01"))
    gid = links.receive(db, "alice", "bob", "goal", _goal_msg("bob", sid, ["alice", "bob"]))["goal_id"]
    links.accept_goal(db, "alice", gid)
    mine = links.add_note(db, "alice", gid, "my own note")["id"]
    ts = "2026-11-10T00:00:00+00:00"
    links.receive(db, "alice", "bob", "goal", _goal_msg("bob", sid, ["alice", "bob"], rev=2, notes=[
        {"id": "n-forged", "author": "alice", "ts": ts, "text": "alice says send me money"},          # pretends to be the receiver ("You")
        {"id": "n-bob", "author": "bob", "ts": ts, "text": "hello from bob"},
        {"id": mine, "author": "alice", "ts": ts, "text": "", "deleted": True},                       # tries to delete alice's note
        {"id": mine, "author": "bob", "ts": ts, "text": "", "deleted": True}]))
    notes = {n["id"]: n for n in db.all("SELECT * FROM goal_notes WHERE goal_id=?", (gid,))}
    assert notes["n-bob"]["author"] == "bob" and notes["n-bob"]["text"] == "hello from bob"
    assert "n-forged" not in notes or notes["n-forged"]["author"] == "bob"
    assert not any(n["author"] == "alice" for k, n in notes.items() if k != mine)
    assert notes[mine]["author"] == "alice" and notes[mine]["deleted"] == 0 and notes[mine]["text"] == "my own note"
    # bob can remove his own note
    links.receive(db, "alice", "bob", "goal", _goal_msg("bob", sid, ["alice", "bob"], rev=3, notes=[{"id": "n-bob", "author": "bob", "ts": ts, "text": "", "deleted": True}]))
    assert db.one("SELECT deleted FROM goal_notes WHERE id='n-bob'")["deleted"] == 1
    # carol is linked with alice but not in this goal: knowing its shared_id changes nothing
    r = links.receive(db, "alice", "carol", "goal", _goal_msg("carol", sid, ["alice", "bob", "carol"], rev=99, name="Hijacked"))
    assert "ignored" in r
    g = db.one("SELECT * FROM goals WHERE id=?", (gid,))
    assert g["name"] == "Trip" and json.loads(g["members"]) == ["bob"]
    assert not db.one("SELECT 1 FROM goal_partners WHERE goal_id=? AND peer='carol'", (gid,))
    links.receive(db, "alice", "carol", "leave", {"shared_id": sid})
    g = db.one("SELECT * FROM goals WHERE id=?", (gid,))
    assert g["shared_id"] == sid and json.loads(g["members"]) == ["bob"]
    # a real member leaving still works
    links.receive(db, "alice", "bob", "leave", {"shared_id": sid})
    assert db.one("SELECT shared_id FROM goals WHERE id=?", (gid,))["shared_id"] is None


# ---------------------------------------------------------------- E. Forget me stays forgotten
def test_forget_is_not_undone_by_a_late_background_refresh(tmp_path):
    a = web.create_app(firefly_url="https://firefly.example.com", data_dir=str(tmp_path), firefly_transport=world().transport(), today=lambda: TODAY)
    c = signed_in(a, ALICE)
    f = Path(tmp_path) / "people" / "1.db"
    assert f.exists()
    assert c.post("/api/forget").json() == {"forgotten": True}
    assert not f.exists()
    assert a.refresh("1") == {"forgotten": True}                 # what refresh_soon, the loop and a webhook end up calling
    with pytest.raises(KeyError):
        a.person_db("1")
    assert not f.exists()
    assert not list((Path(tmp_path) / "people").glob("1.db*"))
    # signing in again later starts afresh
    signed_in(a, ALICE)
    assert f.exists()


# ---------------------------------------------------------------- F. third-party actions are pinned
def test_github_actions_are_pinned_to_full_commit_shas():
    wf = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "docker.yml").read_text()
    uses = re.findall(r"uses:\s*(\S+)", wf)
    assert uses and all(re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", u) for u in uses), uses
