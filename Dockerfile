FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1 DATA_DIR=/data UI_DIR=/app/ui
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
COPY ui ./ui
RUN pip install --no-cache-dir . && useradd -r -u 10001 coinpile && mkdir -p /data && chown coinpile /data
USER coinpile
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"
# --no-access-log: request paths (searches, account ids) stay out of the container log. The app logs only that it started and how many people use it.
CMD ["uvicorn", "--factory", "coinpile.web:main", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--log-level", "info"]
