"""Investments: every account you track as an investment, in one place, whatever holds it.

An account is an investment when:
  - its kind is investment (a brokerage or retirement account Plaid or a connector reported as such, or Robinhood), or
  - you said so: its "what it is for" is Investment or Retirement (Accounts, open the account, "Track as an investment"),
    which works for any account, including a Firefly III asset account (Firefly III itself has no investment type).

Sources that know more than a balance (positions, cost, history) plug in as providers. Robinhood is one; Foliofox is reserved for later.
A provider answers: is it set up, and if so its accounts' positions. Adding one means adding an entry to PROVIDERS and a fetch function."""
from coinpile import plaidplan

INVEST_CATEGORIES = ("Investment", "Retirement")


def is_investment(a: dict) -> bool:
    return a.get("kind") == "investment" or plaidplan.category_group(a.get("category")) == "investment"


def accounts(db) -> list[dict]:
    from coinpile import ledger
    out = []
    for a in db.all("SELECT * FROM accounts ORDER BY institution, name"):
        if not is_investment(a):
            continue
        out.append({"id": a["id"], "name": a.get("nickname") or a["name"], "institution": a["institution"], "source": a["source"], "kind": a["kind"],
                    "category": a.get("category"), "balance": round(ledger.mine(a), 2), "currency": a.get("currency") or "USD",
                    "native_balance": a.get("native_balance"), "native_currency": a.get("native_currency"), "updated": a.get("updated"),
                    "how": "type" if a.get("kind") == "investment" and not a.get("category") else "you"})
    return out


def candidates(db) -> list[dict]:
    """Accounts you could start tracking as an investment."""
    return [{"id": a["id"], "name": a.get("nickname") or a["name"], "institution": a["institution"]}
            for a in db.all("SELECT * FROM accounts WHERE kind IN ('cash','investment') ORDER BY institution, name") if not is_investment(a)]


def providers(rh) -> list[dict]:
    """Where positions can come from. `rh` is the person's Robinhood reader (None if none)."""
    out = []
    if rh is not None:
        st = {"agent": rh.probe("agent"), "live": rh.probe("live")}
        import os
        has_csv = os.path.isdir(rh.csv_dir) and bool(os.listdir(rh.csv_dir))
        out.append({"id": "robinhood", "name": "Robinhood", "available": True, "connected": has_csv or st["agent"] == "up" or st["live"] == "up",
                    "how": "activity-report CSV" + (", agent connector" if st["agent"] != "not deployed" else "") + (", live connector" if st["live"] != "not deployed" else ""),
                    "views": ["main", "agent"]})
    out.append({"id": "foliofox", "name": "Foliofox", "available": False, "connected": False,
                "how": "planned: portfolio tracking and its AI advisor, reading what you hold elsewhere"})
    return out
