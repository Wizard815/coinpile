"""Links between people: two people agree to share a goal (and its notes), nothing else.

What crosses a link, and only for a goal both sides have agreed to share: its name, cost, deadline, priority, status, the notes thread, and one number per
person: how much that person has set aside for it. Accounts only when you choose: an account's name and your part of its balance when you share
it, and for a joint account both of you agreed on, the balance each side's Firefly III shows and what each moved into it per month. Never a
transaction or a private note. The gateway only relays the messages;
it keeps none of them. If the other side is offline the message waits in an outbox and goes out on the next try.
"""
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone

NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
MAX_PENDING = 10
MAX_NOTES = 200


class LinkError(ValueError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def status(db, peer: str) -> str | None:
    r = db.one("SELECT status FROM links WHERE peer=?", (peer,))
    return r["status"] if r else None


def active_peers(db) -> list[str]:
    return [r["peer"] for r in db.all("SELECT peer FROM links WHERE status='active' ORDER BY peer")]


def queue(db, peer: str, kind: str, payload: dict, ref: str = "") -> None:
    """Latest message per (peer, kind, thing) wins: a newer edit replaces one still waiting."""
    with db.tx() as c:
        c.execute("INSERT INTO outbox(peer,kind,ref,payload) VALUES(?,?,?,?) ON CONFLICT(peer,kind,ref) DO UPDATE SET payload=excluded.payload", (peer, kind, ref, json.dumps(payload)))


def flush(db, send) -> int:
    """Try everything waiting. `send(peer, kind, payload)` returns True when the gateway handed it over. Returns how many are still waiting."""
    for r in db.all("SELECT * FROM outbox"):
        try:
            ok = send(r["peer"], r["kind"], json.loads(r["payload"]))
        except Exception:
            ok = False
        if ok:
            with db.tx() as c:
                c.execute("DELETE FROM outbox WHERE peer=? AND kind=? AND ref=? AND payload=?", (r["peer"], r["kind"], r["ref"], r["payload"]))
    return db.one("SELECT COUNT(*) n FROM outbox")["n"]


# ------------------------------------------------------------------ pairing
def invite(db, me: str, peer: str) -> dict:
    peer = (peer or "").strip().lower()
    if not NAME.match(peer) or peer == me:
        raise LinkError("that is not another person's name")
    cur = status(db, peer)
    if cur == "active":
        raise LinkError("already linked")
    if cur == "pending":                                  # they already asked me: inviting back means yes
        return accept(db, peer)
    with db.tx() as c:
        c.execute("INSERT INTO links(peer,status,created) VALUES(?,?,?) ON CONFLICT(peer) DO UPDATE SET status='invited'", (peer, "invited", now()))
    queue(db, peer, "invite", {})
    return {"peer": peer, "status": "invited"}


def accept(db, peer: str) -> dict:
    if status(db, peer) != "pending":
        raise LinkError("no request from them")
    with db.tx() as c:
        c.execute("UPDATE links SET status='active' WHERE peer=?", (peer,))
    queue(db, peer, "accept", {})
    return {"peer": peer, "status": "active"}


def unlink(db, peer: str, notify: bool = True) -> None:
    """Drop the link. Goals shared with them stay here as ordinary private goals; their number and their notes' link to them go away."""
    if status(db, peer) is None:
        raise KeyError(peer)
    with db.tx() as c:
        c.execute("DELETE FROM links WHERE peer=?", (peer,))
        c.execute("DELETE FROM outbox WHERE peer=?", (peer,))
        c.execute("DELETE FROM link_shares WHERE peer=?", (peer,))
        c.execute("DELETE FROM shared_in WHERE peer=?", (peer,))
        c.execute("DELETE FROM joint_accounts WHERE peer=?", (peer,))
        c.execute("UPDATE accounts SET joint_with=NULL, my_share=NULL WHERE joint_with=?", (peer,))
        for g in c.execute("SELECT id, members FROM goals WHERE shared_id IS NOT NULL").fetchall():
            members = [m for m in json.loads(g["members"] or "[]") if m != peer]
            c.execute("DELETE FROM goal_partners WHERE goal_id=? AND peer=?", (g["id"], peer))
            if members:
                c.execute("UPDATE goals SET members=? WHERE id=?", (json.dumps(members), g["id"]))
            else:
                _make_private(c, g["id"])
    if notify:
        queue(db, peer, "unlink", {})


def _make_private(c, gid: int) -> None:
    c.execute("UPDATE goals SET shared_id=NULL, members=NULL, shared_state=NULL, sent_sig=NULL WHERE id=?", (gid,))
    c.execute("DELETE FROM goal_partners WHERE goal_id=?", (gid,))
    c.execute("DELETE FROM goal_notes WHERE goal_id=?", (gid,))


# ------------------------------------------------------------------ shared goals
def _goal(db, gid: int) -> dict:
    g = db.one("SELECT * FROM goals WHERE id=?", (gid,))
    if not g:
        raise KeyError(gid)
    return g


def my_contrib(db, gid: int) -> float:
    return round(db.one("SELECT COALESCE(SUM(amount),0) n FROM allocations WHERE goal_id=?", (gid,))["n"], 2)


def share_goal(db, me: str, gid: int, peers: list[str]) -> dict:
    g = _goal(db, gid)
    peers = sorted({p.strip().lower() for p in peers})
    if not peers:
        raise LinkError("choose who to share it with")
    for p in peers:
        if status(db, p) != "active":
            raise LinkError(f"you are not linked with {p} yet")
    members = sorted(set(json.loads(g["members"] or "[]")) | set(peers))
    with db.tx() as c:
        c.execute("UPDATE goals SET shared_id=COALESCE(shared_id,?), members=?, shared_state='active', rev=rev+1, edited_by=?, sent_sig=NULL WHERE id=?", (uuid.uuid4().hex, json.dumps(members), me, gid))
    push(db, me, gid)
    return {"id": gid, "members": members}


def stop_sharing(db, me: str, gid: int) -> None:
    """Leave a shared goal. It stays here as a private goal; the others keep theirs (without my number)."""
    g = _goal(db, gid)
    if not g["shared_id"]:
        return
    for p in json.loads(g["members"] or "[]"):
        if status(db, p) == "active":
            queue(db, p, "leave", {"shared_id": g["shared_id"]}, g["shared_id"])
    with db.tx() as c:
        _make_private(c, gid)


def _notes(db, gid: int) -> list[dict]:
    return [{"id": n["id"], "author": n["author"], "ts": n["ts"], "text": "" if n["deleted"] else n["text"], "deleted": bool(n["deleted"])}
            for n in db.all("SELECT * FROM goal_notes WHERE goal_id=? ORDER BY ts, id", (gid,))]


def add_note(db, me: str, gid: int, text: str) -> dict:
    g = _goal(db, gid)
    text = (text or "").strip()
    if not g["shared_id"] or g["shared_state"] != "active":
        raise LinkError("notes are for shared goals; a private goal has its own note field")
    if not text or len(text) > 2000:
        raise LinkError("a note is 1 to 2000 characters")
    if db.one("SELECT COUNT(*) n FROM goal_notes WHERE goal_id=?", (gid,))["n"] >= MAX_NOTES:
        raise LinkError("this goal has as many notes as it can hold")
    nid = uuid.uuid4().hex
    with db.tx() as c:
        c.execute("INSERT INTO goal_notes(id,goal_id,author,ts,text) VALUES(?,?,?,?,?)", (nid, gid, me, now(), text))
    push(db, me, gid)
    return {"id": nid}


def delete_note(db, me: str, gid: int, nid: str) -> None:
    n = db.one("SELECT * FROM goal_notes WHERE id=? AND goal_id=?", (nid, gid))
    if not n:
        raise KeyError(nid)
    if n["author"] != me:
        raise LinkError("you can only remove your own notes")
    with db.tx() as c:
        c.execute("UPDATE goal_notes SET deleted=1, text='' WHERE id=?", (nid,))
    push(db, me, gid)


def _message(db, me: str, g: dict) -> dict:
    return {"shared_id": g["shared_id"], "rev": g["rev"], "edited_by": g["edited_by"] or me, "members": sorted(set(json.loads(g["members"] or "[]")) | {me}),
            "goal": {"name": g["name"], "target": g["target"], "deadline": g["deadline"], "priority": g["priority"], "status": g["status"], "closed": g["closed"]},
            "contrib": my_contrib(db, g["id"]), "notes": _notes(db, g["id"])}


def _sig(msg: dict) -> str:
    return hashlib.sha256(json.dumps({k: msg[k] for k in ("rev", "goal", "contrib", "notes", "members")}, sort_keys=True).encode()).hexdigest()


def push(db, me: str, gid: int | None = None) -> int:
    """Queue an update for every shared goal that changed since it was last sent (or just one). Returns how many were queued."""
    n = 0
    q = "SELECT * FROM goals WHERE shared_id IS NOT NULL AND shared_state='active'" + (" AND id=?" if gid else "")
    for g in db.all(q, (gid,) if gid else ()):
        msg = _message(db, me, g)
        sig = _sig(msg)
        if sig == g["sent_sig"]:
            continue
        for p in json.loads(g["members"] or "[]"):
            if status(db, p) == "active":
                queue(db, p, "goal", msg, g["shared_id"])
        with db.tx() as c:
            c.execute("UPDATE goals SET sent_sig=? WHERE id=?", (sig, g["id"]))
        n += 1
    return n


def bump(db, me: str, gid: int) -> None:
    """I edited a shared goal's shared fields: newer revision, then send."""
    g = db.one("SELECT shared_id FROM goals WHERE id=?", (gid,))
    if g and g["shared_id"]:
        with db.tx() as c:
            c.execute("UPDATE goals SET rev=rev+1, edited_by=? WHERE id=?", (me, gid))
        push(db, me, gid)


def accept_goal(db, me: str, gid: int) -> None:
    g = _goal(db, gid)
    if g["shared_state"] != "offered":
        raise LinkError("nothing to accept")
    with db.tx() as c:
        c.execute("UPDATE goals SET shared_state='active', sent_sig=NULL WHERE id=?", (gid,))
    push(db, me, gid)


def decline_goal(db, me: str, gid: int) -> None:
    g = _goal(db, gid)
    if g["shared_state"] != "offered":
        raise LinkError("nothing to decline")
    for p in json.loads(g["members"] or "[]"):
        if status(db, p) == "active":
            queue(db, p, "leave", {"shared_id": g["shared_id"]}, g["shared_id"])
    with db.tx() as c:
        c.execute("DELETE FROM allocations WHERE goal_id=?", (gid,))
        c.execute("DELETE FROM goal_partners WHERE goal_id=?", (gid,))
        c.execute("DELETE FROM goal_notes WHERE goal_id=?", (gid,))
        c.execute("DELETE FROM goals WHERE id=?", (gid,))


# ------------------------------------------------------------------ inbound
def receive(db, me: str, sender: str, kind: str, p: dict) -> dict:
    """One message from the gateway. `sender` is set by the gateway, never by the payload."""
    st = status(db, sender)
    if kind == "invite":
        if st is None:
            if db.one("SELECT COUNT(*) n FROM links WHERE status='pending'")["n"] >= MAX_PENDING:
                return {"ignored": "too many requests"}
            with db.tx() as c:
                c.execute("INSERT INTO links(peer,status,created) VALUES(?,?,?)", (sender, "pending", now()))
        elif st == "invited":                             # we invited each other: that is a yes
            with db.tx() as c:
                c.execute("UPDATE links SET status='active' WHERE peer=?", (sender,))
            queue(db, sender, "accept", {})
        return {"ok": True}
    if kind == "accept":
        if st == "invited":
            with db.tx() as c:
                c.execute("UPDATE links SET status='active' WHERE peer=?", (sender,))
            push(db, me)
        return {"ok": True}
    if kind == "unlink":
        if st is not None:
            unlink(db, sender, notify=False)
        return {"ok": True}
    if st != "active":
        return {"ignored": "not linked"}
    if kind == "leave":
        g = db.one("SELECT * FROM goals WHERE shared_id=?", (p.get("shared_id"),))
        if g and sender in json.loads(g["members"] or "[]"):          # only a member can leave (and so make it private here)
            members = [m for m in json.loads(g["members"] or "[]") if m != sender]
            with db.tx() as c:
                c.execute("DELETE FROM goal_partners WHERE goal_id=? AND peer=?", (g["id"], sender))
                if members:
                    c.execute("UPDATE goals SET members=? WHERE id=?", (json.dumps(members), g["id"]))
                else:
                    _make_private(c, g["id"])
        return {"ok": True}
    if kind == "goal":
        return _receive_goal(db, me, sender, p)
    if kind == "acct":
        try:
            ref, name, bal = str(p["ref"])[:64], str(p["name"])[:80], round(float(p["balance"]), 2)
        except (KeyError, TypeError, ValueError):
            return {"ignored": "malformed"}
        with db.tx() as c:
            c.execute("INSERT INTO shared_in(peer,kind,ref,name,institution,acct_kind,balance,updated) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(peer,kind,ref) DO UPDATE SET name=excluded.name,institution=excluded.institution,acct_kind=excluded.acct_kind,balance=excluded.balance,updated=excluded.updated",
                      (sender, "account", ref, name, str(p.get("institution", ""))[:80], str(p.get("kind", ""))[:20], bal, now()))
        return {"ok": True}
    if kind.startswith("joint_"):
        return _receive_joint(db, sender, kind, p)
    if kind == "unshare":
        with db.tx() as c:
            c.execute("DELETE FROM shared_in WHERE peer=? AND kind=? AND ref=?", (sender, str(p.get("kind", "")), str(p.get("ref", ""))))
        return {"ok": True}
    return {"ignored": "unknown message"}


def _receive_goal(db, me: str, sender: str, p: dict) -> dict:
    sid, members, goal = p.get("shared_id"), p.get("members"), p.get("goal")
    if not (isinstance(sid, str) and 8 <= len(sid) <= 64 and isinstance(members, list) and isinstance(goal, dict) and me in members and sender in members):
        return {"ignored": "malformed"}
    try:
        rev, contrib = int(p.get("rev", 0)), round(float(p.get("contrib", 0)), 2)
        name, target = str(goal["name"])[:120].strip(), round(float(goal["target"]), 2)
        prio = goal.get("priority") if goal.get("priority") in (1, 2, 3) else 2
        status_ = goal.get("status") if goal.get("status") in ("active", "done", "dropped") else "active"
        deadline = goal.get("deadline") if isinstance(goal.get("deadline"), str) and re.match(r"^\d{4}-\d{2}-\d{2}$", goal["deadline"]) else None
        closed = goal.get("closed") if isinstance(goal.get("closed"), str) else None
    except (KeyError, TypeError, ValueError):
        return {"ignored": "malformed"}
    if not name or not target > 0 or contrib < 0:
        return {"ignored": "malformed"}
    others = sorted(m for m in members if m != me)
    g = db.one("SELECT * FROM goals WHERE shared_id=?", (sid,))
    if g and sender not in json.loads(g["members"] or "[]"):
        return {"ignored": "not a member of that goal"}             # knowing a goal's shared_id is not enough to edit it
    with db.tx() as c:
        if not g:
            cur = c.execute("INSERT INTO goals(name,target,deadline,priority,monthly,note,status,created,closed,shared_id,members,shared_state,rev,edited_by) VALUES(?,?,?,?,0,'',?,?,?,?,?,'offered',?,?)",
                            (name, target, deadline, prio, status_, now()[:10], closed, sid, json.dumps(others), rev, p.get("edited_by") or sender))
            gid = cur.lastrowid
        else:
            gid = g["id"]
            newer = rev > (g["rev"] or 0) or (rev == (g["rev"] or 0) and (p.get("edited_by") or sender) > (g["edited_by"] or ""))
            if newer:
                c.execute("UPDATE goals SET name=?,target=?,deadline=?,priority=?,status=?,closed=?,members=?,rev=?,edited_by=? WHERE id=?",
                          (name, target, deadline, prio, status_, closed, json.dumps(others), rev, p.get("edited_by") or sender, gid))
        c.execute("INSERT INTO goal_partners(goal_id,peer,amount) VALUES(?,?,?) ON CONFLICT(goal_id,peer) DO UPDATE SET amount=excluded.amount", (gid, sender, contrib))
        # Each member sends their own notes to everyone, so only the sender's own notes are taken from this message, and they are stored as the
        # sender's whatever the payload says. A note can be removed only by its author, and only in this goal.
        for n in (p.get("notes") or [])[:MAX_NOTES]:
            if not (isinstance(n, dict) and isinstance(n.get("id"), str) and 1 <= len(n["id"]) <= 64 and n.get("author") == sender and isinstance(n.get("ts"), str)):
                continue
            text = "" if n.get("deleted") else str(n.get("text", ""))[:2000]
            c.execute("INSERT INTO goal_notes(id,goal_id,author,ts,text,deleted) VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET text=excluded.text, deleted=excluded.deleted "
                      "WHERE excluded.deleted=1 AND goal_notes.author=excluded.author AND goal_notes.goal_id=excluded.goal_id",
                      (n["id"], gid, sender, n["ts"][:40], text, 1 if n.get("deleted") else 0))
    return {"ok": True, "goal_id": gid}


def decorate(db, items: list[dict]) -> None:
    """Add the shared side to goals_view items in place: who is in, what each has set aside, the notes, and the team's progress."""
    for v in items:
        if not v.get("shared_id"):
            continue
        partners = [{"peer": r["peer"], "amount": r["amount"]} for r in db.all("SELECT peer, amount FROM goal_partners WHERE goal_id=? ORDER BY peer", (v["id"],))]
        v["members"] = json.loads(v.get("members") or "[]")
        v["partners"] = partners
        v["notes"] = [n for n in _notes(db, v["id"]) if not n["deleted"]]


# ------------------------------------------------------------------ permissions: what I let a linked person see (a link alone shares nothing)
def grant(db, me: str, peer: str, kind: str, ref: str, on: bool) -> None:
    if kind != "account":
        raise LinkError("unknown thing to share")
    if status(db, peer) != "active":
        raise LinkError(f"you are not linked with {peer}")
    if not db.one("SELECT 1 FROM accounts WHERE id=?", (ref,)):
        raise KeyError(ref)
    with db.tx() as c:
        if on:
            c.execute("INSERT INTO link_shares(peer,kind,ref) VALUES(?,?,?) ON CONFLICT(peer,kind,ref) DO NOTHING", (peer, kind, ref))
        else:
            c.execute("DELETE FROM link_shares WHERE peer=? AND kind=? AND ref=?", (peer, kind, ref))
            c.execute("DELETE FROM outbox WHERE peer=? AND kind='acct' AND ref=?", (peer, ref))
    if on:
        push_accounts(db)
    else:
        queue(db, peer, "unshare", {"kind": kind, "ref": ref}, "acct:" + ref)


def push_accounts(db) -> int:
    """Send each granted account's name and my part of its balance, whenever either changed."""
    from coinpile import ledger
    n = 0
    for g in db.all("SELECT * FROM link_shares WHERE kind='account'"):
        a = db.one("SELECT * FROM accounts WHERE id=?", (g["ref"],))
        if not a or status(db, g["peer"]) != "active":
            continue
        msg = {"ref": a["id"], "name": a.get("nickname") or a["name"], "institution": a["institution"], "kind": a["kind"], "balance": round(ledger.mine(a), 2)}
        sig = hashlib.sha256(json.dumps(msg, sort_keys=True).encode()).hexdigest()
        if sig == g["sent_sig"]:
            continue
        queue(db, g["peer"], "acct", msg, g["ref"])
        with db.tx() as c:
            c.execute("UPDATE link_shares SET sent_sig=? WHERE peer=? AND kind=? AND ref=?", (sig, g["peer"], g["kind"], g["ref"]))
        n += 1
    return n


def sharing_overview(db) -> list[dict]:
    """For each linked person: which of my accounts they can see, what they let me see, and the goals we share."""
    mine = db.all("SELECT id, name, nickname, institution, kind, balance FROM accounts ORDER BY institution, name")
    out = []
    for l in db.all("SELECT peer, status FROM links WHERE status='active' ORDER BY peer"):
        p = l["peer"]
        granted = {r["ref"] for r in db.all("SELECT ref FROM link_shares WHERE peer=? AND kind='account'", (p,))}
        goals = [{"id": g["id"], "name": g["name"]} for g in db.all("SELECT id, name, members FROM goals WHERE shared_id IS NOT NULL AND shared_state='active'") if p in json.loads(g["members"] or "[]")]
        out.append({"peer": p, "accounts": [{"id": a["id"], "name": a["nickname"] or a["name"], "institution": a["institution"], "kind": a["kind"], "shared": a["id"] in granted} for a in mine],
                    "from_them": [dict(r) for r in db.all("SELECT * FROM shared_in WHERE peer=? ORDER BY institution, name", (p,))], "goals": goals})
    return out


def requests(db) -> dict:
    """Things waiting for an answer: people asking to link, and goals offered to me. The app shows these until they are answered."""
    offered = []
    for g in db.all("SELECT id, name, target, members FROM goals WHERE shared_state='offered' ORDER BY id"):
        offered.append({"id": g["id"], "name": g["name"], "target": g["target"], "from": json.loads(g["members"] or "[]")})
    return {"links": [r["peer"] for r in db.all("SELECT peer FROM links WHERE status='pending' ORDER BY created")], "goals": offered}


# ------------------------------------------------------------------ joint accounts: one bank account that is in both people's Firefly III
# The Plaid connector's "Joint with" puts the same bank account into both people's Firefly III. Here the two Coinpile accounts are tied
# together: each side counts only its share (net worth, savings), each sends the other its own numbers for that account (the balance its
# Firefly III shows, and what it moved in from its own accounts), and settling up compares those with the agreed split.
# Coinpile only: Firefly III is not changed; every number comes from each person's own Firefly III.
JOINT_KINDS = ("cash", "credit", "loan")


def _joint(db, jid: int) -> dict:
    r = db.one("SELECT * FROM joint_accounts WHERE id=?", (jid,))
    if not r:
        raise KeyError(jid)
    return r


def _acct_msg(a: dict) -> dict:
    return {"ref": a["id"], "name": (a.get("nickname") or a["name"] or "")[:80], "institution": (a.get("institution") or "")[:80], "mask": (a.get("mask") or "")[:8]}


def _set_share(c, account_id: str | None, peer: str | None, share: float | None) -> None:
    if account_id:
        c.execute("UPDATE accounts SET joint_with=?, my_share=? WHERE id=?", (peer, share, account_id))


def joint_offer(db, me: str, peer: str, account_id: str, my_share: float = 50) -> dict:
    """Tell a linked person: this account of mine is joint with you, and I count my_share percent of it."""
    if status(db, peer) != "active":
        raise LinkError(f"link with {peer} first (Settings, Sharing)")
    a = db.one("SELECT * FROM accounts WHERE id=?", (account_id,))
    if not a:
        raise KeyError(account_id)
    if a["kind"] not in JOINT_KINDS:
        raise LinkError("only a bank account, card or loan can be joint")
    if not 0 < my_share < 100:
        raise LinkError("your share is a percentage between 0 and 100")
    if db.one("SELECT 1 FROM joint_accounts WHERE account_id=? AND state='active'", (account_id,)):
        raise LinkError("that account is already linked as joint")
    old = db.one("SELECT * FROM joint_accounts WHERE account_id=?", (account_id,))
    if old:                                                   # an earlier offer that was never answered: withdraw it first
        queue(db, old["peer"], "joint_end", {"ref": account_id, "to": old["peer_account"] or ""}, "joint-end:" + account_id)
    with db.tx() as c:
        c.execute("DELETE FROM joint_accounts WHERE account_id=?", (account_id,))
        jid = c.execute("INSERT INTO joint_accounts(account_id,peer,my_share,state,created) VALUES(?,?,?,'offered',?)", (account_id, peer, my_share, now())).lastrowid
    queue(db, peer, "joint_offer", {**_acct_msg(a), "your_share": round(100 - my_share, 2)}, "joint:" + account_id)
    return joint_view_one(db, jid)


def joint_accept(db, me: str, jid: int, account_id: str) -> dict:
    """Say yes to an offer: this account of mine is the same one. My share is what they left for me."""
    r = _joint(db, jid)
    if r["state"] != "pending":
        raise LinkError("there is no offer waiting on this")
    a = db.one("SELECT * FROM accounts WHERE id=?", (account_id,))
    if not a:
        raise KeyError(account_id)
    if a["kind"] not in JOINT_KINDS:
        raise LinkError("only a bank account, card or loan can be joint")
    if db.one("SELECT 1 FROM joint_accounts WHERE account_id=? AND id!=?", (account_id, jid)):
        raise LinkError("that account is already linked as joint")
    with db.tx() as c:
        c.execute("UPDATE joint_accounts SET account_id=?, state='active' WHERE id=?", (account_id, jid))
        _set_share(c, account_id, r["peer"], r["my_share"])
    queue(db, r["peer"], "joint_accept", {**_acct_msg(a), "to": r["peer_account"], "share": r["my_share"]}, "joint:" + account_id)
    push_joint(db)
    return joint_view_one(db, jid)


def joint_decline(db, jid: int) -> None:
    r = _joint(db, jid)
    with db.tx() as c:
        c.execute("DELETE FROM joint_accounts WHERE id=?", (jid,))
        _set_share(c, r["account_id"] if r["state"] == "active" else None, None, None)
    queue(db, r["peer"], "joint_end", {"ref": r["account_id"] or "", "to": r["peer_account"] or ""}, "joint-end:" + str(r["peer_account"] or r["account_id"]))


def _receive_joint(db, sender: str, kind: str, p: dict) -> dict:
    ref, to = str(p.get("ref") or "")[:64], str(p.get("to") or "")[:64]
    if kind == "joint_offer":
        try:
            share = float(p.get("your_share"))
        except (TypeError, ValueError):
            return {"ignored": "malformed"}
        if not ref or not 0 < share < 100:
            return {"ignored": "malformed"}
        if db.one("SELECT COUNT(*) n FROM joint_accounts WHERE state='pending' AND peer=? AND peer_account!=?", (sender, ref))["n"] >= 20:
            return {"ignored": "too many offers"}
        with db.tx() as c:
            for old in c.execute("SELECT * FROM joint_accounts WHERE peer=? AND peer_account=?", (sender, ref)).fetchall():
                if old["state"] == "active":              # they ended it on their side and are asking again
                    _set_share(c, old["account_id"], None, None)
                c.execute("DELETE FROM joint_accounts WHERE id=?", (old["id"],))
            c.execute("INSERT INTO joint_accounts(account_id,peer,peer_account,peer_name,peer_mask,my_share,state,created) VALUES(NULL,?,?,?,?,?,'pending',?)",
                      (sender, ref, str(p.get("name") or "")[:80] + (f" ({str(p.get('institution'))[:60]})" if p.get("institution") else ""), str(p.get("mask") or "")[:8], share, now()))
        return {"ok": True}
    if kind == "joint_accept":
        r = db.one("SELECT * FROM joint_accounts WHERE peer=? AND account_id=? AND state='offered'", (sender, to))
        if not r:
            return {"ignored": "no such offer"}
        try:
            theirs = float(p["share"]) if p.get("share") is not None else 100 - r["my_share"]
        except (TypeError, ValueError):
            theirs = -1
        if abs(theirs + r["my_share"] - 100) > 0.01:          # they accepted a different split: end it on both sides, offer again
            with db.tx() as c:
                c.execute("DELETE FROM joint_accounts WHERE id=?", (r["id"],))
            queue(db, sender, "joint_end", {"ref": r["account_id"], "to": ref}, "joint-end:" + r["account_id"])
            return {"ignored": "the shares do not add up to 100"}
        with db.tx() as c:
            c.execute("UPDATE joint_accounts SET state='active', peer_account=?, peer_name=?, peer_mask=? WHERE id=?",
                      (ref, str(p.get("name") or "")[:80], str(p.get("mask") or "")[:8], r["id"]))
            _set_share(c, r["account_id"], sender, r["my_share"])
        push_joint(db)
        return {"ok": True}
    if kind == "joint_end":
        rows = db.all("SELECT * FROM joint_accounts WHERE peer=? AND (account_id=? OR peer_account=?)", (sender, to, ref))
        with db.tx() as c:
            for r in rows:
                c.execute("DELETE FROM joint_accounts WHERE id=?", (r["id"],))
                if r["state"] == "active":
                    _set_share(c, r["account_id"], None, None)
        return {"ok": True}
    if kind == "joint_stats":
        r = db.one("SELECT * FROM joint_accounts WHERE peer=? AND account_id=? AND peer_account=? AND state='active'", (sender, to, ref))
        if not r:
            return {"ignored": "not a joint account here"}
        try:
            stats = {"balance": round(float(p["balance"]), 2), "as_of": str(p.get("as_of") or "")[:32],
                     "in_by_month": {str(k)[:7]: round(float(v), 2) for k, v in (p.get("in_by_month") or {}).items()}}
        except (KeyError, TypeError, ValueError):
            return {"ignored": "malformed"}
        with db.tx() as c:
            c.execute("UPDATE joint_accounts SET peer_stats=? WHERE id=?", (json.dumps(stats), r["id"]))
        return {"ok": True}
    return {"ignored": "unknown message"}


def contributions(db, account_id: str, months: int = 12, today=None) -> dict:
    """What I moved into this account from my own accounts, per month, from my Firefly III: Firefly III transfers whose other side is one of my
    own (not joint) bank accounts, and (as bank feeds bring them) a deposit here matched one to one by a withdrawal of the same amount from one of
    my own bank accounts within 3 days. Money the other person moves in is in my Firefly III only as a deposit with no such withdrawal, so it is
    never counted as mine; pending lines are left out until they settle."""
    from datetime import date as _d, timedelta
    today = today or _d.today()
    y, m = today.year, today.month - (months - 1)
    while m < 1:
        y, m = y - 1, m + 12
    since = f"{y:04d}-{m:02d}-01"
    joint = {r["account_id"] for r in db.all("SELECT account_id FROM joint_accounts WHERE account_id IS NOT NULL")}
    own = {r["id"] for r in db.all("SELECT id FROM accounts WHERE kind='cash'")} - joint - {account_id}
    deps = db.all("""SELECT id, date, amount FROM transactions WHERE account_id=? AND amount>0 AND COALESCE(pending,0)=0 AND date>=? AND date<=?
                     ORDER BY date, id""", (account_id, since, today.isoformat()))
    if not own or not deps:
        return {}
    marks = ",".join("?" * len(own))
    pool = db.all(f"""SELECT id, date, amount FROM transactions WHERE account_id IN ({marks}) AND amount<0 AND COALESCE(pending,0)=0
                      AND id NOT LIKE '%:out' AND id NOT LIKE '%:in' AND date>=? ORDER BY date, id""",
                  (*own, (_d.fromisoformat(since) - timedelta(days=3)).isoformat()))
    used, out = set(), {}
    for d in deps:
        mine = False
        if d["id"].endswith(":in"):
            leg = db.one("SELECT account_id FROM transactions WHERE id=?", (d["id"][:-3] + ":out",))
            mine = bool(leg and leg["account_id"] in own)
        else:
            dd = _d.fromisoformat(d["date"][:10])
            best = None
            for w in pool:
                if w["id"] in used or abs(w["amount"] + d["amount"]) > 0.005:
                    continue
                gap = abs((_d.fromisoformat(w["date"][:10]) - dd).days)
                if gap <= 3 and (best is None or gap < best[0]):
                    best = (gap, w["id"])
            if best:
                used.add(best[1])
                mine = True
        if mine:
            k = d["date"][:7]
            out[k] = round(out.get(k, 0) + d["amount"], 2)
    return out


def push_joint(db, today=None) -> int:
    """Send the other person my numbers for each joint account whenever they change: the balance my Firefly III shows and what I put in."""
    n = 0
    for r in db.all("SELECT * FROM joint_accounts WHERE state='active'"):
        a = db.one("SELECT balance, updated FROM accounts WHERE id=?", (r["account_id"],))
        if not a or status(db, r["peer"]) != "active":
            continue
        msg = {"ref": r["account_id"], "to": r["peer_account"], "balance": round(a["balance"] or 0, 2), "as_of": a["updated"] or "", "in_by_month": contributions(db, r["account_id"], today=today)}
        sig = hashlib.sha256(json.dumps(msg, sort_keys=True).encode()).hexdigest()
        if sig == r["sent_sig"]:
            continue
        queue(db, r["peer"], "joint_stats", msg, "joint-stats:" + r["account_id"])
        with db.tx() as c:
            c.execute("UPDATE joint_accounts SET sent_sig=? WHERE id=?", (sig, r["id"]))
        n += 1
    return n


def _suggest(db, r: dict) -> list[dict]:
    """My accounts most likely to be the one they offered: the same last digits first, then a similar name."""
    taken = {x["account_id"] for x in db.all("SELECT account_id FROM joint_accounts WHERE account_id IS NOT NULL")}
    words = set(re.findall(r"[a-z]{3,}", (r["peer_name"] or "").lower()))
    scored = []
    for a in db.all("SELECT * FROM accounts WHERE kind IN ('cash','credit','loan') ORDER BY institution, name"):
        if a["id"] in taken:
            continue
        s = (10 if r["peer_mask"] and a.get("mask") == r["peer_mask"] else 0) + len(words & set(re.findall(r"[a-z]{3,}", f"{a['name']} {a['institution']}".lower())))
        scored.append((s, {"id": a["id"], "name": a.get("nickname") or a["name"], "institution": a["institution"], "mask": a.get("mask")}))
    return [x for _, x in sorted(scored, key=lambda x: -x[0])]


def joint_view_one(db, jid: int, today=None) -> dict:
    from datetime import date as _d
    r = _joint(db, jid)
    today = today or _d.today()
    month = today.isoformat()[:7]
    a = db.one("SELECT * FROM accounts WHERE id=?", (r["account_id"],)) if r["account_id"] else None
    stats = json.loads(r["peer_stats"]) if r["peer_stats"] else None
    out = {"id": r["id"], "state": r["state"], "peer": r["peer"], "my_share": r["my_share"], "their_share": round(100 - r["my_share"], 2), "their_account": r["peer_name"],
           "account": {"id": a["id"], "name": a.get("nickname") or a["name"], "institution": a["institution"], "balance": a["balance"]} if a else None}
    if r["state"] == "pending":
        out["suggestions"] = _suggest(db, r)
    if r["state"] == "active":
        if not a:
            out["problem"] = "this account is no longer in your Firefly III"
        mine = contributions(db, r["account_id"], today=today) if a else {}
        theirs = (stats or {}).get("in_by_month") or {}
        m_in, t_in = mine.get(month, 0.0), theirs.get(month, 0.0)
        due_me = round((m_in + t_in) * r["my_share"] / 100, 2)
        diff = round(m_in - due_me, 2)            # > 0: I put in more than my share this month
        out["this_month"] = {"month": month, "you_put_in": m_in, "they_put_in": t_in if stats else None, "your_share_of_it": due_me,
                             "settle": None if not stats else (f"{r['peer']} owes you {diff:,.2f}" if diff > 0.005 else f"you owe {r['peer']} {-diff:,.2f}" if diff < -0.005 else "even")}
        if stats and a:
            gap = round((a["balance"] or 0) - stats["balance"], 2)
            out["balance_check"] = {"yours": a["balance"], "theirs": stats["balance"], "as_of_theirs": stats["as_of"], "in_step": abs(gap) < 0.01,
                                    **({} if abs(gap) < 0.01 else {"note": "the two Firefly III copies show different balances: one has not got the latest from the bank yet"})}
        elif not stats:
            out["balance_check"] = {"note": f"waiting for {r['peer']}'s Coinpile to send its numbers"}
    return out


def joint_list(db, today=None) -> list[dict]:
    return [joint_view_one(db, r["id"], today) for r in db.all("SELECT id FROM joint_accounts ORDER BY state, id")]
