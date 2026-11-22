"""Coinpile's trader: TradingAgents (github.com/TauricResearch/TradingAgents, Apache-2.0) behind a small private API.

TradingAgents does the research and the decision: market, news, social and fundamentals analysts, a bull/bear debate, a risk debate
and a portfolio manager, on your own model (any OpenAI-compatible server, e.g. Argos). It also learns: before each analysis it scores
its earlier calls on that stock against what the price did, and the lessons go into the decision.

This service never places orders and holds no broker sign-in. Coinpile asks it for a decision, checks that against your rules, and
places (or simulates) the order itself.

Endpoints (all but /health need X-Internal-Secret = CONNECTOR_SECRET):
  POST /analyze  {symbol, date?, portfolio: {cash, positions: [{ticker, quantity, average_price}]}, guidance?}
  GET  /quotes?symbols=AAPL,MSFT      last prices (Yahoo Finance)
  POST /settle                        score every due past decision now (it also happens on its own before each analysis)
  GET  /health
"""
import hmac
import os
import re
import threading
from datetime import date

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

SECRET = os.environ.get("CONNECTOR_SECRET", "")
SYMBOL = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
app = FastAPI(title="Coinpile trader", docs_url=None, redoc_url=None, openapi_url=None)
_lock = threading.Lock()          # one analysis at a time: a local model serves one long run better than several
_graph = None


def _auth(secret: str | None) -> None:
    if not SECRET or not secret or not hmac.compare_digest(secret, SECRET):
        raise HTTPException(401, "wrong or missing X-Internal-Secret")


def graph():
    global _graph
    if _graph is None:
        from tradingagents.default_config import build_default_config
        from tradingagents.graph.trading_graph import TradingAgentsGraph
        cfg = build_default_config()            # TRADINGAGENTS_* environment: provider, models, backend URL, memory path
        analysts = [a.strip() for a in os.environ.get("TRADER_ANALYSTS", "market,social,news,fundamentals").split(",") if a.strip()]
        _graph = TradingAgentsGraph(selected_analysts=analysts, config=cfg)
    return _graph


class Holding(BaseModel):
    ticker: str
    quantity: float
    average_price: float | None = None


class Book(BaseModel):
    cash: float | None = None
    positions: list[Holding] = Field(default_factory=list, max_length=200)


class Ask(BaseModel):
    symbol: str
    date: str | None = None
    portfolio: Book | None = None
    guidance: str = Field(default="", max_length=2000)


def _portfolio(book: Book | None, guidance: str):
    """TradingAgents' own portfolio block, with the owner's guidance for this session after it, so the managers weigh it."""
    from tradingagents.portfolio import PortfolioContext, Position

    class Guided(PortfolioContext):
        guidance: str = ""

        def render(self, ticker: str) -> str:
            text = super().render(ticker)
            return text + (f"\n\nThe account owner's guidance for this session (weigh it; it is not an order):\n{self.guidance}" if self.guidance else "")

    b = book or Book()
    return Guided(cash=b.cash, currency="USD", guidance=guidance.strip(),
                  positions=[Position(ticker=p.ticker, quantity=p.quantity, average_price=p.average_price) for p in b.positions])


def _cut(s, n=4000) -> str:
    return (s or "")[:n] if isinstance(s, str) else ""


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/analyze")
def analyze(a: Ask, x_internal_secret: str | None = Header(default=None)):
    _auth(x_internal_secret)
    sym = a.symbol.strip().upper()
    if not SYMBOL.match(sym):
        raise HTTPException(422, "not a stock symbol")
    day = a.date or date.today().isoformat()
    with _lock:
        try:
            state, rating = graph().propagate(sym, day, portfolio=_portfolio(a.portfolio, a.guidance))
        except Exception as e:                # the model or a data source failed: say so, Coinpile treats it as "no decision"
            raise HTTPException(502, f"analysis failed: {type(e).__name__}: {str(e)[:300]}")
    return {"symbol": sym, "date": day, "rating": str(rating or ""),
            "decision": _cut(state.get("final_trade_decision"), 6000),
            "trader_plan": _cut(state.get("trader_investment_plan")),
            "reports": {k: _cut(state.get(f"{k}_report"), 3000) for k in ("market", "news", "sentiment", "fundamentals")},
            "lessons": _cut(state.get("past_context"), 2000)}


@app.get("/quotes")
def quotes(symbols: str, x_internal_secret: str | None = Header(default=None)):
    _auth(x_internal_secret)
    syms = [s for s in (x.strip().upper() for x in symbols.split(",")) if SYMBOL.match(s)][:60]
    import yfinance as yf
    out = {}
    for s in syms:
        try:
            p = yf.Ticker(s).fast_info.get("lastPrice")
            if p and p > 0:
                out[s] = round(float(p), 4)
        except Exception:
            continue
    return {"prices": out}


@app.post("/settle")
def settle(x_internal_secret: str | None = Header(default=None)):
    _auth(x_internal_secret)
    with _lock:
        s = graph().settle_all_pending(wait=True)
    return {"settled": len(s.settled), "failed": len(s.failed)}
