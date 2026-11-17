"""Routes for linking people and sharing goals. Logic lives in links.py."""
from fastapi import HTTPException, Request
from coinpile.strict import BaseModel

from coinpile import links, plan


class InviteIn(BaseModel):
    peer: str


class ShareIn(BaseModel):
    peers: list[str]


class NoteIn(BaseModel):
    text: str


class GrantIn(BaseModel):
    kind: str = "account"
    ref: str
    on: bool


class InboxIn(BaseModel):
    type: str
    payload: dict = {}
    sender: str = ""


def register(app, ctx):
    store, auth, name = ctx.store, ctx.auth, ctx.name

    def run(fn, *a):
        try:
            return fn(*a)
        except links.LinkError as e:
            raise HTTPException(422, str(e))
        except KeyError:
            raise HTTPException(404, "not found")

    def done(res=None):
        ctx.shared_changed()
        return res if res is not None else {"ok": True}

    @app.get("/api/links")
    def list_links(request: Request):
        auth(request)
        waiting = store.one("SELECT COUNT(*) n FROM outbox")["n"]
        return {"me": name, "links": store.all("SELECT peer, status, created FROM links ORDER BY peer"), "waiting": waiting}

    @app.post("/api/links")
    def link_invite(request: Request, b: InviteIn):
        auth(request)
        r = run(links.invite, store, name, b.peer)
        ctx.link_flush()
        return r

    @app.post("/api/links/{peer}/accept")
    def link_accept(request: Request, peer: str):
        auth(request)
        r = run(links.accept, store, peer)
        ctx.link_flush()
        ctx.shared_changed()
        return r

    @app.delete("/api/links/{peer}")
    def link_remove(request: Request, peer: str):
        auth(request)
        run(links.unlink, store, peer)
        ctx.link_flush()
        return {"ok": True}

    @app.post("/api/links/flush")
    def link_flush(request: Request):
        auth(request)
        ctx.shared_changed()
        return {"waiting": store.one("SELECT COUNT(*) n FROM outbox")["n"]}

    @app.get("/api/links/people")
    def link_people(request: Request):
        """The other people on this Coinpile (names only), so linking is a pick, not a guess."""
        auth(request)
        return {"people": [x for x in ctx.link_people() if x != name]}

    @app.get("/api/requests")
    def requests(request: Request):
        auth(request)
        return links.requests(store)

    @app.get("/api/sharing")
    def sharing(request: Request):
        auth(request)
        return links.sharing_overview(store)

    @app.put("/api/links/{peer}/shares")
    def share_set(request: Request, peer: str, b: GrantIn):
        auth(request)
        run(links.grant, store, name, peer, b.kind, b.ref, b.on)
        ctx.link_flush()
        return {"ok": True}

    # ---- joint accounts: the same bank account in both people's Firefly III, tied together here
    class JointIn(BaseModel):
        account_id: str
        peer: str
        my_share: float = 50

    class JointAcceptIn(BaseModel):
        account_id: str

    @app.get("/api/joint")
    def joint_list(request: Request):
        auth(request)
        return links.joint_list(store, ctx.today())

    @app.post("/api/joint")
    def joint_offer(request: Request, b: JointIn):
        auth(request)
        r = run(links.joint_offer, store, name, b.peer.strip().lower(), b.account_id, b.my_share, ctx.today())
        ctx.link_flush()
        return done(r)

    @app.post("/api/joint/{jid}/accept")
    def joint_accept(request: Request, jid: int, b: JointAcceptIn):
        auth(request)
        r = run(links.joint_accept, store, name, jid, b.account_id, ctx.today())
        ctx.link_flush()
        return done(r)

    @app.delete("/api/joint/{jid}")
    def joint_end(request: Request, jid: int):
        auth(request)
        run(links.joint_decline, store, jid)
        ctx.link_flush()
        return done({"ended": jid})

    @app.post("/api/goals/{gid}/share")
    def goal_share(request: Request, gid: int, b: ShareIn):
        auth(request)
        r = run(links.share_goal, store, name, gid, b.peers)
        ctx.link_flush()
        return r

    @app.post("/api/goals/{gid}/unshare")
    def goal_unshare(request: Request, gid: int):
        auth(request)
        run(links.stop_sharing, store, name, gid)
        ctx.link_flush()
        return {"ok": True}

    @app.post("/api/goals/{gid}/accept")
    def goal_accept(request: Request, gid: int):
        auth(request)
        run(links.accept_goal, store, name, gid)
        ctx.link_flush()
        return {"ok": True}

    @app.post("/api/goals/{gid}/decline")
    def goal_decline(request: Request, gid: int):
        auth(request)
        run(links.decline_goal, store, name, gid)
        ctx.link_flush()
        return {"ok": True}

    @app.post("/api/goals/{gid}/notes")
    def note_add(request: Request, gid: int, b: NoteIn):
        auth(request)
        r = run(links.add_note, store, name, gid, b.text)
        ctx.link_flush()
        return r

    @app.delete("/api/goals/{gid}/notes/{nid}")
    def note_delete(request: Request, gid: int, nid: str):
        auth(request)
        run(links.delete_note, store, name, gid, nid)
        ctx.link_flush()
        return {"ok": True}
