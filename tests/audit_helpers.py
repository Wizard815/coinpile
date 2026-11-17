"""Helpers for the money-audit tests: a Firefly III with one person, a full read into a database, and an app with a fixed date."""
from datetime import date
from fastapi.testclient import TestClient
from coinpile import firefly, web
from coinpile.db import DB
from tests.fake_firefly import FakeFirefly
from tests.test_coinpile import ALICE, world

URL = "https://firefly.example.com"


def alone():
    return FakeFirefly({ALICE: {"id": "1", "email": "alice@example.com"}})


def read(ff, today, db=None, full=True, now=None):
    db = db or DB(":memory:")
    firefly.refresh(db, firefly.Firefly(URL, ALICE, ff.transport()), today, full=full, now=now)
    return db


def make_app(tmp_path, ff, today):
    holder = {"d": today}
    a = web.create_app(firefly_url=URL, data_dir=str(tmp_path), firefly_transport=ff.transport(), today=lambda: holder["d"])
    return a, holder


def client(a, raise_server_exceptions=False):
    c = TestClient(a, raise_server_exceptions=raise_server_exceptions)
    r = c.post("/auth/token", json={"token": ALICE})
    assert r.status_code == 200, r.text
    r = c.post("/api/refresh")
    return c
