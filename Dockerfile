FROM python:3.14-slim

WORKDIR /app

# Install dependencies first so this layer is cached across code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code.
COPY server.py subscriptions.py ./

# Run as an unprivileged user. /data holds the persisted ICS subscription pull
# list and is created here owned by `app`: Docker seeds a fresh named volume from
# the image path *including ownership*, so the non-root user can write to it.
# (A bind mount is not seeded that way — chown it to 10001 on the host first.)
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app \
    && mkdir -p /data \
    && chown -R app /app /data
USER app

EXPOSE 8080

# Liveness probe hits the unauthenticated /healthz endpoint.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz').read()"

CMD ["python", "server.py"]
