"""AI categories: a fallback under the rules for payments still on Misc/Income, off unless switched on, never over a category someone set."""
import json
import re

import httpx

from coinpile import web
from tests.test_coinpile import ALICE, TODAY, signed_in, world

GUESS = {"walmart": "Groceries", "amazon": "Shopping", "mystery": "Made Up Category", "zelle": None, "kroger": "Food", "cash app": "Fun"}


def model(log):
    """A model that reads the numbered lines and answers a JSON object, the way a real one is asked to."""
    def h(req: httpx.Request):
        if not req.url.path.endswith("/chat/completions"):
            return httpx.Response(404)
        body = json.loads(req.content)
        log.append(body)
        lines = body["messages"][-1]["content"].splitlines()
        out = {}
        for ln in lines:
            n, text = ln.split(". ", 1)
            out[n] = next((v for k, v in GUESS.items() if k in text.lower()), None)
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "Here you go:\n```json\n" + json.dumps(out) + "\n```"}}]})
    return httpx.MockTransport(h)


def rig(tmp_path, ff=None, cloud=False, allow_cloud=False):
    ff, log = ff or world(), []
    ff.tx("1", 40, "withdrawal", 63.18, "WALMART SUPERCENTER #12", 1, 94, "2026-10-09")          # no category in Firefly III
    ff.tx("1", 41, "withdrawal", 20.00, "MYSTERY CHARGE 0041", 1, 95, "2026-10-10")
    a = web.create_app(firefly_url="https://firefly.example.com", data_dir=str(tmp_path), firefly_transport=ff.transport(), llm_transport=model(log), today=lambda: TODAY)
    c = signed_in(a)
    url = "https://api.example-ai.com/v1" if cloud else "http://localhost:11434/v1"
    c.post("/api/agent/providers", json={"name": "M", "kind": "openai", "base_url": url, "model": "m", "tools": True, "allow_cloud": allow_cloud})
    return c, ff, log


def tx(c, name):
    return next(t for t in c.get("/api/transactions", params={"q": name, "limit": 50}).json()["items"] if name.lower() in t["name"].lower())


def prompt_lines(body):
    return body["messages"][-1]["content"].splitlines()


def test_off_by_default_means_no_model_calls_at_all(tmp_path):
    c, ff, log = rig(tmp_path)
    assert c.get("/api/settings").json()["ai_categorize"] == "off"
    r = c.post("/api/categories/recategorize").json()
    assert "ai" not in r and log == []
    c.post("/api/refresh")
    assert log == [] and tx(c, "walmart")["category"] == "Misc"


def test_one_batch_places_what_the_rules_could_not_and_drops_invented_names(tmp_path):
    c, ff, log = rig(tmp_path)
    c.put("/api/settings", json={"ai_categorize": "on"})
    r = c.post("/api/categories/recategorize").json()
    assert len(log) == 1                                                   # one message for all of them
    sent = "\n".join(prompt_lines(log[0]))
    assert "WALMART" in sent and "AMAZON" in sent and "MYSTERY" in sent
    assert "KROGER" not in sent and "NETFLIX" not in sent and "PAYROLL" not in sent       # Firefly III has a category on these: never asked
    assert "Groceries" in log[0]["messages"][0]["content"] and "ff:" not in sent           # the real category names; no ids
    assert r["ai"]["set"] == 2
    assert tx(c, "walmart")["category"] == "Groceries" and tx(c, "amazon")["category"] == "Shopping"
    assert tx(c, "mystery")["category"] == "Misc"                          # "Made Up Category" is not one of the names: thrown away
    assert tx(c, "kroger")["category"] == "Groceries"                      # set in Firefly III: untouched
    assert c.post("/api/categories/recategorize").json()["ai"]["calls"] == 1 and len(log) == 2     # MYSTERY is asked again on a manual run, the placed ones are not
    assert "WALMART" not in "\n".join(prompt_lines(log[1]))


def test_the_guess_survives_reading_firefly_again_and_gives_way_to_a_category_set_there(tmp_path):
    c, ff, log = rig(tmp_path)
    c.put("/api/settings", json={"ai_categorize": "on"})
    c.post("/api/categories/recategorize")
    c.post("/api/refresh", params={"full": True})
    assert tx(c, "walmart")["category"] == "Groceries"
    g = next(g for g in ff.data["1"]["groups"] if g["id"] == "40")
    g["attributes"]["transactions"][0]["category_name"] = "Shopping"       # the person sets it in Firefly III
    c.post("/api/refresh", params={"full": True})
    assert tx(c, "walmart")["category"] == "Shopping"


def test_a_category_set_by_hand_in_coinpile_is_never_sent_or_changed(tmp_path):
    c, ff, log = rig(tmp_path)
    c.put("/api/settings", json={"ai_categorize": "on"})
    c.post("/api/accounts", json={"name": "Wallet", "kind": "cash", "balance": 50})
    wid = next(a["id"] for a in c.get("/api/accounts").json() if a["name"] == "Wallet")
    c.post("/api/transactions", json={"account_id": wid, "date": "2026-10-11", "name": "Cash App Bob", "amount": -10, "category": "Misc"})      # chosen by hand
    c.post("/api/transactions", json={"account_id": wid, "date": "2026-10-12", "name": "Cash App Ann", "amount": -12})                         # left to Coinpile
    c.post("/api/categories/recategorize")
    sent = "\n".join(prompt_lines(log[0]))
    assert "Cash App Ann" in sent and "Cash App Bob" not in sent
    assert tx(c, "cash app bob")["category"] == "Misc" and tx(c, "cash app ann")["category"] == "Fun"
    c.post("/api/categories/recategorize")                                 # the rules running again keep the guess
    assert tx(c, "cash app ann")["category"] == "Fun"


def test_auto_mode_only_looks_at_what_just_arrived(tmp_path):
    c, ff, log = rig(tmp_path)
    c.put("/api/settings", json={"ai_categorize": "on", "ai_categorize_auto": "on"})
    c.post("/api/refresh")
    assert log == []                                                       # switching on does not send the history
    ff.tx("1", 60, "withdrawal", 31.07, "WALMART #99", 1, 96, "2026-10-20")
    out = c.post("/api/refresh").json()
    assert len(log) == 1 and len(prompt_lines(log[0])) == 1 and "WALMART #99" in prompt_lines(log[0])[0]
    assert out["ai_categorize"]["set"] == 1 and tx(c, "walmart #99")["category"] == "Groceries"
    c.post("/api/refresh")
    assert len(log) == 1                                                   # nothing new: no call
    assert tx(c, "walmart supercenter")["category"] == "Misc"              # history is left to the manual Re-run on all


def test_turning_auto_off_and_on_again_does_not_send_what_came_in_between(tmp_path):
    c, ff, log = rig(tmp_path)
    c.put("/api/settings", json={"ai_categorize": "on", "ai_categorize_auto": "on"})
    c.post("/api/refresh")
    c.put("/api/settings", json={"ai_categorize_auto": "off"})
    ff.tx("1", 61, "withdrawal", 9.0, "AMAZON MKTP", 1, 96, "2026-10-21")
    c.post("/api/refresh")
    c.put("/api/settings", json={"ai_categorize_auto": "on"})
    c.post("/api/refresh")
    assert log == []


def test_a_cloud_model_is_used_only_with_allow_cloud(tmp_path):
    c, ff, log = rig(tmp_path, cloud=True)
    c.put("/api/settings", json={"ai_categorize": "on"})
    r = c.post("/api/categories/recategorize").json()
    assert log == [] and "allow cloud" in r["ai"]["skipped"]


def test_many_go_in_batches_of_forty(tmp_path):
    ff = world()
    for i in range(90):
        ff.tx("1", 200 + i, "withdrawal", 5 + i, "STORE " + chr(65 + i // 26) + chr(65 + i % 26), 1, 97, "2026-09-15")      # 90 different shops
    c, ff, log = rig(tmp_path, ff)
    c.put("/api/settings", json={"ai_categorize": "on"})
    r = c.post("/api/categories/recategorize").json()
    assert [len(prompt_lines(b)) for b in log] == [40, 40, 13] and r["ai"]["calls"] == 3


def test_a_model_that_is_down_does_not_break_the_rules_or_a_refresh(tmp_path):
    ff = world()
    a = web.create_app(firefly_url="https://firefly.example.com", data_dir=str(tmp_path), firefly_transport=ff.transport(),
                       llm_transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ConnectError("down"))), today=lambda: TODAY)
    c = signed_in(a)
    c.post("/api/agent/providers", json={"name": "M", "kind": "openai", "base_url": "http://localhost:11434/v1", "model": "m", "tools": True})
    c.put("/api/settings", json={"ai_categorize": "on", "ai_categorize_auto": "on"})
    r = c.post("/api/categories/recategorize")
    assert r.status_code == 200 and "could not be asked" in r.json()["ai"]["error"]
    c.post("/api/refresh")
    ff.tx("1", 70, "withdrawal", 3.0, "WALMART", 1, 96, "2026-10-22")
    assert c.post("/api/refresh").status_code == 200


def test_the_same_merchant_is_asked_once_and_every_one_of_its_payments_gets_that_answer(tmp_path):
    ff = world()
    ff.tx("1", 300, "withdrawal", 12.50, "WALMART SUPERCENTER #12", 1, 98, "2026-09-01")          # other store numbers and amounts: same merchant
    ff.tx("1", 301, "withdrawal", 7.25, "Walmart  Supercenter #4471", 1, 98, "2026-09-02")
    ff.tx("1", 302, "deposit", 7.25, "WALMART SUPERCENTER #12", 98, 1, "2026-09-03")             # a refund: money in, asked on its own line
    c, ff, log = rig(tmp_path, ff)
    c.put("/api/settings", json={"ai_categorize": "on"})
    r = c.post("/api/categories/recategorize").json()
    lines = prompt_lines(log[0])
    assert len(log) == 1 and sum("walmart" in ln.lower() for ln in lines) == 2
    walmarts = [t for t in c.get("/api/transactions", params={"q": "walmart", "limit": 50}).json()["items"] if t["amount"] < 0]
    assert len(walmarts) == 3 and {t["category"] for t in walmarts} == {"Groceries"}
    assert r["ai"]["asked"] == len(log[0]["messages"][-1]["content"].splitlines()) + 2        # 3 out-going Walmart rows went as 1 line


def test_a_merchant_placed_before_gets_the_same_category_without_asking_again(tmp_path):
    c, ff, log = rig(tmp_path)
    c.put("/api/settings", json={"ai_categorize": "on", "ai_categorize_auto": "on"})
    c.post("/api/refresh")                                                 # switching auto on: history is marked, not sent
    c.post("/api/categories/recategorize")                                 # the manual run places WALMART SUPERCENTER #12
    n = len(log)
    ff.tx("1", 62, "withdrawal", 44.0, "WALMART SUPERCENTER #12", 1, 96, "2026-10-23")
    out = c.post("/api/refresh").json()
    assert len(log) == n and out["ai_categorize"]["calls"] == 0           # no model call
    assert out["ai_categorize"]["set"] == 1
    assert [t["category"] for t in c.get("/api/transactions", params={"q": "walmart supercenter", "limit": 50}).json()["items"]] == ["Groceries", "Groceries"]
