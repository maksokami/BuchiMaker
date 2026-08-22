# ----- Build stage --------------------------------------------------------
FROM python:3.11-slim AS builder

WORKDIR /build

# System deps for DuckDB native extensions (optional, keeps image lean)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ \
    && rm -rf /var/lib/apt/lists/*

# requirements.txt is a compiled, hash-pinned lockfile (see requirements.in
# and docs/backend_architecture.md's "Dependency lockfile" section) —
# --require-hashes fails the build if any package (including transitive
# deps) resolves to something not listed with a verified hash, so a
# compromised/tampered PyPI upload can't silently slip into the image.
COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes --prefix=/install -r requirements.txt


# ----- Runtime stage ------------------------------------------------------
# Using Python slim (consider Docker Hardened Images in production:
# https://www.docker.com/products/hardened-images/)
FROM python:3.11-slim AS runtime

LABEL org.opencontainers.image.title="DataBoom Backend"
LABEL org.opencontainers.image.version="0.3.0"
LABEL org.opencontainers.image.description="API-first dashboard backend with DuckDB"

# Create a non-root user (security hardening)
RUN groupadd -r databoom && useradd -r -g databoom databoom

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application source
COPY app/ ./app/
COPY main.py .

# Mount points for external data and dashboard configs
VOLUME ["/app/dashboards", "/app/data"]

# Ensure databoom user can write to /app (needed for ./db creation)
RUN chown -R databoom:databoom /app

# Non-root execution
USER databoom

# Main API port
EXPOSE 8000
# Health check port (spec: classes.md — port 35400)
EXPOSE 35400

# Docker native healthcheck
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c \
    "import urllib.request; urllib.request.urlopen('http://localhost:35400/healthz')" \
    || exit 1

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

CMD ["python", "main.py"]
