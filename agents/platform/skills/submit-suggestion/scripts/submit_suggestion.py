#!/usr/bin/env python3
"""
GKE Platform Agent — GitOps PR Suggestion Submitter

Two commands, because a change proposal takes two turns of the agent's shell
and the agent has to know *where* to work in between:

    prepare  -> bring the repository down, take the branch, print the workspace
    (agent edits files in that workspace)
    submit   -> record the change, send it up, open or refresh the proposal

Everything here is the version-control verbs. `prepare` is `clone` plus
`branch`; `submit` is `commit`, `publish` and one of `proposal-create` /
`proposal-update`. There is no `gh` in this file, no token in this container,
and no directory shared with the process that holds the credential.

Three things that used to be here are gone with it, and each is worth naming
because their absence is what makes the rest simple.

**The lease is gone.** It existed because clones lived on a volume six audit
crons and every kanban worker shared, so "which clone is mine" was a real
question with a wrong answer. `clone` writes one copy per repository under this
container's own scratch root and refuses to replace a copy holding work that
was never published — which is the same protection, taken from the thing being
protected rather than from a file beside it. `--force` is the way past it.

**Content mode is gone.** It was the other answer to "the agent must not author
a `.git/config` the credential process will read", and it bought that by taking
the checkout away from the agent entirely — no `.git`, so no filter driver, no
alias, no hook path. The verbs get the same result the other way round: the
checkout is here and the credential is not, so a hook in it runs against
nothing worth having. With a real repository on disk again, `list` and `fetch`
have nothing to do; `ls` and `cat` are back.

**`--force-with-lease` is gone**, and nothing replaced it. `publish` is
fast-forward only. A second round on a branch extends it; a branch that
diverged is refused by name rather than overwritten.
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

# Append global scripts path to allow importing the shared helpers
sys.path.append("/opt/defaults/scripts")
sys.path.append("/opt/data/scripts")
# The same directory in a source checkout, where nothing is staged into /opt.
sys.path.append(str(Path(__file__).resolve().parents[3] / "scripts"))

import gitops_workspace
import vcs_client
from github_token_refresh import log


# Branches a suggestion may never target. `main` and `master` are the GitOps
# rollout branches; `production` is the convention some fleets use instead.
#
# Not the only guard, and deliberately the weakest of the three: the broker
# refuses the remote's own default branch whatever this list says, and the
# forge's branch protection refuses whatever the broker lets through. This one
# is here to fail early, in the container the agent can read the message in.
PROTECTED_BRANCHES = {"main", "master", "production"}

# The one directory `--body-file` may name. The same bound `pr_conversation.py`
# and `github-issue-resolver`'s resolver put on their own body paths, for the
# same reason: what the file holds is posted publicly, so a path the model
# supplies is a way to publish any file the agent container can read.
SCRATCH_DIR = "/opt/data/scratch"


def check_branch(branch_name: str) -> str:
    branch = (branch_name or "").strip()
    if not branch:
        raise ValueError("--branch is required and must not be empty")
    # Compare the short name: "refs/heads/main" is not in PROTECTED_BRANCHES,
    # but pushing it moves main all the same.
    short = branch.lower()
    if short.startswith("refs/heads/"):
        short = short[len("refs/heads/"):]
    if short in PROTECTED_BRANCHES:
        raise ValueError(
            f"CRITICAL SECURITY REFUSAL: Publishing to protected branch "
            f"'{branch_name}' is strictly blocked by GKE SRE guardrails!"
        )
    return branch


def validate_repo(repo: str) -> str:
    """Ensure repo is formatted as owner/name and is in the managed repos allowlist if configured."""
    if not repo or not gitops_workspace.is_valid_repo_slug(repo):
        raise ValueError(f"Invalid repository format: {repo!r}. Expected 'owner/name'.")
    managed = gitops_workspace.get_managed_github_repos()
    if managed and repo not in managed:
        raise ValueError(
            f"Repository {repo!r} is not in the managed repositories list: {managed}"
        )
    return gitops_workspace.validate_repo_org(repo)


def open_proposal(repo: str, branch: str) -> dict | None:
    """The open change proposal whose source is `branch`, or None.

    Asked of the forge as a filter rather than by listing and matching here:
    see the `source` parameter's own note. Three things this does that the
    `gh pr view <branch>` it replaces did not.

    It does not count a merged or closed proposal. Branch names here are
    derived from the change (`platform-agent/<type>-<target>`), so a branch is
    reused after its proposal merges, and asking for "the proposal on this
    branch" answered with that one.

    It does not read a failed lookup as an empty one. An expired credential and
    "this branch has no proposal" are opposite answers, and collapsing them
    into "" sends the caller down the path that rewrites a description it was
    told to keep. A failure here raises.

    And it does not need a forge's vocabulary. The answer is a proposal with a
    `number`, a `target` and a `url`, whichever forge the repository is on.
    """
    answer = vcs_client.forge(
        "proposal-list",
        {"source": branch, "state": "open", "limit": 1},
        repository=repo,
    )
    proposals = answer.get("proposals") or []
    return proposals[0] if proposals else None


def handle_prepare(args) -> int:
    """Bring the repository down and stand on the branch this change goes on.

    Two shapes, and which one runs is decided by the forge rather than by a
    flag: a branch with an open proposal on it is one this run is *adding to*,
    so the copy is taken of that branch and its revisions come with it. A
    branch with no open proposal is one this run is starting, so the copy is
    taken of the base and the branch is cut from it.

    Getting this wrong destroyed work, which is why it is decided rather than
    assumed. Step 5 of the SKILL runs `prepare --branch <source>` against the
    branch an open proposal is already sitting on. Cutting that branch afresh
    from the base does not amend the proposal — it replaces every reviewed
    revision with one that no longer contains them.
    """
    branch = check_branch(args.branch)
    # `--repo` first, as everything downstream reads it. Ignoring it silently
    # opened the default repository under a flag that named another one, and a
    # fleet whose cards target several GitOps repositories writes every
    # suggestion to whichever one `resolve_repo` happens to answer with.
    repo = args.repo or gitops_workspace.resolve_repo()
    validate_repo(repo)

    proposal = open_proposal(repo, branch)
    if proposal:
        log(f"'{branch}' already has an open proposal; taking a copy of it.")
        cloned = vcs_client.clone(repo, branch=branch, force=args.force)
        base = proposal["target"]
        started_from = branch
    else:
        cloned = vcs_client.clone(repo, force=args.force)
        base = cloned["branch"]
        vcs_client.branch(repo, branch)
        started_from = base

    print(json.dumps({
        "workspace": cloned["path"],
        "repo": repo,
        "branch": branch,
        "base": base,
        "started_from": started_from,
        "proposal": (proposal or {}).get("url", ""),
    }))
    return 0


def pending_changes(session: dict) -> str:
    """What the working copy holds that its revision does not."""
    return vcs_client.local(session, ["status", "--porcelain"], "status")["stdout"].strip()


def handle_submit(args) -> int:
    body = _submit_body(args)
    if not args.keep_description and not (args.title and body):
        raise ValueError(
            "--title and one of --body / --body-file are required unless "
            "--keep-description is given."
        )
    branch = check_branch(args.branch)

    session = vcs_client.resolve_session(args.repo)
    repo = args.repo or session["spec"]
    validate_repo(repo)

    current = vcs_client.current_branch(session)
    if current != branch:
        raise ValueError(
            f"the copy at {session['path']} is on branch '{current}', not "
            f"'{branch}'. Make your changes on '{branch}' before submitting, "
            "or pass the branch you are actually on."
        )

    # Before anything is sent. Two of the three refusals below are ones the
    # caller cannot retry out of once the revisions are on the forge: discover
    # after the publish that there is no proposal to keep the description of,
    # and the retry the message asks for finds the work already published and
    # nothing left to commit.
    proposal = open_proposal(repo, branch)
    if args.keep_description:
        if not proposal:
            raise RuntimeError(
                f"--keep-description was given but no proposal is open for "
                f"'{branch}' on {repo}. There is no description to keep. Open "
                "it with a --title and a --body-file first."
            )
        if args.title:
            # Not silently. `--keep-description` keeps the title along with the
            # body, so a title passed here is read and discarded, and a caller
            # who passed one believes it landed.
            log(
                "--title is ignored under --keep-description: the title is part "
                "of the description being kept."
            )

    # What the change merges into. From the open proposal when there is one,
    # because that is where it already says it is going and moving it is not
    # this script's call; from the branch the copy came down on otherwise.
    base = args.base or (proposal or {}).get("target") or session["branch"]
    if base == branch:
        raise ValueError(
            f"the base and the branch are both '{branch}', so there is nothing "
            "to propose this onto. Pass --base <branch> naming what it merges "
            "into."
        )

    pending = pending_changes(session)
    if pending:
        if not args.title:
            raise ValueError(
                f"{session['path']} has uncommitted changes and no --title to "
                "record them under. Pass --title, or commit them yourself with "
                "`vcs.py commit --message ...` before submitting."
            )
        log(f"Recording {len(pending.splitlines())} pending change(s)...")
        vcs_client.commit(args.title, spec=repo)

    log(f"Publishing '{branch}' to {repo}...")
    # `advance` exactly when the copy was taken of this branch rather than of
    # the base — the second round on an open proposal. `publish` refuses to
    # write to the branch a copy came down on otherwise, and that refusal is
    # the one that caught a worker fast-forwarding a branch it had cloned.
    vcs_client.publish(repo, target=base, advance=session["branch"] == branch)

    url = _land_proposal(repo, branch, base, args.title, body, proposal, args.keep_description)
    log(f"PR SUBMITTED SUCCESSFULLY! 🏆 URL: {url}")

    # Print raw URL to stdout for the MCP tool to parse
    print(url)
    return 0


def _land_proposal(
    repo: str,
    branch: str,
    base: str,
    title: str,
    body: str,
    proposal: dict | None,
    keep_description: bool,
) -> str:
    """Open the proposal — or refresh the one that is already open.

    An existing proposal is the success case for a resubmission, not an error.
    Creating one for a branch that already has one fails *after* the revisions
    have landed, which is the worst possible shape: the reviewer sees the new
    work, the skill reports the whole submission as failed, so the agent
    retries, publishes again, and fails again — for as many rounds of feedback
    as the proposal gets.

    It is refreshed rather than merely located. Step 5 of the SKILL hands this
    a title and body written for the revisions it just published; leaving the
    old description in place would describe work the branch no longer contains.

    `keep_description` inverts that, for a caller that is not re-describing the
    change but adding to it — a conflict merge or a CI fix pushed onto a
    proposal that has been under human review. There the description is
    somebody else's work and rewriting it is pure loss, invisible in the
    output: the skill prints a URL and says nothing about the body.
    """
    if proposal and keep_description:
        log(f"Leaving the description of '{branch}' as its author wrote it.")
        return proposal["url"]
    if proposal:
        log(f"A proposal for '{branch}' is already open; updating it in place.")
        answer = vcs_client.forge(
            "proposal-update",
            {"number": proposal["number"], "title": title, "body": body},
            repository=repo,
        )
        return answer["proposal"]["url"]

    log(f"Opening a proposal for '{branch}' onto '{base}'...")
    try:
        answer = vcs_client.forge(
            "proposal-create",
            {"title": title, "body": body, "source": branch, "target": base},
            repository=repo,
        )
    except vcs_client.VcsError:
        # The race the pre-publish lookup leaves: a retried card, or a sibling
        # run, opened the proposal between that read and this write. Asking
        # again is the difference between reporting a submission that landed as
        # a failure and reporting it as what it is.
        raced = open_proposal(repo, branch)
        if not raced:
            raise
        log(f"A proposal for '{branch}' was opened while this run worked; updating it.")
        return _land_proposal(repo, branch, base, title, body, raced, keep_description)
    return answer["proposal"]["url"]


def _submit_body(args) -> str:
    """The description text, from `--body-file` if one was given.

    A change proposal's body is long, full of backticks, and assembled by a
    model into a shell command. Through argv it is one `$(...)` away from
    executing in the working copy and one stray backtick away from silently
    deleting its own text. A file is the channel the rest of this repository
    already uses for model-written prose — `pr_conversation.py reply` and
    `resolver.py report` both take a path — and the asymmetry was that the
    larger document went the other way.

    The path is confined the way both of those confine theirs, and for the
    reason they give: the file's contents are published, so an unbounded path
    is a way to put `/proc/self/environ` into a public description. Reaching
    for one is not something the agent has to intend — Step 5 of the SKILL has
    it read review comments, which are somebody else's text.

    `--body` stays because callers outside this repository pass it and short
    bodies are fine.
    """
    if not args.body_file:
        return args.body or ""
    # Resolved before the prefix test, so a symlink planted inside scratch
    # cannot reach out of it.
    scratch = os.path.realpath(SCRATCH_DIR)
    real = os.path.realpath(args.body_file)
    if not real.startswith(scratch + os.sep):
        raise ValueError(f"--body-file {args.body_file} resolves outside {scratch}.")
    if not os.path.isfile(real):
        raise ValueError(f"--body-file {args.body_file} does not exist.")
    body = Path(real).read_text(encoding="utf-8")
    if not body.strip():
        raise ValueError(f"--body-file {args.body_file} is empty.")
    return body


COMMANDS = ("prepare", "submit")

# Flags that named a thing this script no longer has. Accepted and ignored
# rather than removed, for one turn of the agent's shell: a card that ran
# `prepare` before the image rolled and `submit` after it would otherwise die
# on "unrecognized arguments" with its work committed and unpublished. Each
# names what took its place in the help text, and they go when the SKILL.md
# that documented them has been through a release.
RETIRED = {
    "--workspace": "the copy's path is in the session, not an argument",
    "--lease": "there is no shared volume to lease a clone on",
    "--handle": "there is no broker-side checkout to hold a handle to",
    "--base-sha": "`publish` checks ancestry against what it cloned",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Secure GitOps PR Suggestion Submitter")
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser(
        "prepare", help="Bring the repository down and take the branch"
    )
    prepare.add_argument("--branch", required=True, help="Branch to work on")
    prepare.add_argument("--repo", default=None, help="Target repository as owner/name")
    prepare.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing copy even if it holds unpublished work",
    )

    submit = subparsers.add_parser(
        "submit", help="Publish the branch and open or refresh the proposal"
    )
    submit.add_argument("--branch", required=True, help="Active Git branch name")
    # Not `required=True`: `--keep-description` submits without them, and
    # `handle_submit` refuses a call that gives neither. Argparse cannot express
    # "required unless" without a mutually-exclusive group that would also
    # forbid the legitimate `--title` + `--keep-description` combination.
    submit.add_argument("--title", default=None, help="Pull Request title")
    # One group of three, not two of two. `--keep-description` says the
    # description on the proposal is the one to publish, so a body handed over
    # beside it is a body the run would read and throw away.
    description = submit.add_mutually_exclusive_group()
    description.add_argument(
        "--body", default=None, help="Pull Request description body"
    )
    description.add_argument(
        "--body-file",
        default=None,
        help=f"File under {SCRATCH_DIR} holding the description; the safe "
             "channel for a long body",
    )
    description.add_argument(
        "--keep-description",
        action="store_true",
        help="Leave the open proposal's title and body as its author wrote them",
    )
    submit.add_argument("--repo", default=None, help="Target repository as owner/name")
    submit.add_argument(
        "--base", default=None,
        help="The branch this merges into (default: the open proposal's, else "
             "the branch the copy was taken of)",
    )

    for command in (prepare, submit):
        for flag in RETIRED:
            command.add_argument(
                flag, dest=f"retired_{flag.lstrip('-').replace('-', '_')}",
                default=None, help=argparse.SUPPRESS,
            )
    return parser


def warn_about_retired(args) -> None:
    for flag, replaced_by in RETIRED.items():
        if getattr(args, f"retired_{flag.lstrip('-').replace('-', '_')}", None):
            log(f"{flag} is no longer read: {replaced_by}.")


def normalise_argv(argv: list) -> list:
    """Accept the pre-`prepare` call shape, which had no subcommand at all.

    The skill used to invoke this with a bare `--branch/--title/--body`. A
    session already mid-flight when this ships must not die on "invalid choice",
    so an argv that does not name a command is read as `submit` — except a bare
    help request, which has to keep printing the help for the whole script.
    """
    argv = list(argv)
    if not argv or argv[0] in COMMANDS or argv[0] in ("-h", "--help"):
        return argv
    return ["submit", *argv]


def dispatch(argv: list) -> int:
    """Parse and run, letting failures out as themselves.

    Separate from `main` so a caller — the tests, mainly — can see the
    exception a refusal raises rather than an exit code.
    """
    args = build_parser().parse_args(normalise_argv(argv))
    warn_about_retired(args)
    return {"prepare": handle_prepare, "submit": handle_submit}[args.command](args)


def main():
    try:
        sys.exit(dispatch(sys.argv[1:]))

    except vcs_client.VcsError as e:
        # The forge's or the broker's refusal, with the code and detail the
        # SKILL's rules key on. Distinct from the generic failure below because
        # it is the one an agent can usually act on without an operator.
        log(f"REFUSED: {e}")
        log(json.dumps(e.as_json()))
        sys.exit(1)
    except PermissionError as e:
        log(f"REFUSED: {e}")
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        log("FATAL ERROR: GitOps subprocess execution failed!")
        log(f"Exit Code: {e.returncode}")
        if e.stderr:
            log(f"Stderr Output:\n{e.stderr.strip()}")
        if e.stdout:
            log(f"Stdout Output:\n{e.stdout.strip()}")
        sys.exit(1)
    except Exception as e:
        log(f"FATAL ERROR: GitOps suggestion submission failed: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
