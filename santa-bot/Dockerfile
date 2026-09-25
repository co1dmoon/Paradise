# «Санта в чате»: bot + website in one container (§13). Built by `docker compose up -d --build`.
FROM python:3.12-slim

# pip reads PIP_INDEX_URL from the build environment: a Russian VPS can switch to a PyPI mirror
# when pypi.org is unreachable. docker-compose.yml passes it from .env (README_RU.md, «Неполадки»).
ARG PIP_INDEX_URL=https://pypi.org/simple

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Europe/Moscow

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

# Trust the Russian CA (Минцифры) system-wide too; the app itself also loads certs/ (app/tls.py).
COPY certs/*.pem /tmp/certs/
RUN for pem in /tmp/certs/*.pem; do \
        cp "$pem" "/usr/local/share/ca-certificates/$(basename "$pem" .pem).crt"; \
    done \
    && rm -rf /tmp/certs \
    && update-ca-certificates

RUN useradd --system --uid 10001 --user-group --home-dir /app --no-create-home --shell /usr/sbin/nologin santa \
    && mkdir -p /data \
    && chown santa:santa /data

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .

EXPOSE 8080

# The container starts as root only to hand /data to the app user: the ./data bind mount is
# created by Docker as root, and files restored by the owner (README_RU.md) are root-owned too.
# setpriv (util-linux) then drops to the unprivileged `santa` user, which runs the app.
ENTRYPOINT ["/bin/sh", "-c", "chown -R santa:santa /data && exec setpriv --reuid=santa --regid=santa --clear-groups \"$@\"", "entrypoint"]
CMD ["python", "-m", "app.main"]
