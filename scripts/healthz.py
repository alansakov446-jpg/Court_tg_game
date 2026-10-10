"""Liveness probe for the long-running poller.

main.py rewrites health.json after every polling cycle; this probe answers
whether that file is fresh and describes a worker that is really polling.

Modes:
    python scripts/healthz.py            HTTP server on HEALTHZ_PORT (default 8080),
                                         GET /healthz -> 200 alive / 503 dead
    python scripts/healthz.py --check    one-shot probe for Docker HEALTHCHECK or CI:
                                         prints the verdict, exit 0 alive / 1 dead

Hygiene: only counters, timestamps, the lock state and the public bot username
leave the process — never tokens, keys, prompts, answers, chat text or truth.
"""

import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# The poller touches health.json every cycle (a few seconds); a stale file
# means the worker hung or died even if the process is technically alive.
DEFAULT_MAX_AGE = 120


def health_file():
    return os.getenv("HEALTH_FILE", "health.json")


def max_age():
    return int(os.getenv("HEALTHZ_MAX_AGE_SECONDS", str(DEFAULT_MAX_AGE)))


def read_health():
    try:
        with open(health_file(), encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def verdict():
    """(alive, human-readable reason). Never includes secrets or chat text."""
    health = read_health()
    if health is None:
        return False, "health.json is missing or unreadable"
    try:
        age = time.time() - os.path.getmtime(health_file())
    except OSError:
        return False, "health.json disappeared"
    if age > max_age():
        return False, f"health.json is stale: {int(age)}s old, limit {max_age()}s"
    if health.get("lock") not in ("ok", "pending"):
        return False, f"lock={health.get('lock')}: this worker does not own the poller lock"
    if health.get("status") not in ("starting", "ok"):
        return False, f"status={health.get('status')}"
    return True, (
        f"alive: polls={health.get('poll_cycles', 0)} "
        f"handled={health.get('updates_handled', 0)} age={int(age)}s"
    )


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?", 1)[0] != "/healthz":
            self.send_response(404)
            self.end_headers()
            return
        ok, reason = verdict()
        body = json.dumps({"healthy": ok, "reason": reason}).encode("utf-8")
        self.send_response(200 if ok else 503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002 — keep the stdlib signature
        pass  # probes must not spam; the verdict itself is the observable output


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--check" in argv:
        ok, reason = verdict()
        print(reason)
        return 0 if ok else 1
    port = int(os.getenv("HEALTHZ_PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"healthz listening on 0.0.0.0:{port}/healthz", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
