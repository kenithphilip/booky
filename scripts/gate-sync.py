#!/usr/bin/env python3
"""gate-sync.py — carry portal password changes (and logins the portal's /admin creates) into
the Authelia gate's user file (L05). Host side, root, standard library only.

The portal never writes Authelia's file: it queues a PBKDF2-SHA512 hash (never the password) in
librarian.db and touches librarian/state/gate-sync.flag. The bookstack-gate-sync.path unit runs
this within seconds of that; a 10-minute timer catches anything the path unit missed. Each row:
  * the user already has a gate login -> its password line is replaced (e-mail, 2FA, groups kept)
  * no gate login yet and the row carries an e-mail -> a new login (groups users[, admins])
  * no gate login and no e-mail -> reported back as "missing" (the admin adds it in the TUI)
Authelia re-reads the file itself (authentication_backend.file.watch); an older rendered config
without watch gets a restart instead.
"""
import json, os, re, subprocess, sys, tempfile

STACK = os.environ.get("STACK_DIR", "/srv/bookstack")
ENV = os.path.join(STACK, ".env")
USERS = os.environ.get("GATE_USERS_FILE", os.path.join(STACK, "authelia/users_database.yml"))
CONFIG = os.environ.get("GATE_CONFIG_FILE", os.path.join(STACK, "authelia/configuration.yml"))
LIBRARIAN = os.environ.get("GATE_LIBRARIAN", "librarian")
NAME = re.compile(r"^[A-Za-z0-9._-]+$")
HASH = re.compile(r"^\$(pbkdf2-sha512|argon2id)\$[A-Za-z0-9$.,=/+-]+$")


def envget(key, default=""):
    try:
        for line in open(ENV, encoding="utf-8"):
            if line.startswith(key + "="):
                v = line.split("=", 1)[1].rstrip("\n")
                return v[1:-1].replace("'\\''", "'") if len(v) >= 2 and v[0] == v[-1] == "'" else v
    except OSError:
        pass
    return default


def admin_cli(*args):
    r = subprocess.run(["docker", "exec", "-i", LIBRARIAN, "python", "-m", "admin_cli", *args],
                       capture_output=True, text=True, timeout=60)
    try:
        return json.loads((r.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"ok": False, "error": (r.stderr or r.stdout or "no output")[:200]}


def apply(text, row):
    """(new text, outcome, detail). Pure: the unit tests drive it directly."""
    u, h = row["user"], row["hash"]
    if not NAME.match(u or "") or not HASH.match(h or ""):
        return text, "failed", "refused: odd user name or hash"
    entry = re.search(r"(?m)^  " + re.escape(u) + r":[ \t]*\n((?:    .*\n?)*)", text)
    if entry:
        body = entry.group(1)
        if not re.search(r"(?m)^    password: ", body):
            return text, "failed", "the gate entry has no password line"
        nb = re.sub(r"(?m)^    password: .*$", "    password: " + json.dumps(h), body, count=1)
        return text[:entry.start(1)] + nb + text[entry.end(1):], "ok", "password updated"
    em = row.get("email") or ""
    if not em or "@" not in em:
        return text, "missing", "no gate login for this user (Security -> Authelia: add or reset a user)"
    text = re.sub(r"(?m)^users: \{\}\s*$", "users:", text)
    if not re.search(r"(?m)^users:", text):
        text = text.rstrip("\n") + "\nusers:\n"
    groups = "      - users\n" + ("      - admins\n" if row.get("admin") else "")
    text = text.rstrip("\n") + "\n  %s:\n    displayname: %s\n    password: %s\n    email: %s\n    groups:\n%s" % (
        u, json.dumps(row.get("display") or u), json.dumps(h), json.dumps(em), groups)
    return text, "ok", "gate login created"


def write(path, text):
    st = os.stat(path) if os.path.exists(path) else None
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".users.")
    with os.fdopen(fd, "w") as f:
        f.write(text if text.endswith("\n") else text + "\n")
    os.chmod(tmp, 0o600)
    if st:
        os.chown(tmp, st.st_uid, st.st_gid)
    else:
        os.chown(tmp, 1000, 1000)
    os.replace(tmp, path)


def main():
    if envget("AUTHELIA_ENABLED") != "true":
        print("gate-sync: the Authelia gate is off; nothing to do")
        return 0
    ans = admin_cli("gate", "pending")
    if not ans.get("ok"):
        print(f"gate-sync: cannot read the queue: {ans.get('error')}", file=sys.stderr)
        return 1
    rows = ans.get("rows") or []
    if not rows:
        return 0
    try:
        text = open(USERS, encoding="utf-8").read()
    except OSError:
        text = "users: {}\n"
    results, changed = [], False
    for row in rows:
        text, outcome, detail = apply(text, row)
        changed |= outcome == "ok"
        results.append((row["user"], outcome, detail))
    if changed:
        write(USERS, text)
        try:
            watching = re.search(r"(?m)^\s+watch:\s*true\b", open(CONFIG, encoding="utf-8").read())
        except OSError:
            watching = None
        if not watching:
            subprocess.run(["docker", "restart", "authelia"], capture_output=True, timeout=120)
    rc = 0
    for u, outcome, detail in results:
        admin_cli("gate", "done", u, outcome, "--reason", detail)
        print(f"gate-sync: {u}: {outcome} ({detail})")
        rc |= outcome != "ok"
    return rc


if __name__ == "__main__":
    sys.exit(main())
