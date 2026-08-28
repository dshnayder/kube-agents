#!/usr/bin/env python3
"""Ask the real agent each probe and score what it answers and how it got there.

This is the experiment. The companion `ladder.py` measures what each access
design *can* put in front of a model; this measures what the model actually
does with it. The two disagree, which is the point: the ladder scores a
history probe unanswerable under content passing, and the agent answers it
anyway by leaving the workspace protocol for `gh api`.

The prompt is byte-identical across arms. The only thing that changes between
arm A and arm B is `spec.harness.experimental.shellSandbox.contentWorkspaces`
on the PlatformAgent, which decides whether `inspect-repository` speaks the
content verbs or falls back to a leased checkout on the shared volume. So any
difference in the answer or the route is a property of the access design and
not of the wording.

Delegation is not bypassed. The chat profile is a front door with no terminal,
so it files a kanban card and the platform worker does the repository work;
that worker is the subject. The runner polls the same conversation until the
card settles, then reads the worker's own log out of the pod for the route
trace.

Usage:
    agent_probe.py --arm B --rung 200 --out results/agent-B-r200.json
    agent_probe.py --arm B --rung 200 --only P01,P07 --workers 4
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ENDPOINT = os.environ.get("AGENT_ENDPOINT", "http://127.0.0.1:8642/v1/responses")
TOKEN = os.environ.get("PLATFORM_AGENT_TOKEN", "")
# The read rungs all run against the one long-lived corpus; the write rung takes
# a throwaway repository per arm, because a pull request the previous arm opened
# is still findable after it is closed. new_write_repo.sh makes those and prints
# the value to put here.
REPO = os.environ.get("GITAB_REPO", "dshnayder-org/infra")
CONTEXT = "gke_YOUR-PROJECT_YOUR-REGION_YOUR-CLUSTER"
NAMESPACE = "kubeagents-system"
GATEWAY = "deploy/platform-agent-gateway"
LOG_DIR = "/opt/data/kanban/logs"

TASK_ID = re.compile(r"\bt_[0-9a-f]{8}\b")

# The gateway rewrites an empty assistant message rather than sending nothing,
# and the note it substitutes is prose the probe never asked for. Stripped
# before a reply is read as an answer; see `still_pending`.
SANITISED_NOTE = re.compile(r"\[System:[^\]]*\]")

# The instruction half of the prompt is fixed. It states the goal and the
# evidence standard and says nothing about how to reach the repository -- which
# route the agent picks is the measurement, so naming one would destroy it.
PREAMBLE = (
    "You have access to the git repository {repo} on branch {branch}.\n\n"
    "Answer the question below from that repository. Quote the exact values you "
    "find and name the file paths they came from. If the repository does not "
    "contain the answer, say so explicitly rather than guessing.\n\n"
    "Question: {question}"
)

POLL = (
    "What is the status of task {ids}? If it has finished, paste its full "
    "result here verbatim. If it is still running, reply with exactly PENDING."
)

# Route markers, matched against the worker's own log.
#
# `skill` is deliberately separate from the mechanism underneath it. The
# inspect-repository skill is the sanctioned entry point in *both* arms -- it
# speaks the workspace verbs when they exist and falls back to a leased
# checkout when they do not -- so its presence says the agent used the front
# door, not which design served it. Counting it as arm B's route was wrong and
# made arm A look like it was content passing.
#
# `workspace_http` is the content protocol proper. `git` is a real checkout.
# `ghapi` is the GitHub REST API: the escape hatch that neither design intends
# and that neither arm blocks, so its rate is the number worth reading.
ROUTES = {
    "skill": ("inspect_repository", "inspect-repository"),
    # Arm C's door. Named separately from `skill` because the two are not the
    # same door: arm C hides inspect-repository and puts this in its place.
    "vcs": ("vcs.py",),
    "workspace_http": ("/v1/workspace/",),
    "git": (
        "git clone", "git log", "git ls-files", "git show", "git cat-file",
        "git rev-parse", "git diff", "git status", "git checkout",
    ),
    # Opening a change proposal, which is the sanctioned write ending in every
    # arm -- `gh pr create` is what the skill itself is documented to run. This
    # used to fall under `ghapi` and the conflation flattered arm B: arm A's
    # write probes scored 2/4 "escaped" for doing the one thing the write path
    # exists to do, while arm B's genuine detours scored the same.
    "propose": ("gh pr create",),
    # The escape hatch proper: reads that go around whatever access design the
    # arm is testing. `gh pr view/list/diff` is here rather than under
    # `propose` because reading a pull request is a read, and reading it this
    # way is a read the arm's protocol was supposed to serve.
    "ghapi": (
        "gh api", "gh search", "gh repo ",
        "gh pr view", "gh pr list", "gh pr diff",
    ),
}


def post(conversation: str, text: str, timeout: float = 900) -> dict:
    body = json.dumps(
        {"model": "model-default", "conversation": conversation, "input": text}
    ).encode()
    request = urllib.request.Request(
        ENDPOINT,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {TOKEN}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        return {"_error": f"HTTP {exc.code}: {exc.read()[:300].decode('utf-8', 'replace')}"}
    except Exception as exc:  # a turn that dies is data, not a crash
        return {"_error": f"{type(exc).__name__}: {exc}"}


def answer_text(payload: dict) -> str:
    parts = []
    for item in payload.get("output", []):
        if item.get("type") != "message":
            continue
        for chunk in item.get("content", []):
            if chunk.get("text"):
                parts.append(chunk["text"])
    return "\n".join(parts)


def tool_names(payload: dict) -> list[str]:
    return [
        item.get("name", "")
        for item in payload.get("output", [])
        if item.get("type") == "function_call"
    ]


def card_log(task_ids: list[str]) -> str:
    """The worker's transcript, read from the agent volume.

    The response envelope carries the front door's turn, not the worker's, so
    the route it took is only visible here.
    """
    if not task_ids:
        return ""
    names = " ".join(f"{LOG_DIR}/{t}.log" for t in task_ids)
    try:
        done = subprocess.run(
            [
                "kubectl", f"--context={CONTEXT}", "-n", NAMESPACE,
                "exec", GATEWAY, "-c", "platform-agent", "--",
                "sh", "-lc", f"cat {names} 2>/dev/null",
            ],
            capture_output=True, text=True, timeout=120,
        )
        return done.stdout
    except subprocess.SubprocessError:
        return ""


def classify_route(log: str) -> dict:
    lowered = log.lower()
    return {
        name: sum(lowered.count(marker.lower()) for marker in markers)
        for name, markers in ROUTES.items()
    }


def _groups(probe: dict) -> list[list[str]]:
    """`must_contain` entries, normalised to alternative groups."""
    out = []
    for entry in probe.get("must_contain", []):
        out.append([entry] if isinstance(entry, str) else list(entry))
    return out


def _earliest(text: str, needles: list[str]) -> int:
    hits = [text.index(n.lower()) for n in needles if n.lower() in text]
    return min(hits) if hits else -1


def score(probe: dict, answer: str, route: dict, log: str) -> dict:
    lowered = answer.lower()
    groups = _groups(probe)
    satisfied = [g for g in groups if _earliest(lowered, g) >= 0]
    missing = [g for g in groups if _earliest(lowered, g) < 0]

    # Contamination, judged the way a reader would. A good answer names the
    # superseded values in order to rule them out, so a mention is not a fault.
    # It is a fault when the stale value is put ahead of the correct one, or
    # stands in for it -- that is the case where a reader takes away the wrong
    # answer. Correct position is the earliest hit across all required groups.
    correct_at = min(
        (_earliest(lowered, g) for g in satisfied if _earliest(lowered, g) >= 0),
        default=-1,
    )
    contaminating = []
    for stale in probe.get("must_not_contain", []):
        at = lowered.find(stale.lower())
        if at < 0:
            continue
        if correct_at < 0 or at < correct_at:
            contaminating.append(stale)

    must_not = contaminating
    answered = not missing and not contaminating

    # Whether the agent stayed on the route its arm intends. Content passing
    # intends the workspace verbs; the shared volume intends git. Reaching for
    # the REST API means the design did not carry the task.
    used = [name for name, count in route.items() if count]

    return {
        "probe": probe["id"],
        "class": probe["class"],
        "needs": probe.get("needs", []),
        "answered": answered,
        "must_contain_hit": len(satisfied),
        "must_contain_total": len(groups),
        "must_contain_missing": missing,
        "contamination": len(must_not),
        "contamination_hits": must_not,
        "mentions_superseded": [
            s for s in probe.get("must_not_contain", []) if s.lower() in lowered
        ],
        "route": route,
        "routes_used": used,
        # How many `gh` calls died because there is no gh. Under the sealed arm
        # the binary is gone, so an attempt that was turned back and then
        # re-routed is the abstraction working; scoring it the same as a call
        # that reached GitHub would report the opposite of what happened.
        "gh_absent": sum(
            log.count(marker) for marker in
            ("gh: command not found", "gh: not found", "command not found: gh")
        ),
        "worker_log_bytes": len(log.encode()),
        "answer": answer,
    }


def gh_json(*args: str):
    done = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=120)
    if done.returncode != 0:
        raise SystemExit(f"gh {' '.join(args)} failed: {done.stderr.strip()}")
    return json.loads(done.stdout or "null")


def target_paths(probe: dict) -> list[str]:
    """The paths this probe's pull request must change, from its own asks.

    Not `gold`, which is what the agent has to read. Kept identical to
    `verify_writes.target_paths` on purpose: the follow-up has to find the same
    pull request the verifier will score.
    """
    return sorted(
        {*(probe.get("expect_files") or {}), *(probe.get("expect_mode") or {})}
    )


def proposal_touching(paths: list[str]) -> int | None:
    """The open pull request that changes one of `paths`, or None.

    Found in the repository rather than parsed out of the dependency's answer.
    A probe that timed out still opened its pull request -- arm C's did, fifty
    minutes after the runner stopped listening -- and a follow-up that reads the
    answer text instead of the repository would conclude there was nothing to
    follow up on.
    """
    for entry in gh_json("api", f"repos/{REPO}/pulls?state=open&per_page=100") or []:
        files = gh_json("api", f"repos/{REPO}/pulls/{entry['number']}/files")
        if any(changed["filename"] in paths for changed in files or []):
            return entry["number"]
    return None


def commentable_line(patch: str, prefer: str) -> int:
    """A right-hand line number that is inside `patch`, favouring one that
    mentions `prefer`.

    A review comment has to hang off a line the diff actually shows, and the
    arms do not produce the same diff for the same ask. Arm A rewrote the header
    and the field together, giving one hunk covering new-file lines 1-13; arm B
    added a two-line note and changed one field, giving hunks at 2-9 and 12-15.
    A hardcoded line 11 sits inside the first and in the gap between the second
    pair, so the seeding worked for one arm and was rejected by GitHub for the
    next -- which is a harness difference between arms masquerading as an arm
    difference, on the one probe the whole write rung turns on.
    """
    candidates: list[tuple[int, str]] = []
    lineno = 0
    for line in patch.splitlines():
        if line.startswith("@@"):
            # `@@ -old,count +new,count @@` -- the right-hand start is what the
            # comment's `line` is measured in.
            lineno = int(line.split("+")[1].split(",")[0].split(" ")[0]) - 1
            continue
        if line.startswith("-"):
            continue
        lineno += 1
        if line.startswith((" ", "+")):
            candidates.append((lineno, line[1:]))
    if not candidates:
        raise SystemExit(f"no commentable line in patch:\n{patch}")
    for number, text in candidates:
        if prefer in text:
            return number
    return candidates[-1][0]


def seed_review(probe: dict) -> None:
    """Make the thing the probe asserts actually true before asking it.

    P23 opens "a reviewer on your open pull request asked for...". Nothing ever
    posted that comment, so the sentence was false in every arm: three agents
    went and looked, found a pull request with zero reviews and zero comments,
    and each declined to guess which of eighteen `effectiveFrom` fields was
    meant. That is the right answer to the question they were given, and it was
    scored as three arms failing a write probe.

    Posting it for real also makes P23 measure what the experiment is about. The
    comment is on the forge, not in the repository, so reaching it is a
    forge-read -- the capability arm B has no verb for and the one arm C's
    broker was silently refusing.
    """
    body = probe.get("review_comment")
    if not body:
        return
    targets = target_paths(probe)
    number = proposal_touching(targets)
    if number is None:
        print(
            f"{probe['id']}: no open pull request touches {targets}; "
            "its dependency opened none, so the follow-up has nothing to revise",
            file=sys.stderr, flush=True,
        )
        return
    # Read the object and pick the field out here. `gh api --jq .head.sha`
    # prints a bare SHA, which is not JSON, so asking `gh_json` for it raises a
    # JSONDecodeError from inside the seeding step -- and that took down a wave
    # of answers that had cost forty minutes to collect.
    head = (gh_json("api", f"repos/{REPO}/pulls/{number}").get("head") or {}).get("sha")
    files = gh_json("api", f"repos/{REPO}/pulls/{number}/files") or []
    patch = next(
        (f.get("patch", "") for f in files if f["filename"] == targets[0]), ""
    )
    line = commentable_line(patch, probe.get("review_anchor", ""))
    # A review comment, not an issue comment: it hangs off a line of the diff,
    # which is where a reviewer actually leaves this and what a forge-read verb
    # has to reach. An issue comment would be readable from the cheaper
    # endpoint and would not exercise the same path.
    done = subprocess.run(
        ["gh", "api", "--method", "POST",
         f"repos/{REPO}/pulls/{number}/comments",
         "-f", f"body={body}",
         "-f", f"commit_id={head}",
         "-f", f"path={targets[0]}",
         "-F", f"line={line}",
         "-f", "side=RIGHT"],
        capture_output=True, text=True, timeout=120,
    )
    if done.returncode != 0:
        # Loud and fatal. A run that carries on without the comment asks P23
        # about a review that does not exist, and the arm's correct refusal to
        # invent one is then written up as a capability failure.
        raise SystemExit(
            f"{probe['id']}: could not seed the review comment on #{number} "
            f"at line {line}: {done.stderr.strip()}"
        )
    print(f"{probe['id']}: seeded a review comment on #{number} line {line}",
          file=sys.stderr, flush=True)


def still_pending(reply: str) -> bool:
    """Is this poll reply the card saying "not yet" rather than an answer?

    An exact match on PENDING is not enough. The gateway sanitises an empty
    assistant message by replacing it with a note, and the note lands in front
    of the word: `[System: Empty message content sanitised to satisfy protocol]
    PENDING`. That is not equal to "PENDING", so the loop read it as the answer,
    stopped polling, and recorded the probe unanswered -- while the worker
    carried on and, fifty minutes later, opened exactly the pull request the
    probe had asked for. One arm's write probe was scored as a capability
    failure on the strength of a string comparison.
    """
    stripped = reply.strip()
    if not stripped:
        return True
    without_notes = SANITISED_NOTE.sub("", stripped).strip()
    return without_notes.upper() == "PENDING" or not without_notes


def run_probe(probe: dict, arm: str, rung: int, stamp: str) -> dict:
    branch = f"git-access-ab/r{rung}"
    # A follow-up probe rides its dependency's conversation. P23 asks the agent
    # to revise "your open pull request", which is only a meaningful request to
    # the session that opened one -- run it fresh and it is a different, easier
    # question. Probes carrying `depends_on` must therefore run serially, after
    # the probe they name.
    thread = probe.get("depends_on", probe["id"])
    conversation = f"gitab-{arm}-r{rung}-{thread}-{stamp}"
    prompt = PREAMBLE.format(
        repo=REPO, branch=branch, question=probe["question"]
    )

    started = time.time()
    first = post(conversation, prompt)
    if "_error" in first:
        return {"probe": probe["id"], "arm": arm, "rung": rung,
                "error": first["_error"], "seconds": round(time.time() - started, 1)}

    tokens = dict(first.get("usage", {}))
    text = answer_text(first)
    tools = tool_names(first)
    task_ids = list(dict.fromkeys(TASK_ID.findall(text)))
    turns = 1

    # The front door answers with an acknowledgement, so keep taking turns on
    # the same conversation until the card reports a result. The bound is wall
    # clock rather than turn count: a slow worker is not a stuck one.
    deadline = started + 2400
    while task_ids and time.time() < deadline:
        time.sleep(45)
        turn = post(conversation, POLL.format(ids=", ".join(task_ids)))
        turns += 1
        if "_error" in turn:
            continue
        for key, value in turn.get("usage", {}).items():
            if isinstance(value, int):
                tokens[key] = tokens.get(key, 0) + value
        tools += tool_names(turn)
        reply = answer_text(turn)
        if still_pending(reply):
            continue
        if reply.strip():
            text = reply
            break

    log = card_log(task_ids)

    # Kept, not just measured. `ghapi` counts the string `gh api` in the log,
    # and under a sealed arm that string can mean either of two opposite
    # things: a call that reached GitHub, or a call the /opt/vcs/bin/gh stub
    # turned back. The write rung recorded one `ghapi` hit for arm C and there
    # is no way to tell now which it was, because only the log's length was
    # kept. One run is forty minutes; the log is thirty kilobytes.
    logs = Path("results/logs")
    logs.mkdir(parents=True, exist_ok=True)
    (logs / f"{arm}-{probe['id']}-{stamp}.log").write_text(log)

    route = classify_route(log)
    row = score(probe, text, route, log)
    row.update({
        "arm": arm,
        "rung": rung,
        "task_ids": task_ids,
        "turns": turns,
        "tools": tools,
        "tokens": tokens,
        "seconds": round(time.time() - started, 1),
    })
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm", required=True, choices=("A", "B", "C"))
    parser.add_argument("--rung", required=True, type=int)
    parser.add_argument("--probes", type=Path, default=Path("probes.json"))
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--only", default="")
    parser.add_argument("--skip-classes", default="")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--stamp", required=True)
    parser.add_argument(
        "--resume-dependents", action="store_true",
        help="skip the first wave, keep the rows already in --out, and run only "
             "the probes that depend on them. For a rung that died between the "
             "waves: the answers cost forty minutes each and the dependent runs "
             "in the conversation its dependency opened, so re-running the first "
             "wave under the same stamp would replay a finished card.",
    )
    arguments = parser.parse_args()

    if not TOKEN:
        raise SystemExit("PLATFORM_AGENT_TOKEN is not set")

    probes = json.loads(arguments.probes.read_text())["probes"]
    wanted = set(arguments.only.split(",")) if arguments.only else None
    skipped = set(arguments.skip_classes.split(",")) if arguments.skip_classes else set()
    probes = [
        p for p in probes
        if (wanted is None or p["id"] in wanted) and p["class"] not in skipped
    ]

    # `run_probe` says a probe carrying `depends_on` must run after the probe it
    # names, and until now nothing made that true: every probe went into the
    # pool at once. P23 and P21 then raced inside the one conversation they
    # share, and P23 -- "a reviewer on your open pull request asked for..." --
    # reached an agent whose pull request did not exist yet. All three arms
    # answered it the same way, correctly: there is no such pull request and no
    # such review, name the target and I will make the change. Three arms
    # scored as failing one write probe, for a question the harness had made
    # unanswerable.
    #
    # So the dependents are held back and run once the wave they depend on has
    # finished. Two waves is all this needs; a chain three deep would need a
    # real topological sort and there is no probe that wants one.
    independent = [p for p in probes if not p.get("depends_on")]
    dependent = [p for p in probes if p.get("depends_on")]
    rows: list[dict] = []
    if arguments.resume_dependents:
        rows = json.loads(arguments.out.read_text())["rows"]
        rows = [r for r in rows if r["probe"] not in {p["id"] for p in dependent}]
        independent = []

    finished = {p["id"] for p in independent} | {r["probe"] for r in rows}
    orphans = [p["id"] for p in dependent if p["depends_on"] not in finished]
    if orphans:
        # Running it anyway is what produced the invalid rung. A dependent whose
        # dependency was filtered out by --only is a different, easier question
        # wearing the same probe id.
        raise SystemExit(
            f"{', '.join(orphans)} depend on probes this selection does not run; "
            "add them to --only or drop the dependents"
        )

    def run_wave(wave: list[dict]) -> None:
        if not wave:
            return
        with concurrent.futures.ThreadPoolExecutor(arguments.workers) as pool:
            futures = {
                pool.submit(
                    run_probe, p, arguments.arm, arguments.rung, arguments.stamp
                ): p
                for p in wave
            }
            for future in concurrent.futures.as_completed(futures):
                probe = futures[future]
                try:
                    row = future.result()
                except Exception as exc:
                    row = {"probe": probe["id"], "arm": arguments.arm,
                           "rung": arguments.rung,
                           "error": f"{type(exc).__name__}: {exc}"}
                rows.append(row)
                print(
                    f"{row['probe']} answered={row.get('answered')} "
                    f"routes={row.get('routes_used')} {row.get('seconds')}s",
                    file=sys.stderr, flush=True,
                )

    def save() -> None:
        rows.sort(key=lambda r: r["probe"])
        arguments.out.parent.mkdir(parents=True, exist_ok=True)
        arguments.out.write_text(json.dumps(
            {"arm": arguments.arm, "rung": arguments.rung,
             "stamp": arguments.stamp, "rows": rows}, ensure_ascii=False,
            indent=2))

    run_wave(independent)
    # Written before the second wave starts. A probe answer is forty minutes of
    # a real agent's work and there is no way to recover one that was collected
    # and then dropped, so it goes to disk as soon as it exists rather than at
    # the end of a run that can still fail -- and one did, on a typo in the
    # seeding step, taking three answered probes with it.
    save()
    for probe in dependent:
        seed_review(probe)
    run_wave(dependent)
    save()
    answered = sum(1 for r in rows if r.get("answered"))
    print(f"\n{arguments.arm} r{arguments.rung}: {answered}/{len(rows)} answered",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
