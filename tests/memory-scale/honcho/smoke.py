#!/usr/bin/env python3
"""End-to-end smoke test for the in-cluster Honcho.

Proves the whole path works before any corpus is seeded: workspace/peer/session
creation, message ingest, embedding via LiteLLM, deriver processing, and search.
"""
import json
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:18800"
WS = "smoke"


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:400]


def step(label, method, path, body=None, ok=(200, 201)):
    code, out = call(method, path, body)
    mark = "ok " if code in ok else "FAIL"
    print(f"  [{mark}] {code} {label}")
    if code not in ok:
        print(f"         {str(out)[:400]}")
    return code, out


print("== setup ==")
step("create workspace", "POST", "/v3/workspaces", {"name": WS})
step("create peer", "POST", f"/v3/workspaces/{WS}/peers", {"name": "probe-operator"})
step("create session", "POST", f"/v3/workspaces/{WS}/sessions", {"name": "smoke-1"})
step(
    "add peer to session",
    "POST",
    f"/v3/workspaces/{WS}/sessions/smoke-1/peers",
    {"probe-operator": {}},
)

print("== ingest ==")
FACTS = [
    "ADR-2026-052: service account keys are banned; use Workload Identity Federation instead.",
    "ADR-2026-047: new services must use the Gateway API, not ingress-nginx.",
    "ADR-2026-044: audit logs are retained for seven years.",
]
code, out = step(
    "post 3 messages",
    "POST",
    f"/v3/workspaces/{WS}/sessions/smoke-1/messages",
    # The wire field is peer_id: MessageCreate declares
    # `peer_name: str = Field(alias="peer_id")` (src/schemas/api.py:256).
    {"messages": [{"peer_id": "probe-operator", "content": f} for f in FACTS]},
)

print("== wait for deriver ==")
deadline = time.time() + 300
hits = 0
while time.time() < deadline:
    time.sleep(15)
    code, out = call(
        "POST",
        f"/v3/workspaces/{WS}/search",
        {"query": "What is the policy on service account keys?", "limit": 5},
    )
    if code == 200:
        items = out.get("items", out) if isinstance(out, dict) else out
        hits = len(items) if isinstance(items, list) else 0
        print(f"  search -> {code}, {hits} hits  (t+{int(time.time()-deadline+300)}s)")
        if hits:
            print("  first hit:", json.dumps(items[0])[:300])
            break
    else:
        print(f"  search -> {code} {str(out)[:200]}")

print("== result ==")
print("PASS" if hits else "NO HITS — check deriver logs and embedding wiring")
sys.exit(0 if hits else 1)
