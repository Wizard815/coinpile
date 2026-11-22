"""The chat answer as it is written (streamed), keep-alive lines so a proxy in front does not give up, and which model servers count as local."""
import json
import socket

import httpx

from coinpile import agent, web
from coinpile.agent import is_local
from tests.test_coinpile import TODAY, signed_in, world


def sse(events):
    return ("".join("data: " + json.dumps(e) + "\n\n" for e in events) + "data: [DONE]\n\n").encode()


def model(log, stream=True):
    """An OpenAI-style server: the first answer asks for a tool in pieces, the second writes the answer in pieces."""
    def h(req: httpx.Request):
        if not req.url.path.endswith("/chat/completions"):
            return httpx.Response(404)                          # asking the server its context size when the model is added
        body = json.loads(req.content)
        log.append(body)
        first = not any(m["role"] == "tool" for m in body["messages"])
        if first:
            chunks = [{"choices": [{"delta": {"content": "Let me look. "}}]},
                      {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "list_", "arguments": "{"}}]}}]},
                      {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "accounts", "arguments": "}"}}]}}]}]
            msg = {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "list_accounts", "arguments": "{}"}}]}
        else:
            chunks = [{"choices": [{"delta": {"content": "Your accounts "}}]}, {"choices": [{"delta": {"content": "look fine."}}]}]
            msg = {"role": "assistant", "content": "Your accounts look fine."}
        if stream and body.get("stream"):
            return httpx.Response(200, content=sse(chunks), headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={"choices": [{"message": msg}]})
    return httpx.MockTransport(h)


def rig(tmp_path, stream=True):
    log = []
    a = web.create_app(firefly_url="https://firefly.example.com", data_dir=str(tmp_path), firefly_transport=world().transport(),
                       llm_transport=model(log, stream), today=lambda: TODAY)
    c = signed_in(a)
    c.post("/api/agent/providers", json={"name": "M", "kind": "openai", "base_url": "http://localhost:11434/v1", "model": "m", "tools": True})
    return c, log


def events(c, cid, text):
    r = c.post(f"/api/chats/{cid}/messages?stream=1", json={"content": text})
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/x-ndjson")
    return [json.loads(ln) for ln in r.text.splitlines() if ln.strip()]


def test_the_answer_arrives_in_pieces_and_ends_with_the_same_reply_as_without_streaming(tmp_path):
    c, log = rig(tmp_path)
    cid = c.post("/api/chats").json()["chat"]["id"]
    evs = events(c, cid, "how are my accounts?")
    kinds = [e["type"] for e in evs]
    assert kinds[0] == "text" and evs[0]["text"] == "Let me look. "                  # what it wrote before asking for a tool...
    assert {"type": "tool", "name": "list_accounts"} in evs                          # ...then the tool, put together from its pieces
    after = [e["text"] for e in evs[kinds.index("tool"):] if e["type"] == "text"]
    assert after == ["Your accounts ", "look fine."] and kinds[-1] == "done"
    done = evs[-1]["data"]
    assert done["messages"][-1]["content"] == "Your accounts look fine." and done["messages"][-1]["role"] == "assistant"
    assert any(t["tool"] == "list_accounts" for t in done["messages"][-1]["trace"])
    assert all(b.get("stream") for b in log)
    assert c.get(f"/api/chats/{cid}").json()["messages"][-1]["content"] == "Your accounts look fine."          # saved like any other answer


def test_a_server_that_does_not_stream_still_works(tmp_path):
    c, log = rig(tmp_path, stream=False)
    cid = c.post("/api/chats").json()["chat"]["id"]
    evs = events(c, cid, "how are my accounts?")
    assert evs[-1]["type"] == "done" and evs[-1]["data"]["messages"][-1]["content"] == "Your accounts look fine."


def test_without_stream_the_reply_is_one_json_answer_as_before(tmp_path):
    c, log = rig(tmp_path)
    cid = c.post("/api/chats").json()["chat"]["id"]
    r = c.post(f"/api/chats/{cid}/messages", json={"content": "how are my accounts?"}).json()
    assert r["messages"][-1]["content"] == "Your accounts look fine." and not any(b.get("stream") for b in log)


def test_a_failing_model_ends_the_stream_with_an_error_and_keeps_no_half_question(tmp_path):
    a = web.create_app(firefly_url="https://firefly.example.com", data_dir=str(tmp_path), firefly_transport=world().transport(),
                       llm_transport=httpx.MockTransport(lambda r: httpx.Response(504, text="Gateway Timeout")), today=lambda: TODAY)
    c = signed_in(a)
    c.post("/api/agent/providers", json={"name": "M", "kind": "openai", "base_url": "http://localhost:11434/v1", "model": "m", "tools": True})
    cid = c.post("/api/chats").json()["chat"]["id"]
    evs = events(c, cid, "hi")
    assert evs[-1] == {"type": "error", "status": 502, "detail": "model server answered 504"}
    assert c.get(f"/api/chats/{cid}").json()["messages"] == []


def test_a_slow_model_gets_keep_alive_lines(tmp_path, monkeypatch):
    import time
    monkeypatch.setattr(agent, "PING_EVERY", 0.05)

    def slow(req):
        time.sleep(0.3)
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "Done."}}]})
    a = web.create_app(firefly_url="https://firefly.example.com", data_dir=str(tmp_path), firefly_transport=world().transport(),
                       llm_transport=httpx.MockTransport(slow), today=lambda: TODAY)
    c = signed_in(a)
    c.post("/api/agent/providers", json={"name": "M", "kind": "openai", "base_url": "http://localhost:11434/v1", "model": "m", "tools": False})
    cid = c.post("/api/chats").json()["chat"]["id"]
    evs = events(c, cid, "hi")
    assert sum(e["type"] == "ping" for e in evs) >= 2 and evs[-1]["type"] == "done"


def test_anthropic_streaming_puts_text_and_tool_input_back_together():
    chunks = [{"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
              {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Checking "}},
              {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "t1", "name": "list_accounts", "input": {}}},
              {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"kind": '}},
              {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '"cash"}'}},
              {"type": "message_stop"}]
    t = httpx.MockTransport(lambda r: httpx.Response(200, content=sse(chunks), headers={"content-type": "text/event-stream"}))
    got = []
    with httpx.Client(transport=t) as http:
        out = agent._call_anthropic(http, {"model": "m", "base_url": "https://api.anthropic.com"}, "k", "s", [{"role": "user", "content": "hi"}], None, got.append)
    assert got == ["Checking "] and out["content"] == "Checking "
    assert out["tool_calls"] == [{"id": "t1", "name": "list_accounts", "args": {"kind": "cash"}}]


# ------------------------------------------------------------------ which servers are local
def resolving(monkeypatch, *ips):
    agent._local_seen.clear()
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", (ip, 0)) for ip in ips])


def test_a_full_domain_that_resolves_to_your_own_network_is_local(monkeypatch):
    resolving(monkeypatch, "192.168.20.5")
    assert is_local("https://ai.myhome.example.com/v1") is True                     # a home server with its own certificate and split DNS


def test_a_full_domain_with_any_public_address_is_cloud(monkeypatch):
    resolving(monkeypatch, "192.168.20.5", "8.8.8.8")
    assert is_local("https://ai.myhome.example.com/v1") is False
    resolving(monkeypatch, "8.8.8.8")
    assert is_local("https://api.example-ai.com/v1") is False


def test_a_full_domain_that_does_not_resolve_is_cloud(monkeypatch):
    agent._local_seen.clear()

    def nope(*a, **k):
        raise socket.gaierror("no such name")
    monkeypatch.setattr(socket, "getaddrinfo", nope)
    assert is_local("https://ai.myhome.example.com/v1") is False and is_local("http://ollama:11434") is True
    agent._local_seen.clear()
