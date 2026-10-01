FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements-prod.txt ./
RUN pip install --upgrade pip \
    && pip install --index-url https://pypi.org/simple --user -r requirements-prod.txt

FROM python:3.11-slim

ARG BUILD_COMMIT=unknown
ARG APP_VERSION=development
ARG FRONTEND_ASSET_VERSION=development
ARG QQNT_ADAPTER_VERSION=qqnt-9.9.x-basic

ENV BUILD_COMMIT=$BUILD_COMMIT \
    APP_VERSION=$APP_VERSION \
    FRONTEND_ASSET_VERSION=$FRONTEND_ASSET_VERSION \
    QQNT_ADAPTER_VERSION=$QQNT_ADAPTER_VERSION \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/root/.local/bin:$PATH

WORKDIR /app

COPY --from=builder /root/.local /root/.local
COPY app ./app
COPY alembic.ini ./alembic.ini
COPY migrations ./migrations
COPY data/storage/.gitkeep ./data/storage/.gitkeep
COPY data/backups/.gitkeep ./data/backups/.gitkeep

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import json, urllib.request; print(json.load(urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)))" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
