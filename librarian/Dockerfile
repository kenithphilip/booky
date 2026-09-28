FROM python:3.12.14-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libxml2 libxslt1.1 \
    && rm -rf /var/lib/apt/lists/*
# requirements.lock pins every transitive dependency with hashes (regenerate with
# `uvx --from pip-tools pip-compile --generate-hashes requirements.txt -o requirements.lock`);
# --require-hashes refuses anything that was not audited into it.
COPY requirements.lock fetch_wheels.py ./
# These layers are cached, so a routine Deploy never gets here. When it does and pypi.org's
# index stalls (measured on the real server 2026-09-28: its CDN timed out on pip's JSON request
# for ONE project page, every time, while the plain HTML page answered in milliseconds),
# fetch_wheels.py takes the pinned files through the HTML pages instead. Every file is checked
# against requirements.lock's sha256 either way (--require-hashes), so the path cannot matter.
RUN pip install --timeout 30 --retries 3 --require-hashes -r requirements.lock \
 || { echo "pypi.org's index did not answer: fetching the pinned files through its plain pages (hash-checked)"; \
      python fetch_wheels.py requirements.lock /tmp/wheels \
      && pip install --no-index --find-links /tmp/wheels --require-hashes -r requirements.lock \
      && rm -rf /tmp/wheels; }
# kepubify (pgaskin/kepubify v4.0.4, statically linked): the portal converts an EPUB to KEPUB
# when a reader DOWNLOADS one (L11). It is never used by Kobo sync — see DECISIONS-PENDING on
# why letting sync convert is a trap. The release publishes no checksums, so these were
# computed once (2026-09-26) and the build refuses any other file.
ARG TARGETARCH
RUN python -c "import hashlib,sys,urllib.request; \
a={'amd64':('64bit','37d7628d26c5c906f607f24b36f781f306075e7073a6fe7820a751bb60431fc5'),'arm64':('arm64','5a15b8f6f6a96216c69330601bca29638cfee50f7bf48712795cff88ae2d03a3')}['${TARGETARCH:-amd64}']; \
d=urllib.request.urlopen('https://github.com/pgaskin/kepubify/releases/download/v4.0.4/kepubify-linux-'+a[0], timeout=60).read(); \
h=hashlib.sha256(d).hexdigest(); sys.exit('kepubify checksum mismatch: '+h) if h!=a[1] else None; \
open('/usr/local/bin/kepubify','wb').write(d)" && chmod 755 /usr/local/bin/kepubify && kepubify --version
COPY . .
# Identifies the code in the image (git hash / date, passed by compose or the installer), so
# /healthz?detail=1, /admin and selftest can tell a stale portal image from the checkout.
# HERE, after the dependency layers, never above them: a value that changes on every Deploy
# invalidates every layer after it, which made every Deploy download all of pypi.org again.
ARG BUILD_VERSION=unknown
ENV BUILD_VERSION=${BUILD_VERSION}
# The container runs as an unprivileged uid (compose "user:"), so the code must be readable
# regardless of the file modes it was copied from (scp'd sources are often 0600).
RUN chmod -R a+rX /app
# Runs as the library owner (1000) via compose "user:". Binds localhost by default (host
# network + Caddy in front); BIND=0.0.0.0 is for the test harness only.
# ONE worker process: the background engine (queue, dropbox watcher, housekeeping) runs inside
# it, so a second process would double-process; threads give the request concurrency.
# exec-form CMD + `exec`: gunicorn becomes PID 1 and receives SIGTERM, so `docker stop`,
# restarts and updates shut it down gracefully instead of waiting 10 s for the SIGKILL
# (which cut uploads off and left 'interrupted by restart' rows behind).
CMD ["sh", "-c", "exec gunicorn -w 1 --threads 8 --graceful-timeout 25 -b \"${BIND:-127.0.0.1}:8090\" --access-logfile - app:app"]
