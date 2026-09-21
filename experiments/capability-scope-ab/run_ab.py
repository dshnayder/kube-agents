#!/usr/bin/env python3
"""Drive one arm of the capability-scoping A/B against a deployed Platform Agent.

For every scenario in scenarios.json and every repetition, POST the prompt to the Hermes API
server's /v1/responses with a fresh conversation id, then fetch the session's token totals and
its message log. Everything is written under --out/<scenario>-r<rep>.json; nothing is scored
here (analyze.py does that from the run files and the plugin's capability_scope.jsonl record).

The arm itself (which index mode, which scope mode, which catalogue) is a property of the
deployment, set before this script runs. --label is recorded so the run files say which arm they
belong to.

Usage:
  run_ab.py --base http://127.0.0.1:8643 --token "$PLATFORM_AGENT_TOKEN" \
      --scenarios scenarios.json --out runs/stock-r1 --label stock-r1 --reps 3 --parallel 3
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import pathlib
import time
import urllib.error
import urllib.request
import uuid

RESPONSES_PATH = "/v1/responses"
SESSION_PATH = "/api/sessions/{sid}"
MESSAGES_PATH = "/api/sessions/{sid}/messages?limit=500&order=oldest"
SESSION_HEADER = "X-Hermes-Session-Id"
MODEL_NAME = "model-default"
HTTP_TIMEOUT_SECONDS = 900
RETRY_STATUSES = (429, 502, 503, 504)
RETRY_ATTEMPTS = 4
RETRY_SLEEP_SECONDS = 20
DEFAULT_REPS = 3
DEFAULT_PARALLEL = 3
CONVERSATION_PREFIX = "csc-ab"
POST_TURN_SETTLE_SECONDS = 2
DEFAULT_PROMPT_PREFIX = ""


def _request(method: str, url: str, token: str, body: dict | None = None) -> tuple[int, dict, dict]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
            payload = resp.read().decode("utf-8", errors="replace")
            return resp.status, (json.loads(payload) if payload else {}), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            parsed = {"raw": payload}
        return exc.code, parsed, dict(exc.headers or {})


def run_one(base: str, token: str, scenario: dict, rep: int, label: str, out_dir: pathlib.Path, prefix: str) -> dict:
    out_path = out_dir / f"{scenario['id']}-r{rep}.json"
    if out_path.exists():
        return {"id": scenario["id"], "rep": rep, "skipped": True}
    conversation = f"{CONVERSATION_PREFIX}-{label}-{scenario['id']}-r{rep}-{uuid.uuid4().hex[:6]}"
    body = {"model": MODEL_NAME, "conversation": conversation, "input": prefix + scenario["prompt"]}
    started = time.time()
    status, payload, headers = 0, {}, {}
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        status, payload, headers = _request("POST", base + RESPONSES_PATH, token, body)
        if status not in RETRY_STATUSES:
            break
        time.sleep(RETRY_SLEEP_SECONDS * attempt)
    ended = time.time()
    session_id = headers.get(SESSION_HEADER) or headers.get(SESSION_HEADER.lower()) or ""
    session, messages = {}, {}
    if session_id:
        time.sleep(POST_TURN_SETTLE_SECONDS)
        _, session, _ = _request("GET", base + SESSION_PATH.format(sid=session_id), token)
        _, messages, _ = _request("GET", base + MESSAGES_PATH.format(sid=session_id), token)
    calls = []
    for item in payload.get("output", []) if isinstance(payload, dict) else []:
        if item.get("type") == "function_call":
            args = item.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    pass
            calls.append({"name": item.get("name"), "arguments": args})
    result = {
        "label": label,
        "scenario": scenario["id"],
        "rep": rep,
        "conversation": conversation,
        "session_id": session_id,
        "http_status": status,
        "started": started,
        "ended": ended,
        "wall_seconds": round(ended - started, 2),
        "tool_calls": calls,
        "usage": payload.get("usage") if isinstance(payload, dict) else None,
        "final_text": "\n".join(
            part.get("text", "")
            for item in (payload.get("output", []) if isinstance(payload, dict) else [])
            if item.get("type") == "message"
            for part in (item.get("content") or [])
            if isinstance(part, dict)
        ),
        "session": session,
        "messages": messages,
        "raw_error": payload if status >= 400 else None,
    }
    out_path.write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
    return {"id": scenario["id"], "rep": rep, "status": status, "calls": [c["name"] for c in calls], "seconds": result["wall_seconds"]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--token", required=True)
    ap.add_argument("--scenarios", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--label", required=True)
    ap.add_argument("--reps", type=int, default=DEFAULT_REPS)
    ap.add_argument("--parallel", type=int, default=DEFAULT_PARALLEL)
    ap.add_argument("--only", default="", help="comma-separated scenario ids")
    ap.add_argument("--prefix", default=DEFAULT_PROMPT_PREFIX, help="text prepended to every prompt")
    args = ap.parse_args()
    scenarios = json.loads(pathlib.Path(args.scenarios).read_text(encoding="utf-8"))["scenarios"]
    if args.only:
        wanted = set(args.only.split(","))
        scenarios = [s for s in scenarios if s["id"] in wanted]
    out_dir = pathlib.Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    jobs = [(s, rep) for rep in range(1, args.reps + 1) for s in scenarios]
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.parallel) as pool:
        futures = [pool.submit(run_one, args.base, args.token, s, rep, args.label, out_dir, args.prefix) for s, rep in jobs]
        for fut in concurrent.futures.as_completed(futures):
            print(json.dumps(fut.result()), flush=True)


if __name__ == "__main__":
    main()
