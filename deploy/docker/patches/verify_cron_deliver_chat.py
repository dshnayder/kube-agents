"""Build-time behaviour gate for the ``deliver: "chat"`` patch.

Run by ``deploy/docker/Dockerfile`` against the patched ``/opt/hermes`` tree,
immediately after ``apply_cron_deliver_chat.py``. The applier proves the two
anchors matched and both files still parse; this proves the patched
``_deliver_result`` actually routes a report to the relay instead of to a chat
platform, and that it falls back when the relay says no.

The distinction matters because both failure modes are quiet. A patch that
applied but did not take effect delivers the old way, which on this install
means posting nowhere — indistinguishable from a watchdog with nothing to say.
A fallback that does not work loses the report entirely and leaves a
``last_delivery_error`` nobody reads. Neither shows up in a build log, so the
checks below drive the real ``_deliver_result`` and assert on what crossed the
wire.

The unit suite in ``test_cron_deliver_chat.py`` covers ``cron_deliver_chat.py``
in isolation and cannot cover any of this: the fork it installs lives inside
Hermes' own module.

What is real, and what is not
-----------------------------
Real: the shipped ``_deliver_result``, the patched fork, ``wants_chat_delivery``,
the JSON body, the bearer header, an actual HTTP request over a real socket.
Not real: the Session KV server, which is a stdlib handler on an ephemeral
loopback port — the build host has no gateway, no SQLite database and no Chat
Agent. The fallback assertion leans on the scheduler's own behaviour with no
home channels configured, which is the build host's natural state: ``deliver:
"all"`` resolves to no targets and returns the string this file matches on. If
that ever stops being true the check fails loudly rather than passing by
accident.
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

API_KEY = "verify-key"
JOB_ID = "verify-deliver-chat"
REPORT = "3 clusters are missing the workload-identity binding."

received: list[dict] = []
#: Flipped between cases to make the fake relay accept or refuse.
relay_status = [202]

failures: list[str] = []


def check(label: str, actual: object, expected: object) -> None:
    if actual != expected:
        failures.append(f"{label}: expected {expected!r}, got {actual!r}")


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 — stdlib naming
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length).decode("utf-8")
        received.append(
            {
                "path": self.path,
                "authorization": self.headers.get("Authorization", ""),
                "body": json.loads(body) if body else {},
            }
        )
        status = relay_status[0]
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status":"accepted"}')

    def log_message(self, *_args) -> None:
        """Silence the per-request line; the build log has enough in it."""


def main() -> int:
    import os

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[0], server.server_address[1]

    os.environ["SESSION_KV_API_KEY"] = API_KEY
    os.environ["CRON_REPORT_RELAY_URL"] = f"http://{host}:{port}/v1/cron-reports"
    os.environ["HERMES_HOME"] = "/opt/data/profiles/platform"

    try:
        from cron.scheduler import _deliver_result
    except Exception as exc:  # an import cycle from the trailer lands here
        print(f"VERIFY FAILED: cannot import the patched scheduler: {exc}")
        return 1

    try:
        # --- the report goes to the relay, not to a platform ----------------
        job = {"id": JOB_ID, "name": "Verify", "deliver": "chat"}
        result = _deliver_result(job, REPORT)
        check("relayed: no delivery error", result, None)
        check("relayed: one request", len(received), 1)
        if received:
            sent = received[0]
            check("relayed: route", sent["path"], "/v1/cron-reports")
            check(
                "relayed: bearer",
                sent["authorization"],
                f"Bearer {API_KEY}",
            )
            check("relayed: job id", sent["body"].get("job_id"), JOB_ID)
            check("relayed: report", sent["body"].get("report"), REPORT)
            check("relayed: profile", sent["body"].get("profile"), "platform")
        # The job dict the caller owns is untouched, so a relay outage cannot
        # rewrite the roster on its way through.
        check("relayed: job unmodified", job["deliver"], "chat")

        # --- a job that did not ask for it is not intercepted ---------------
        del received[:]
        _deliver_result({"id": "other", "deliver": "local"}, REPORT)
        check("deliver=local: no request", len(received), 0)

        # --- a refused relay falls back rather than swallowing --------------
        del received[:]
        relay_status[0] = 500
        result = _deliver_result({"id": JOB_ID, "deliver": "chat"}, REPORT)
        check("refused: one request", len(received), 1)
        # No home channel is configured on a build host, so `all` resolves to
        # nothing — which is exactly the error that proves `deliver` was
        # rewritten and the ordinary path ran.
        check(
            "refused: fell back to deliver=all",
            result,
            "no delivery target resolved for deliver=all",
        )

        # --- an unreachable relay does the same ------------------------------
        del received[:]
        os.environ["CRON_REPORT_RELAY_URL"] = "http://127.0.0.1:1/v1/cron-reports"
        result = _deliver_result({"id": JOB_ID, "deliver": "chat"}, REPORT)
        check(
            "unreachable: fell back to deliver=all",
            result,
            "no delivery target resolved for deliver=all",
        )

        # --- the create-time notice no longer contradicts the mode ----------
        from tools.cronjob_tools import _local_delivery_notice

        check(
            "notice: silent for deliver=chat",
            _local_delivery_notice({"id": JOB_ID, "deliver": "chat"}, "chat"),
            None,
        )
        notice = _local_delivery_notice({"id": "other"}, None)
        check("notice: still fires otherwise", bool(notice), True)
    finally:
        server.shutdown()

    if failures:
        print("\nVERIFY FAILED:")
        for failure in failures:
            print("  " + failure)
        return 1
    print("\ncron_deliver_chat verify OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
