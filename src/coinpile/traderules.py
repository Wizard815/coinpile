"""The rules every order of the auto wallet must pass, in this order:

1. Daily loss stop (Coinpile): once the wallet is down `daily_loss_pct` from where it started the day, nothing more trades that day.
2. Day-trade guard (Coinpile): never sell a stock the same day it was bought, so the account never makes a day trade and the
   pattern-day-trader limit for accounts under $25,000 can't be hit.
3. Your policy (AgentRisk, github.com/trycoin-ai/agentrisk, MIT): a YAML file you edit, with the cap per trade, the largest
   position, a cash floor, crypto/options/margin allowed or not, and symbols never to trade.

A rule that can't be evaluated blocks: these checks fail closed."""
from datetime import datetime, timezone

import yaml

DEFAULT_POLICY = """# Your rules for the auto wallet. Every order the agent wants is checked against these (AgentRisk),
# after the daily loss stop and the day-trade guard. Percentages are of the wallet's value after the trade.
limits:
  max_single_position_pct: 20      # no stock may be more than 20% of the wallet
asset_rules:                       # allow | warn | block   (warn asks you first)
  crypto: block
  options: block
  margin: block
order_rules:
  max_order_pct_of_portfolio: 20   # cap per trade
  min_cash_pct: 0                  # keep at least this much cash
restricted:
  never_trade: []                  # e.g. [GME, AMC]
  warn_list: []                    # asks you before trading these
"""


class Verdict(dict):
    """{ok: bool, ask: bool, rule: str, reason: str}: ok=False with ask=True means a person may approve it."""


def _v(ok: bool, rule: str, reason: str = "", ask: bool = False) -> Verdict:
    return Verdict(ok=ok, ask=ask, rule=rule, reason=reason)


def parse_policy(text: str) -> dict:
    """The policy YAML, validated by AgentRisk itself. Raises ValueError with a readable message."""
    from agentrisk import Policy
    try:
        data = yaml.safe_load(text or "") or {}
    except yaml.YAMLError as e:
        raise ValueError(f"the rules are not valid YAML: {str(e).splitlines()[0]}")
    if not isinstance(data, dict):
        raise ValueError("the rules must be a YAML mapping (limits:, order_rules: ...)")
    try:
        Policy.model_validate(data)
    except Exception as e:
        raise ValueError(f"the rules don't fit AgentRisk's policy: {str(e).splitlines()[0]}")
    return data


def daily_loss(start_value: float | None, value_now: float | None, limit_pct: float | None) -> Verdict:
    if not limit_pct or limit_pct <= 0:
        return _v(True, "daily_loss")
    if not start_value or start_value <= 0 or value_now is None:
        return _v(False, "daily_loss", "the wallet's value isn't known, so the daily loss stop can't be checked")
    down = (start_value - value_now) / start_value * 100
    if down >= limit_pct:
        return _v(False, "daily_loss", f"the wallet is down {down:.1f}% today (stop at {limit_pct:g}%): no more trades until tomorrow")
    return _v(True, "daily_loss")


def day_trade(side: str, symbol: str, bought_today: set[str], enabled: bool = True) -> Verdict:
    if enabled and side == "sell" and symbol.upper() in bought_today:
        return _v(False, "day_trade", f"{symbol} was bought today; selling it today would be a day trade")
    return _v(True, "day_trade")


def policy(policy_text: str, cash: float, positions: list[dict], side: str, symbol: str, quantity: float, price: float, asset_class: str | None = None) -> Verdict:
    """AgentRisk's verdict on one order. positions: [{symbol, quantity, price}]. AgentRisk tells stocks, ETFs and crypto apart itself."""
    from agentrisk import check_trade_risk
    try:
        pol = parse_policy(policy_text)
    except ValueError as e:
        return _v(False, "policy", str(e))
    pf = {"as_of": datetime.now(timezone.utc).isoformat(), "cash": round(max(cash, 0), 2),
          "positions": [{"symbol": p["symbol"], "quantity": p["quantity"], "price": p["price"]} for p in positions if p.get("quantity") and p.get("price")]}
    try:
        v = check_trade_risk(pf, {"action": side, "symbol": symbol, "quantity": quantity, "estimated_price": price, **({"asset_class": asset_class} if asset_class else {})}, policy=pol, audit=False)
    except Exception as e:                       # AgentRisk fails closed itself; this is belt and braces
        return _v(False, "policy", f"the rules could not be checked ({type(e).__name__})")
    if v.proceed:
        return _v(True, "policy", v.summary if v.verdict == "WARN" else "")
    return _v(False, "policy", v.summary, ask=v.verdict == "WARN")


def check(*, side: str, symbol: str, quantity: float, price: float, cash: float, positions: list[dict], policy_text: str,
          start_value: float | None, value_now: float | None, daily_loss_pct: float | None, bought_today: set[str], pdt_guard: bool) -> Verdict:
    for v in (daily_loss(start_value, value_now, daily_loss_pct), day_trade(side, symbol, bought_today, pdt_guard)):
        if not v["ok"]:
            return v
    return policy(policy_text, cash, positions, side, symbol, quantity, price)
