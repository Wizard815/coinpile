"""Connecting your own SearXNG: what each common problem says, and the Test button's report."""
import httpx
import pytest

from coinpile import websearch


def sx(status=200, body=None, text=None):
    def h(req: httpx.Request):
        assert req.url.path == "/search" and req.url.params["format"] == "json"
        if text is not None:
            return httpx.Response(status, text=text)
        return httpx.Response(status, json=body if body is not None else {})
    return httpx.MockTransport(h)


GOOD = {"results": [{"url": "https://www.bankrate.com/cd", "title": "CD rates", "content": "4.5% APY", "engines": ["google", "bing"]},
                    {"url": "https://www.fdic.gov/rates", "title": "National rates", "content": "", "engine": "duckduckgo"}],
        "unresponsive_engines": [["brave", "timeout"]]}


@pytest.mark.parametrize("status,body,text,says", [
    (403, None, "Forbidden", "search.formats"),
    (429, None, "Too many", "limiter"),
    (401, None, "Unauthorized", "user:password@"),
    (404, None, "Not found", "no SearXNG search page"),
    (200, None, "<html>results</html>", "did not answer in JSON"),
])
def test_each_common_problem_says_what_to_change(status, body, text, says):
    with pytest.raises(websearch.SearchError) as e:
        websearch.search("http://192.168.1.20:8080", "cd rates", sx(status, body, text))
    assert says in str(e.value)


def test_the_address_is_forgiving_and_the_test_reports_engines():
    r = websearch.probe("http://192.168.1.20:8080/search/", sx(body=GOOD))          # the search page's address pasted: still works
    assert [x["site"] for x in r["results"]] == ["www.bankrate.com", "www.fdic.gov"]
    assert r["engines"] == ["bing", "duckduckgo", "google"] and r["unresponsive"] == ["brave (timeout)"]
    with pytest.raises(websearch.SearchError, match="http:// or https://"):
        websearch.search("192.168.1.20:8080", "x", sx(body=GOOD))
    with pytest.raises(websearch.SearchError, match="these engines failed: google"):
        websearch.probe("http://h:8080", sx(body={"results": [], "unresponsive_engines": [["google", "CAPTCHA"]]}))


def test_the_setting_and_its_certificate_switch_are_saved(tmp_path):
    from tests.test_coinpile import signed_in, world
    from coinpile import web
    a = web.create_app(firefly_url="https://firefly.example.com", data_dir=str(tmp_path), firefly_transport=world().transport(), search_transport=sx(body=GOOD))
    c = signed_in(a)
    assert c.put("/api/settings", json={"searx_url": "https://searx.home.lan", "searx_insecure": "on"}).status_code == 200
    s = c.get("/api/settings").json()
    assert s["searx_url"] == "https://searx.home.lan" and s["searx_insecure"] == "on"
    r = c.post("/api/agent/search-test", json={}).json()
    assert r["ok"] and r["engines"]
