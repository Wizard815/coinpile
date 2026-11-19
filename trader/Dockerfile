# Coinpile's trader: TradingAgents on your own model. Never places orders; Coinpile does, after checking your rules.
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TRADINGAGENTS_LLM_PROVIDER=openai_compatible \
    TRADINGAGENTS_RESULTS_DIR=/data/logs TRADINGAGENTS_CACHE_DIR=/data/cache TRADINGAGENTS_MEMORY_LOG_PATH=/data/memory/trading_memory.md
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
# TradingAgents pinned to a reviewed commit; change it deliberately.
RUN pip install --no-cache-dir "tradingagents @ git+https://github.com/TauricResearch/TradingAgents@9fa1196fffbcfd9de5146d2dd2869df84ab99838" "fastapi>=0.110" "uvicorn>=0.29"
WORKDIR /app
COPY app.py ./
RUN useradd -r -u 10002 trader && mkdir -p /data && chown trader /data
USER trader
VOLUME /data
EXPOSE 8100
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8100/health', timeout=4).status == 200 else 1)"
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8100", "--no-access-log"]
