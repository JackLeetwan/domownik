# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.12.23 AS uv

FROM python:3.14-slim
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    DOMOWNIK_DATA_DIR=/data \
    TZ=Europe/Warsaw \
    PATH="/app/.venv/bin:$PATH"
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev
RUN useradd --uid 1000 --create-home domownik && mkdir -p /data && chown domownik /data
USER domownik
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"
CMD ["domownik", "serve", "--host", "0.0.0.0", "--port", "8000"]
