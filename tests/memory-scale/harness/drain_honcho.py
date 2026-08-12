#!/usr/bin/env python3
"""Block until Honcho's deriver has finished, and report what the wait cost.

Why this exists
---------------
Hindsight pays its extraction cost inline: when `retain` returns, the fact is
in the bank. Honcho does not. Message ingest returns as soon as the rows are
written, and an asynchronous deriver then turns them into conclusions and peer
representations. Scoring before that queue drains measures how fast the deriver
is, not how good the retrieval is.

A fixed sleep would be the wrong instrument twice over: too short and the
numbers are wrong in Honcho's disfavour, too long and the ladder wastes hours.
`GET /v3/workspaces/{ws}/queue/status` reports pending and in-progress work
units directly, so the wait can be exactly as long as it needs to be — and the
elapsed time becomes a **result**, since settle time is a real operational
difference between the two designs.

Why it waits for stability rather than a single zero
----------------------------------------------------
The queue can read empty in the gap between one work unit completing and the
next being enqueued. `--stable-polls` requires the queue to be empty on N
consecutive polls before declaring the drain finished, which costs a few
seconds and removes a whole class of flaky early exits.
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

DEFAULT_API = "http://127.0.0.1:18800"


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def status(api_url: str, workspace: str, timeout: int = 120) -> dict:
    url = f"{api_url.rstrip('/')}/v3/workspaces/{workspace}/queue/status"
    req = urllib.request.Request(url, method="GET")
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read() or "{}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api-url", default=DEFAULT_API)
    ap.add_argument("--workspace", default="meridian")
    ap.add_argument("--poll", type=float, default=10.0, help="seconds between polls")
    ap.add_argument("--stable-polls", type=int, default=3,
                    help="consecutive empty polls required before declaring drained")
    ap.add_argument("--max-wait", type=float, default=14400.0,
                    help="give up after this many seconds (0 = wait forever)")
    ap.add_argument("--out", default="", help="write the drain record here as JSON")
    a = ap.parse_args()

    started = time.time()
    stable, last_completed, first = 0, None, None

    while True:
        try:
            q = status(a.api_url, a.workspace)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            log(f"  queue/status unavailable ({type(e).__name__}: {e}); retrying")
            time.sleep(a.poll)
            continue

        pending = int(q.get("pending_work_units") or 0)
        running = int(q.get("in_progress_work_units") or 0)
        done = int(q.get("completed_work_units") or 0)
        total = int(q.get("total_work_units") or 0)
        if first is None:
            first = q

        elapsed = time.time() - started
        if done != last_completed or (pending + running) == 0:
            # Rate is measured over this drain only, so it is not skewed by work
            # completed during seeding.
            rate = (done - (first.get("completed_work_units") or 0)) / elapsed if elapsed else 0
            eta = (pending + running) / rate if rate > 0 else None
            log(f"  {done}/{total} work units  ({pending} pending, {running} running)  "
                f"{elapsed / 60:.1f}m elapsed"
                + (f", ~{eta / 60:.0f}m remaining" if eta else ""))
            last_completed = done

        if pending == 0 and running == 0:
            stable += 1
            if stable >= a.stable_polls:
                break
        else:
            stable = 0

        if a.max_wait and elapsed > a.max_wait:
            log(f"GAVE UP after {elapsed / 60:.1f}m with {pending + running} work units "
                f"outstanding — results scored now understate Honcho")
            return 2

        time.sleep(a.poll)

    elapsed = time.time() - started
    record = {
        "workspace": a.workspace,
        "drain_seconds": round(elapsed, 1),
        "work_units_total": int(q.get("total_work_units") or 0),
        "work_units_completed": int(q.get("completed_work_units") or 0),
        "sessions": len(q.get("sessions") or {}),
        "stable_polls": a.stable_polls,
    }
    log(f"DRAINED in {elapsed / 60:.1f}m — "
        f"{record['work_units_completed']}/{record['work_units_total']} work units "
        f"across {record['sessions']} sessions")
    if a.out:
        with open(a.out, "w") as fh:
            json.dump(record, fh, indent=2)
        log(f"wrote {a.out}")
    else:
        print(json.dumps(record, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
