## Stage 1 — frontend build (Node)
FROM node:20-slim AS frontend-builder
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

## Stage 2 — hub runtime (Python)
FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends curl git && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY pyproject.toml ./
COPY src/ src/
# Editable install: site-packages gets a .pth pointer to /app/src (not a copy).
# docker-compose bind-mounts host src/ over /app/src at runtime so uvicorn
# --reload picks up edits without a rebuild.
RUN pip install --no-cache-dir --no-deps -e "."
COPY VERSION /app/VERSION
# Frontend bundle from stage 1. server.py mounts /app/frontend/dist at /
# when this directory exists; without it, the legacy landing page serves.
COPY --from=frontend-builder /build/dist /app/frontend/dist
RUN groupadd -r tubemail && useradd -r -g tubemail -d /app tubemail
RUN mkdir -p /data && chown -R tubemail:tubemail /app /data
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh
ENTRYPOINT ["/app/entrypoint.sh"]
