#!/usr/bin/env bash
# alert.sh "<title>" "<text>" [priority]  — one alert channel for host-side jobs.
# Delivered by the portal (NOTIFY_WEBHOOK and/or ADMIN_EMAIL via its SMTP); when the portal is
# down, the message still lands in the journal (journalctl -t bookstack).
title="${1:-bookstack}"; text="${2:-}"; prio="${3:-}"
if ! docker exec -i librarian python -m notify alert "$title" "$text" ${prio:+"$prio"} >/dev/null 2>&1; then
  logger -t bookstack -p user.warning "ALERT $title: $text" 2>/dev/null || echo "ALERT $title: $text" >&2
fi
exit 0
