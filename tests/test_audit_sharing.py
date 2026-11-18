"""Edge cases an audit of sign-in, isolation and sharing found: one handle per person, Forget me that cannot take the server down, numbers
from another person that cannot break my pages, offers that are not mine until I say yes, and no error pages on bad input."""
import json
import threading
import time
from datetime import date

import pytest
from fastapi.testclient import TestClient

from coinpile import feed, importers, links, netguard, plan, webhooks
from coinpile.db import DB, Closed
from coinpile.security import Throttle
from tests.fake_firefly import FakeFirefly
from tests.test_coinpile import ALICE, BOB, TODAY, app, signed_in, world
from tests.test_joint_accounts import two_fireflies, wait


def linked(tmp_path, ff=None):
    a = app(tmp_path, ff or world())
    alice, bob = signed_in(a, ALICE), signed_in(a, BOB)
    alice.post("/api/links", json={"peer": "bob"}); bob.post("/api/links/alice/accept", json={})
    assert wait(lambda: alice.get("/api/links").json()["links"][0]["status"] == "active")
    return a, alice, bob


def soft(c):
    s = TestClient(c.app, raise_server_exceptions=False)
    s.cookies = c.cookies
    return s


# ------------------------------------------------------------------ one handle, one person
def test_two_people_never_get_the_same_handle(tmp_path):
    toks = {n: f"tok-{n}-" + "z" * 30 for n in ("mallory", "bob", "bob9")}
    ff = FakeFirefly({toks["bob"]: {"id": "3", "email": "bob@first.example"}, toks["mallory"]: {"id": "7", "email": "bob-9@evil.example"}, toks["bob9"]: {"id": "9", "email": "bob@other.example"}})
    a = app(tmp_path, ff)
    for n in ("bob", "mallory", "bob9"):                       # the third would have been given bob-9 too (bob is taken, their Firefly III id is 9)
        signed_in(a, toks[n])
    handles = a.users.handles()
    assert len(handles) == 3 == len(set(handles)) and {"bob", "bob-9"} <= set(handles)
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):               # and the database itself refuses a second
        with a.users.db.tx() as c:
            c.execute("UPDATE users SET handle='bob' WHERE handle='bob-9'")


# ------------------------------------------------------------------ Forget me
def test_a_closed_database_says_so_instead_of_crashing():
    db = DB(":memory:")
    stop, errors = threading.Event(), []

    def reader():
        while not stop.is_set():
            try:
                db.all("SELECT * FROM accounts")
            except Closed:
                return
            except Exception as e:                             # pragma: no cover
                errors.append(e)
                return
    ts = [threading.Thread(target=reader) for _ in range(8)]
    for t in ts:
        t.start()
    time.sleep(0.05)
    db.close(); db.close()
    stop.set()
    for t in ts:
        t.join(5)
    assert not errors
    with pytest.raises(Closed):
        db.one("SELECT 1")
    with pytest.raises(Closed):
        with db.tx():
            pass


def test_forget_me_only_ends_that_person(tmp_path):
    a, alice, bob = linked(tmp_path)
    assert alice.post("/api/forget").status_code == 200
    assert soft(alice).get("/api/overview").status_code == 401 and bob.get("/api/overview").status_code == 200
    assert wait(lambda: not bob.get("/api/links").json()["links"])


# ------------------------------------------------------------------ numbers from someone else
BAD = [float("inf"), float("-inf"), float("nan"), "1e999", "Infinity", 1e300, None, "x", [1], {"a": 1}, True]


def peer_db():
    db = DB(":memory:")
    with db.tx() as c:
        c.execute("INSERT INTO links(peer,status) VALUES('bob','active')")
        c.execute("INSERT INTO accounts(id,source,institution,name,kind,balance) VALUES('mine','firefly','Bank','Joint','cash',100)")
        c.execute("INSERT INTO joint_accounts(account_id,peer,peer_account,my_share,state) VALUES('mine','bob','b1',50,'active')")
    return db


@pytest.mark.parametrize("bad", BAD)
def test_nothing_a_linked_person_sends_breaks_my_pages(bad):
    db = peer_db()
    goal = {"shared_id": "s" * 12, "members": ["alice", "bob"], "rev": 1, "contrib": 0, "goal": {"name": "Trip", "target": 100}}
    msgs = [("goal", {**goal, "goal": {"name": "Trip", "target": bad}}), ("goal", {**goal, "contrib": bad}), ("goal", {**goal, "rev": bad if bad is not True else 10 ** 30}),
            ("goal", {**goal, "members": ["alice", "bob", bad]}), ("goal", {**goal, "goal": {"name": "Trip", "target": 100, "deadline": "9999-99-99"}, "shared_id": "t" * 12}),
            ("acct", {"ref": "a1", "name": "Savings", "balance": bad}), ("joint_stats", {"ref": "b1", "to": "mine", "balance": bad, "in_by_month": {}}),
            ("joint_stats", {"ref": "b1", "to": "mine", "balance": 5, "in_by_month": {"2026-11": bad}}), ("joint_stats", {"ref": "b1", "to": "mine", "balance": 5, "in_by_month": [bad]}),
            ("joint_offer", {"ref": "b2", "name": "x", "your_share": bad}), ("unshare", bad), (bad, {})]
    for kind, p in msgs:
        r = links.receive(db, "alice", "bob", kind, p)
        assert isinstance(r, dict)
    json.dumps(plan.goals_view(db, TODAY), allow_nan=False)                         # every page that adds these up still answers
    json.dumps(links.joint_list(db, TODAY), allow_nan=False)
    json.dumps(links.sharing_overview(db), allow_nan=False)
    assert all(g["deadline"] is None for g in db.all("SELECT deadline FROM goals"))


@pytest.mark.parametrize("path,body", [("/api/goals", '{"name": "Boat", "target": 1e999}'), ("/api/accounts", '{"name": "Jar", "kind": "cash", "balance": 1e999}'),
                                       ("/api/accounts", '{"name": "Jar", "kind": "cash", "balance": NaN}'), ("/api/joint", '{"account_id": "ff:1", "peer": "bob", "my_share": Infinity}')])
def test_an_infinite_number_in_a_request_is_refused(tmp_path, path, body):
    c = soft(signed_in(app(tmp_path, world())))
    r = c.post(path, content=body, headers={"content-type": "application/json"})
    assert r.status_code == 422, r.text
    assert c.get("/api/goals").status_code == 200 and c.get("/api/accounts").status_code == 200


@pytest.mark.parametrize("method,path", [("DELETE", "/api/joint/{n}"), ("POST", "/api/joint/{n}/accept"), ("POST", "/api/goals/{n}/share"), ("POST", "/api/goals/{n}/accept"),
                                         ("DELETE", "/api/tokens/{n}"), ("DELETE", "/api/webhooks/{n}")])
def test_an_id_no_row_can_have_is_not_an_error_page(tmp_path, method, path):
    c = soft(signed_in(app(tmp_path, world())))
    r = c.request(method, path.format(n="9" * 30), json={"account_id": "ff:1", "peers": ["bob"]})
    assert r.status_code in (404, 422), r.status_code


def test_settings_and_addresses_that_cannot_be_used_are_refused(tmp_path):
    c = soft(signed_in(app(tmp_path, world())))
    assert c.put("/api/settings", json={"refresh_minutes": "inf"}).status_code == 422
    assert c.post("/api/webhooks", json={"url": "http://hooks.example.com:abc/x", "events": ["alert.new"]}).status_code == 422


# ------------------------------------------------------------------ shared goals
def offer(alice, bob, name="Boat", target=900):
    gid = bob.post("/api/goals", json={"name": name, "target": target}).json()["id"]
    assert bob.post(f"/api/goals/{gid}/share", json={"peers": ["alice"]}).status_code == 200
    assert wait(lambda: alice.get("/api/requests").json()["goals"])
    return gid


def test_an_offer_i_never_answered_is_not_mine_and_is_not_published(tmp_path):
    a, alice, bob = linked(tmp_path)
    gid = offer(alice, bob)
    alice.put("/api/settings", json={"share": ["goals"]})
    tok = alice.post("/api/tokens", json={"name": "orb", "scopes": ["orb", "goals", "mcp"]}).json()["token"]
    H = {"Authorization": "Bearer " + tok}
    c = TestClient(alice.app)
    assert "Boat" not in c.get("/api/v1/summary", headers=H).text and "Boat" not in c.get("/api/v1/me/goals", headers=H).text
    mcp = c.post("/api/v1/mcp", headers=H, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "list_goals", "arguments": {}}}).text
    assert "Boat" not in mcp
    assert bob.post(f"/api/goals/{gid}/unshare", json={}).status_code == 200            # bob takes it back
    assert wait(lambda: not alice.get("/api/requests").json()["goals"])
    assert not [g for g in alice.get("/api/goals").json()["items"] if g["name"] == "Boat"]
    offer(alice, bob, "Car")                                                             # and the same when the link ends
    alice.delete("/api/links/bob")
    assert not [g for g in alice.get("/api/goals").json()["items"] if g["name"] == "Car"]


def test_a_person_someone_else_adds_gets_nothing_of_mine_until_i_share_with_them():
    db = DB(":memory:")
    with db.tx() as c:
        for p in ("bob", "carol"):
            c.execute("INSERT INTO links(peer,status) VALUES(?, 'active')", (p,))
        gid = c.execute("INSERT INTO goals(name,target,status,priority,created,rev) VALUES('Surprise for Carol',500,'active',2,'2026-01-01',0)").lastrowid
    links.share_goal(db, "alice", gid, ["bob"])
    sid = db.one("SELECT shared_id FROM goals WHERE id=?", (gid,))["shared_id"]
    links.add_note(db, "alice", gid, "do not tell carol")
    with db.tx() as c:
        c.execute("DELETE FROM outbox")
    # bob brings carol in
    r = links.receive(db, "alice", "bob", "goal", {"shared_id": sid, "members": ["alice", "bob", "carol"], "rev": 9, "contrib": 0, "goal": {"name": "Surprise for Carol", "target": 500}})
    assert r.get("ok")
    links.bump(db, "alice", gid)
    assert {o["peer"] for o in db.all("SELECT peer FROM outbox WHERE kind='goal'")} == {"bob"}
    view = plan.goals_view(db, TODAY)["items"][0]
    assert view["added_by_others"] == ["carol"]
    links.share_goal(db, "alice", gid, ["carol"])                                        # my own yes
    assert "carol" in {o["peer"] for o in db.all("SELECT peer FROM outbox WHERE kind='goal'")} and not plan.goals_view(db, TODAY)["items"][0]["added_by_others"]


def test_goal_offers_from_one_person_are_limited():
    db = DB(":memory:")
    with db.tx() as c:
        c.execute("INSERT INTO links(peer,status) VALUES('bob','active')")
    for i in range(links.MAX_OFFERS + 10):
        links.receive(db, "alice", "bob", "goal", {"shared_id": f"offer-{i:06d}", "members": ["alice", "bob"], "rev": 1, "contrib": 0, "goal": {"name": f"g{i}", "target": 10}})
    assert db.one("SELECT COUNT(*) n FROM goals")["n"] == links.MAX_OFFERS


# ------------------------------------------------------------------ accounts that go away, and the split
def test_an_account_that_is_gone_stops_being_shared_and_joint_on_the_other_side(tmp_path):
    ff = two_fireflies()
    a, alice, bob = linked(tmp_path, ff)
    jar = alice.post("/api/accounts", json={"name": "Cash at home", "kind": "cash", "balance": 800}).json()["id"]
    alice.put("/api/links/bob/shares", json={"kind": "account", "ref": jar, "on": True})
    alice.post("/api/joint", json={"account_id": "ff:21", "peer": "bob", "my_share": 50})
    assert wait(lambda: bob.get("/api/joint").json())
    bob.post(f"/api/joint/{bob.get('/api/joint').json()[0]['id']}/accept", json={"account_id": "ff:31"})
    assert wait(lambda: alice.get("/api/joint").json()[0]["state"] == "active" and bob.get("/api/sharing").json()[0]["from_them"])
    # one side cannot change an agreed split alone
    assert alice.put("/api/accounts/ff:21", json={"my_share": 10}).status_code == 409
    assert alice.put("/api/accounts/ff:3", json={"joint_with": "nobody", "my_share": 50}).status_code == 200 and alice.put(f"/api/accounts/{jar}", json={"nickname": "Jar"}).status_code == 200
    assert alice.delete(f"/api/accounts/{jar}").status_code == 200
    assert wait(lambda: not bob.get("/api/sharing").json()[0]["from_them"])
    ff.data["1"]["accounts"] = [x for x in ff.data["1"]["accounts"] if x["id"] != "21"]                  # alice closes the joint account in Firefly III
    assert alice.post("/api/refresh?full=true").status_code == 200
    assert wait(lambda: not bob.get("/api/joint").json())
    assert next(x for x in bob.get("/api/accounts").json() if x["id"] == "ff:31")["joint_with"] is None


@pytest.mark.parametrize("share,ok", [(50, True), (0.01, True), (99.99, True), (99.999, False), (0.001, False), (0, False), (100, False), (-5, False)])
def test_a_joint_split_is_two_usable_percentages(share, ok):
    db = peer_db()
    with db.tx() as c:
        c.execute("DELETE FROM joint_accounts")
    if ok:
        j = links.joint_offer(db, "alice", "bob", "mine", share)
        assert round(j["my_share"] + j["their_share"], 2) == 100
    else:
        with pytest.raises(links.LinkError):
            links.joint_offer(db, "alice", "bob", "mine", share)


# ------------------------------------------------------------------ CSV
def test_csv_imports_are_bounded_and_empty_files_make_nothing(tmp_path):
    c = soft(signed_in(app(tmp_path, world())))
    n = len(c.get("/api/accounts").json())
    assert c.post("/api/import/csv", content=b"").json()["added"] == 0 and len(c.get("/api/accounts").json()) == n
    assert c.post("/api/import/csv?provider=whatever", content=b"a,b\n1,2").status_code == 422
    assert c.post("/api/import/csv", content=b"a,b\n1,2,3").status_code == 422                          # a row longer than its header: refused, not a crash
    with pytest.raises(ValueError, match="more than"):
        importers._rows("a,b\n" + "1,2\n" * (importers.MAX_ROWS + 1))
    with pytest.raises(ValueError):
        importers._num("9" * 400)
    jar = c.post("/api/accounts", json={"name": "Old bank", "kind": "cash", "balance": 0}).json()["id"]
    body = "Date,Description,Amount\n" + "".join(f"2026-10-{1 + i % 28:02d},Shop {i},-{1 + i}.00\n" for i in range(3000))
    t = time.time()
    r = c.post(f"/api/import/bank-csv?account_id={jar}", content=body.encode())
    assert r.status_code == 200 and r.json()["added"] == 3000 and time.time() - t < 20


# ------------------------------------------------------------------ sign-in
def test_old_failures_are_forgotten_and_a_new_sign_in_ends_the_old_session(tmp_path):
    now = [0.0]
    t = Throttle(clock=lambda: now[0])
    for _ in range(4):
        t.fail("ip")
    now[0] += t.MAX_WAIT + 1                                    # weeks of a household's typos never add up to a lock
    for _ in range(4):
        t.fail("ip")
    assert t.wait_for("ip") == 0
    for _ in range(4):
        t.fail("ip")
    assert t.wait_for("ip") > 0
    t.fail("pw:alice@example.com|ip:1"); t.ok_prefix("pw:alice@example.com|")
    assert "pw:alice@example.com|ip:1" not in t.state
    a = app(tmp_path, world())
    c = signed_in(a, ALICE)
    old = dict(c.cookies)
    assert c.post("/auth/token", json={"token": BOB}).status_code == 200                # bob signs in in the same browser
    stale = TestClient(a)
    stale.cookies.update(old)
    assert stale.get("/api/overview").status_code == 401 and c.get("/api/overview").status_code == 200
    for _ in range(40):                                                                # wrong tokens from one place are slowed down, not sent on to Firefly III forever
        r = TestClient(a).post("/auth/token", json={"token": "wrong-" + "q" * 30})
    assert r.status_code == 429


# ------------------------------------------------------------------ small things
def test_webhook_delivery_goes_on_after_an_address_that_cannot_be_called(tmp_path):
    import httpx
    db = DB(":memory:")
    seen = []

    def h(req):
        if "bad" in str(req.url):
            raise RuntimeError("cannot call this one")
        seen.append(str(req.url))
        return httpx.Response(200)
    from coinpile import crypto
    webhooks.init(db)
    with db.tx() as c:
        for i, url in enumerate(("https://bad.example/x", "https://good.example/x"), start=1):
            c.execute("INSERT INTO webhooks(id,name,url,secret_enc,events,active,created) VALUES(?,?,?,?,?,1,'2026-01-01')", (i, f"h{i}", url, crypto.encrypt("s" * 32), "alert.new"))
            c.execute("INSERT INTO webhook_outbox(hook_id,event,body,next_at,tries,done) VALUES(?, 'alert.new', '{}', 0, 0, 0)", (i,))
    out = webhooks.deliver(db, httpx.MockTransport(h))
    assert out == {"sent": 1, "failed": 1} and seen == ["https://good.example/x"]


def test_calendar_text_and_metadata_addresses():
    assert "\r" not in feed._esc("Gym\rATTENDEE:mailto:x@example.com") and "\r" not in feed._esc("a\r\nb")
    assert all(netguard.bad_ip(ip) for ip in ("168.63.129.16", "100.100.100.200", "fd00:ec2::254"))
