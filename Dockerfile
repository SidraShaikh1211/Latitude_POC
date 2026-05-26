# syntax=docker/dockerfile:1.7

# Stage 1: build the React app
FROM node:20-alpine AS frontend
WORKDIR /app/frontend-react

COPY frontend-react/package.json frontend-react/package-lock.json ./
RUN npm ci

COPY frontend-react/ ./
# Same-origin in production: API calls go to the FastAPI host that's also
# serving the static bundle, so the base URL is empty (relative paths).
ENV VITE_API_BASE=""
RUN npm run build


# Stage 2: Python runtime
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential libpq-dev \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY app/ ./app/
COPY policies/ ./policies/
COPY scripts/ ./scripts/
COPY skills/ ./skills/
COPY --from=frontend /app/frontend-react/dist ./frontend-react/dist

# Cloud Run injects $PORT (default 8080). Bind to 0.0.0.0 so the container
# is reachable from the Cloud Run proxy.
ENV PORT=8080
EXPOSE 8080

CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT}
