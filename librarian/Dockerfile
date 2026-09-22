FROM python:3.12.14-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libxml2 libxslt1.1 \
    && rm -rf /var/lib/apt/lists/*
# requirements.lock pins every transitive dependency with hashes (regenerate with
# `uvx --from pip-tools pip-compile --generate-hashes requirements.txt -o requirements.lock`);
# --require-hashes refuses anything that was not audited into it.
COPY requirements.lock .
RUN pip install --require-hashes -r requirements.lock
COPY . .
# The container runs as an unprivileged uid (compose "user:"), so the code must be readable
# regardless of the file modes it was copied from (scp'd sources are often 0600).
RUN chmod -R a+rX /app
# Runs as the library owner (1000) via compose "user:". Binds localhost by default (host
# network + Caddy in front); BIND=0.0.0.0 is for the test harness only.
# ONE worker process: the background engine (queue, dropbox/torrent watchers) runs inside
# it, so a second process would double-process; threads give the request concurrency.
CMD gunicorn -w 1 --threads 8 -b "${BIND:-127.0.0.1}:8090" --access-logfile - app:app
