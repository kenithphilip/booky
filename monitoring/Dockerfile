# One-shot tool that configures Uptime Kuma (admin account, notification channels, monitors,
# the nightly-reboot maintenance window). Run by bookstack.sh after every Deploy and from
# Operations -> Monitoring; never a long-running service. Reads its configuration as JSON on
# stdin (so no secret is ever in its environment or `docker inspect`), prints one JSON line.
FROM python:3.12.14-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PYTHONDONTWRITEBYTECODE=1
# requirements.lock pins every transitive dependency with hashes (regenerate with
# `uv pip compile --python-version 3.12 --generate-hashes requirements.txt -o requirements.lock`).
COPY requirements.lock /tmp/requirements.lock
RUN pip install --require-hashes -r /tmp/requirements.lock && rm /tmp/requirements.lock
COPY kuma_bootstrap.py /app/kuma_bootstrap.py
RUN chmod -R a+rX /app
USER 65534:65534
ENTRYPOINT ["python", "/app/kuma_bootstrap.py"]
