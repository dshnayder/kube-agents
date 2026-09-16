"""Unit tests for the submit-suggestion skill's PR submitter.

The subject lives in `agents/platform/skills/submit-suggestion/scripts/`, but
the test lives here: CI discovers tests in exactly two directories
(.github/workflows/python-tests.yml), and that is not one of them. Loading it by
path keeps the coverage without a third discovery step.

Run:
  python3 -m unittest discover -s agents/platform/scripts -p 'test_submit_suggestion.py' -v

Real git against a local repository throughout, with the broker faked at the
one seam the script has: `vcs_client.call`. A recorded runner on this side
would make most of it vacuous — whether a second round extends a branch or
diverges from it is a question only real objects answer — while a real broker
would need a credential CI does not have. So the fake serves bundles out of a
real repository and pushes them back into it, and the proposals it keeps are a
table.
"""

from __future__ import annotations

import base64
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from itertools import count
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import gitops_workspace  # noqa: E402
import vcs_client  # noqa: E402

SUBJECT = (
    HERE.parent / "skills" / "submit-suggestion" / "scripts" / "submit_suggestion.py"
)
REAL_GIT = shutil.which("git") or "/usr/bin/git"


def _load_subject():
    spec = importlib.util.spec_from_file_location("submit_suggestion", SUBJECT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


submit_suggestion = _load_subject()

GIT_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@x",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@x",
}


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, **GIT_ENV},
    )


class FakeBroker:
    """A real repository on one side, a table of proposals on the other.

    Faithful where the script can tell the difference: `clone` bundles from a
    working copy the way the broker does, `publish` unbundles and pushes so the
    next clone sees the branch, and the proposals answer in the neutral shape
    with the forge's own vocabulary nowhere in them.
    """

    def __init__(self, origin: Path, scratch: Path):
        self.origin = origin
        self.scratch = scratch
        self.proposals: list[dict] = []
        self.calls: list[tuple[str, dict]] = []
        self.numbers = count(101)
        self.serial = count()
        self.create_fails_with: Exception | None = None

    def __call__(self, verb: str, payload: dict) -> dict:
        self.calls.append((verb, dict(payload)))
        return getattr(self, verb.replace("-", "_"))(payload)

    def payloads(self, verb: str) -> list[dict]:
        return [payload for seen, payload in self.calls if seen == verb]

    # -- repository verbs ------------------------------------------------

    def _serving_copy(self) -> Path:
        work = self.scratch / f"serve-{next(self.serial)}"
        git(self.scratch, "clone", "--quiet", str(self.origin), work.name)
        return work

    def clone(self, payload):
        branch = payload.get("branch") or "main"
        work = self._serving_copy()
        git(work, "checkout", "--quiet", "-B", branch, f"origin/{branch}")
        bundle = work.parent / f"{work.name}.bundle"
        git(work, "bundle", "create", str(bundle), "HEAD", branch)
        blob = bundle.read_bytes()
        return {
            "forge": "local",
            "repo": payload["repository"],
            "branch": branch,
            "revision": git(work, "rev-parse", "HEAD").stdout.strip(),
            "size": len(blob),
            "bundleBase64": base64.b64encode(blob).decode("ascii"),
        }

    def publish(self, payload):
        branch = payload["branch"]
        work = self._serving_copy()
        bundle = work.parent / f"{work.name}.in.bundle"
        bundle.write_bytes(base64.b64decode(payload["bundleBase64"]))
        git(work, "fetch", "--quiet", str(bundle), f"refs/heads/{branch}:refs/heads/{branch}")
        git(work, "push", "--quiet", "origin", f"refs/heads/{branch}:refs/heads/{branch}")
        tip = git(work, "rev-parse", f"refs/heads/{branch}").stdout.strip()
        return {"forge": "local", "repo": payload["repository"], "branch": branch, "revision": tip}

    # -- collaboration verbs ---------------------------------------------

    def proposal_list(self, payload):
        found = [
            proposal
            for proposal in self.proposals
            if payload.get("source") in (None, proposal["source"])
            and payload.get("state", "open") in ("all", proposal["state"])
            and payload.get("target") in (None, proposal["target"])
        ]
        return {"proposals": found, "count": len(found), "truncated": False}

    def proposal_create(self, payload):
        if self.create_fails_with:
            raise self.create_fails_with
        number = next(self.numbers)
        proposal = {
            "number": number,
            "title": payload["title"],
            "body": payload.get("body", ""),
            "state": "open",
            "draft": False,
            "author": "kube-agents",
            "source": payload["source"],
            "target": payload["target"],
            "url": f"https://forge.test/acme/infra/pull/{number}",
            "created": "2026-09-15T00:00:00Z",
            "updated": "2026-09-15T00:00:00Z",
        }
        self.proposals.append(proposal)
        return {"proposal": proposal}

    def proposal_update(self, payload):
        for proposal in self.proposals:
            if proposal["number"] == payload["number"]:
                for field in ("title", "body"):
                    if payload.get(field) is not None:
                        proposal[field] = payload[field]
                return {"proposal": proposal}
        raise AssertionError(f"no proposal {payload['number']}")


@unittest.skipIf(shutil.which("git") is None, "git is not on PATH")
class SubmitSuggestionTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)

        self.origin = base / "origin"
        self.origin.mkdir()
        git(self.origin, "init", "--quiet", "--initial-branch=main")
        (self.origin / "app.yaml").write_text("replicas: 1\n")
        git(self.origin, "add", "-A")
        git(self.origin, "commit", "--quiet", "-m", "seed")

        self.served = base / "served"
        self.served.mkdir()
        self.broker = FakeBroker(self.origin, self.served)

        root = base / "vcs"
        for attribute, value in (
            ("ROOT", root),
            ("SESSIONS", root / ".sessions"),
            ("LOCAL_GIT", REAL_GIT),
            ("call", self.broker),
        ):
            patch = mock.patch.object(vcs_client, attribute, value)
            patch.start()
            self.addCleanup(patch.stop)

        self.scratch = base / "scratch"
        self.scratch.mkdir()
        patch = mock.patch.object(submit_suggestion, "SCRATCH_DIR", str(self.scratch))
        patch.start()
        self.addCleanup(patch.stop)

        self.logged: list[str] = []
        patch = mock.patch.object(submit_suggestion, "log", self.logged.append)
        patch.start()
        self.addCleanup(patch.stop)

        for name, value in (
            ("resolve_repo", lambda workspace=None: "acme/infra"),
            ("get_managed_github_repos", lambda: []),
        ):
            patch = mock.patch.object(gitops_workspace, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    # -- helpers ----------------------------------------------------------

    def run_subject(self, *argv) -> tuple[int, str]:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = submit_suggestion.dispatch(list(argv))
        return code, buffer.getvalue().strip()

    def prepare(self, branch="platform-agent/scale-web", **extra) -> dict:
        argv = ["prepare", "--branch", branch]
        for flag, value in extra.items():
            argv += [f"--{flag.replace('_', '-')}"] + ([] if value is True else [str(value)])
        _, out = self.run_subject(*argv)
        return json.loads(out)

    def edit(self, prepared: dict, text: str = "replicas: 3\n") -> None:
        (Path(prepared["workspace"]) / "app.yaml").write_text(text)

    def body_file(self, text: str = "why this change\n") -> str:
        path = self.scratch / "body.md"
        path.write_text(text)
        return str(path)

    def remote_branches(self) -> list[str]:
        listing = git(self.origin, "branch", "--format=%(refname:short)")
        return listing.stdout.split()

    def existing_proposal(self, branch: str, target: str = "main", **fields) -> dict:
        return self.broker.proposal_create(
            {"title": "under review", "body": "somebody else wrote this", "source": branch, "target": target, **fields}
        )["proposal"]

    # -- prepare ----------------------------------------------------------

    def test_prepare_cuts_a_new_branch_from_the_base(self):
        prepared = self.prepare()
        self.assertEqual(prepared["repo"], "acme/infra")
        self.assertEqual(prepared["branch"], "platform-agent/scale-web")
        self.assertEqual(prepared["base"], "main")
        self.assertEqual(prepared["started_from"], "main")
        self.assertEqual(prepared["proposal"], "")
        copy = Path(prepared["workspace"])
        self.assertTrue((copy / "app.yaml").is_file())
        self.assertEqual(
            git(copy, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip(),
            "platform-agent/scale-web",
        )

    def test_prepare_takes_a_copy_of_a_branch_that_already_has_a_proposal(self):
        # Step 5 of the SKILL: another round on a proposal under review. The
        # branch's own revisions have to come down with it -- cutting it afresh
        # from the base is what replaced every reviewed revision.
        branch = "platform-agent/scale-web"
        self.edit(self.prepare(branch))
        self.run_subject("submit", "--branch", branch, "--title", "first round", "--body", "b")
        reviewed = git(self.origin, "rev-parse", f"refs/heads/{branch}").stdout.strip()

        again = self.prepare(branch, force=True)
        self.assertEqual(again["started_from"], branch)
        self.assertEqual(again["base"], "main")
        self.assertTrue(again["proposal"].endswith("/101"))
        self.assertEqual(
            git(Path(again["workspace"]), "rev-parse", "HEAD").stdout.strip(), reviewed
        )

    def test_prepare_reads_the_base_off_the_open_proposal_not_the_default_branch(self):
        git(self.origin, "checkout", "--quiet", "-b", "release")
        git(self.origin, "checkout", "--quiet", "main")
        branch = "platform-agent/scale-web"
        self.existing_proposal(branch, target="release")
        git(self.origin, "branch", branch, "main")
        self.assertEqual(self.prepare(branch)["base"], "release")

    def test_prepare_refuses_a_protected_branch(self):
        for branch in ("main", "MASTER", "refs/heads/production"):
            with self.assertRaises(ValueError) as caught:
                self.run_subject("prepare", "--branch", branch)
            self.assertIn("CRITICAL SECURITY REFUSAL", str(caught.exception))
        self.assertEqual(self.broker.calls, [])

    def test_prepare_refuses_a_repository_outside_the_managed_list(self):
        with mock.patch.object(
            gitops_workspace, "get_managed_github_repos", lambda: ["acme/infra"]
        ):
            with self.assertRaises(ValueError) as caught:
                self.run_subject("prepare", "--branch", "b", "--repo", "other/elsewhere")
        self.assertIn("not in the managed repositories list", str(caught.exception))
        self.assertEqual(self.broker.calls, [])

    def test_prepare_honours_repo_over_the_resolved_default(self):
        self.prepare(repo="acme/other")
        self.assertEqual(self.broker.payloads("clone")[0]["repository"], "acme/other")

    def test_prepare_refuses_to_replace_a_copy_holding_unpublished_work(self):
        branch = "platform-agent/scale-web"
        prepared = self.prepare(branch)
        self.edit(prepared)
        vcs_client.commit("not yet published", ["app.yaml"], spec="acme/infra")
        with self.assertRaises(vcs_client.VcsError) as caught:
            self.run_subject("prepare", "--branch", branch)
        self.assertIn("unpublished revision", str(caught.exception))
        # And `--force` is the way past it, named in the refusal itself.
        self.assertIn("--force", str(caught.exception))
        self.prepare(branch, force=True)

    def test_two_cards_on_one_repository_get_two_working_copies(self):
        # The scratch root is shared by every card in this container. Keyed on
        # the repository alone, the second card's `prepare` either refused or,
        # with `--force`, deleted the first card's unpublished work.
        first = self.prepare("platform-agent/scale-web")
        self.edit(first, "replicas: 3\n")
        vcs_client.commit("first card", ["app.yaml"], spec="acme/infra")

        second = self.prepare("platform-agent/other")
        self.assertNotEqual(second["workspace"], first["workspace"])
        self.edit(second, "replicas: 9\n")

        # Neither card can see the other's work, and the first one's revision
        # is still there to publish.
        self.assertEqual((Path(first["workspace"]) / "app.yaml").read_text(), "replicas: 3\n")
        self.assertEqual(
            git(Path(first["workspace"]), "log", "-1", "--format=%s").stdout.strip(),
            "first card",
        )
        self.assertEqual(
            git(Path(second["workspace"]), "rev-parse", "--abbrev-ref", "HEAD").stdout.strip(),
            "platform-agent/other",
        )

    def test_submit_sends_the_copy_keyed_on_the_branch_it_names(self):
        # Two copies of one repository, and `--repo` names both. The branch is
        # what tells them apart: submitting the second card's change must not
        # publish the first card's.
        first = self.prepare("platform-agent/scale-web")
        self.edit(first, "replicas: 3\n")
        second = self.prepare("platform-agent/other")
        self.edit(second, "replicas: 9\n")

        self.run_subject(
            "submit", "--branch", "platform-agent/other",
            "--repo", "acme/infra", "--title", "t", "--body", "b",
        )
        self.assertIn("platform-agent/other", self.remote_branches())
        self.assertNotIn("platform-agent/scale-web", self.remote_branches())

    # -- submit -----------------------------------------------------------

    def test_submit_publishes_the_branch_and_opens_the_proposal(self):
        prepared = self.prepare()
        self.edit(prepared)
        code, out = self.run_subject(
            "submit",
            "--branch", "platform-agent/scale-web",
            "--title", "fix(capacity): raise the replica floor",
            "--body-file", self.body_file(),
        )
        self.assertEqual(code, 0)
        self.assertEqual(out, "https://forge.test/acme/infra/pull/101")
        self.assertIn("platform-agent/scale-web", self.remote_branches())
        created = self.broker.payloads("proposal-create")[0]
        self.assertEqual(created["source"], "platform-agent/scale-web")
        self.assertEqual(created["target"], "main")
        self.assertEqual(created["body"], "why this change\n")
        # The commit is the change, not the whole tree of the container.
        published = self.broker.payloads("publish")[0]
        self.assertEqual(published["target"], "main")
        self.assertIs(published["advance"], False)

    def test_a_second_round_updates_the_proposal_instead_of_failing_to_open_one(self):
        prepared = self.prepare()
        self.edit(prepared)
        self.run_subject("submit", "--branch", "platform-agent/scale-web", "--title", "first", "--body", "one")
        again = self.prepare("platform-agent/scale-web", force=True)
        self.edit(again, "replicas: 5\n")
        _, out = self.run_subject(
            "submit", "--branch", "platform-agent/scale-web", "--title", "second", "--body", "two"
        )
        self.assertEqual(out, "https://forge.test/acme/infra/pull/101")
        self.assertEqual(len(self.broker.payloads("proposal-create")), 1)
        self.assertEqual(self.broker.proposals[0]["title"], "second")
        self.assertEqual(self.broker.proposals[0]["body"], "two")
        # The branch was extended rather than replaced: both revisions are on it.
        log = git(self.origin, "log", "--format=%s", "refs/heads/platform-agent/scale-web")
        self.assertEqual(log.stdout.split("\n")[:2], ["second", "first"])

    def test_the_second_round_tells_the_broker_it_means_to_extend_the_branch(self):
        prepared = self.prepare()
        self.edit(prepared)
        self.run_subject("submit", "--branch", "platform-agent/scale-web", "--title", "first", "--body", "one")
        again = self.prepare("platform-agent/scale-web", force=True)
        self.edit(again, "replicas: 5\n")
        self.run_subject("submit", "--branch", "platform-agent/scale-web", "--title", "second", "--body", "two")
        self.assertTrue(self.broker.payloads("publish")[1]["advance"])
        self.assertEqual(self.broker.payloads("publish")[1]["clonedFrom"], "platform-agent/scale-web")

    def test_keep_description_leaves_the_body_alone_and_still_publishes(self):
        branch = "platform-agent/scale-web"
        prepared = self.prepare()
        self.edit(prepared)
        self.run_subject("submit", "--branch", branch, "--title", "first", "--body", "one")
        again = self.prepare(branch, force=True)
        self.edit(again, "replicas: 7\n")
        _, out = self.run_subject(
            "submit", "--branch", branch, "--title", "a merge commit", "--keep-description"
        )
        self.assertEqual(out, "https://forge.test/acme/infra/pull/101")
        self.assertEqual(self.broker.proposals[0]["title"], "first")
        self.assertEqual(self.broker.proposals[0]["body"], "one")
        self.assertEqual(self.broker.payloads("proposal-update"), [])
        self.assertEqual(len(self.broker.payloads("publish")), 2)
        # Both halves of what the notice now claims: the title did not reach
        # the proposal, and it is still what the pending edit was recorded
        # under. The earlier wording said "ignored", which was false here.
        self.assertTrue(
            any("does not reach the proposal" in line for line in self.logged)
        )
        self.assertTrue(
            any("commit message" in line for line in self.logged)
        )

    def test_keep_description_with_no_open_proposal_refuses_before_publishing(self):
        prepared = self.prepare()
        self.edit(prepared)
        with self.assertRaises(RuntimeError) as caught:
            self.run_subject(
                "submit", "--branch", "platform-agent/scale-web", "--keep-description"
            )
        self.assertIn("no proposal is open", str(caught.exception))
        self.assertEqual(self.broker.payloads("publish"), [])
        self.assertNotIn("platform-agent/scale-web", self.remote_branches())

    def test_submit_refuses_a_copy_standing_on_another_branch(self):
        prepared = self.prepare()
        self.edit(prepared)
        with self.assertRaises(ValueError) as caught:
            self.run_subject("submit", "--branch", "platform-agent/something-else", "--title", "t", "--body", "b")
        self.assertIn("is on branch 'platform-agent/scale-web'", str(caught.exception))
        self.assertEqual(self.broker.payloads("publish"), [])

    def test_submit_refuses_without_a_title_and_a_body(self):
        self.prepare()
        for argv in (
            ("submit", "--branch", "b", "--title", "t"),
            ("submit", "--branch", "b", "--body", "only a body"),
        ):
            with self.assertRaises(ValueError) as caught:
                self.run_subject(*argv)
            self.assertIn("--title and one of --body / --body-file", str(caught.exception))

    def test_submit_refuses_a_protected_branch(self):
        with self.assertRaises(ValueError) as caught:
            self.run_subject("submit", "--branch", "main", "--title", "t", "--body", "b")
        self.assertIn("CRITICAL SECURITY REFUSAL", str(caught.exception))

    def test_submit_commits_the_tracked_changes_the_copy_holds_under_the_title(self):
        prepared = self.prepare()
        self.edit(prepared)
        self.run_subject(
            "submit", "--branch", "platform-agent/scale-web", "--title", "one file", "--body", "b"
        )
        listing = git(self.origin, "show", "--name-only", "--format=%s", "refs/heads/platform-agent/scale-web")
        self.assertEqual(listing.stdout.split(), ["one", "file", "app.yaml"])

    def test_submit_refuses_rather_than_sweeping_a_file_the_agent_never_staged(self):
        """The SKILL forbids `git add .`; a helper doing it for them forbids nothing.

        The copy is a real clone on a filesystem the agent also scratches in, so
        the untracked file here is as likely to be a debug dump as a manifest.
        Refusing names it and says what to do; the alternatives are shipping it
        in a public proposal or dropping a real change without saying so.
        """
        prepared = self.prepare()
        self.edit(prepared)
        (Path(prepared["workspace"]) / "scratch.log").write_text("debug\n")
        with self.assertRaises(vcs_client.VcsError) as caught:
            self.run_subject(
                "submit", "--branch", "platform-agent/scale-web", "--title", "t", "--body", "b"
            )
        self.assertIn("scratch.log", str(caught.exception))
        self.assertEqual(self.broker.payloads("publish"), [])

    def test_submit_records_a_new_file_the_agent_staged_itself(self):
        """Staging is the agent saying this one belongs, which is the whole gate."""
        prepared = self.prepare()
        self.edit(prepared)
        (Path(prepared["workspace"]) / "new.yaml").write_text("added\n")
        vcs_client.local(
            vcs_client.resolve_session("acme/infra"), ["add", "--", "new.yaml"], "add"
        )
        self.run_subject(
            "submit", "--branch", "platform-agent/scale-web", "--title", "two files", "--body", "b"
        )
        listing = git(self.origin, "show", "--name-only", "--format=%s", "refs/heads/platform-agent/scale-web")
        self.assertEqual(listing.stdout.split(), ["two", "files", "app.yaml", "new.yaml"])

    def test_submit_takes_a_copy_the_agent_committed_itself(self):
        # The SKILL has always let the agent commit; nothing here insists on
        # making the revision, only that there is one.
        prepared = self.prepare()
        self.edit(prepared)
        vcs_client.commit("the agent's own message", spec="acme/infra")
        self.run_subject(
            "submit", "--branch", "platform-agent/scale-web", "--title", "t", "--body", "b"
        )
        subject = git(self.origin, "log", "--format=%s", "-1", "refs/heads/platform-agent/scale-web")
        self.assertEqual(subject.stdout.strip(), "the agent's own message")

    def test_a_retry_after_the_create_failed_opens_the_proposal_it_never_got(self):
        """Publish landed, `proposal-create` did not: the retry has to reach it.

        Without this the second `submit` finds nothing new to send and is
        refused before the step that failed, and `prepare` is no way out either
        -- it cuts the branch afresh and the broker refuses the publish as
        `BRANCH_DIVERGED`. The `git push --force-with-lease` + `gh pr create`
        pair this replaced was idempotent on retry.
        """
        prepared = self.prepare()
        self.edit(prepared)
        self.broker.create_fails_with = vcs_client.VcsError(
            "secondary rate limit", code="FORGE_RATE_LIMITED"
        )
        with self.assertRaises(vcs_client.VcsError):
            self.run_subject(
                "submit", "--branch", "platform-agent/scale-web", "--title", "t", "--body", "b"
            )
        published = git(self.origin, "rev-parse", "refs/heads/platform-agent/scale-web")
        self.assertTrue(published.stdout.strip())

        self.broker.create_fails_with = None
        _, url = self.run_subject(
            "submit", "--branch", "platform-agent/scale-web", "--title", "t", "--body", "b"
        )
        self.assertEqual(url, self.broker.proposals[0]["url"])
        # And it did not publish a second time: there was nothing new to send.
        self.assertEqual(len(self.broker.payloads("publish")), 1)

    def test_a_proposal_opened_by_a_racing_run_is_updated_not_reported_as_failure(self):
        prepared = self.prepare()
        self.edit(prepared)
        raced = self.existing_proposal("platform-agent/scale-web")
        # The lookup before the publish is what would normally find it; this is
        # the window after that read, so the fake refuses the create the way a
        # forge does and the script has to ask again.
        self.broker.create_fails_with = vcs_client.VcsError(
            "a pull request for branch already exists", code="FORGE_REJECTED"
        )
        with mock.patch.object(submit_suggestion, "open_proposal", side_effect=[None, raced]):
            _, out = self.run_subject(
                "submit", "--branch", "platform-agent/scale-web", "--title", "t", "--body", "b"
            )
        self.assertEqual(out, raced["url"])
        self.assertEqual(self.broker.proposals[0]["title"], "t")

    def test_a_create_that_fails_for_another_reason_is_not_swallowed(self):
        prepared = self.prepare()
        self.edit(prepared)
        self.broker.create_fails_with = vcs_client.VcsError("base is protected", code="FORGE_REJECTED")
        with self.assertRaises(vcs_client.VcsError) as caught:
            self.run_subject("submit", "--branch", "platform-agent/scale-web", "--title", "t", "--body", "b")
        self.assertEqual(caught.exception.code, "FORGE_REJECTED")

    def test_base_names_what_the_change_merges_into(self):
        git(self.origin, "branch", "release", "main")
        prepared = self.prepare()
        self.edit(prepared)
        self.run_subject(
            "submit", "--branch", "platform-agent/scale-web", "--title", "t", "--body", "b",
            "--base", "release",
        )
        self.assertEqual(self.broker.payloads("proposal-create")[0]["target"], "release")
        self.assertEqual(self.broker.payloads("publish")[0]["target"], "release")

    # -- the description file ---------------------------------------------

    def test_a_body_file_outside_scratch_is_refused(self):
        self.prepare()
        outside = Path(self.tmp.name) / "elsewhere.md"
        outside.write_text("secrets\n")
        with self.assertRaises(ValueError) as caught:
            self.run_subject("submit", "--branch", "b", "--title", "t", "--body-file", str(outside))
        self.assertIn("resolves outside", str(caught.exception))

    def test_a_body_file_symlinked_out_of_scratch_is_refused(self):
        self.prepare()
        outside = Path(self.tmp.name) / "elsewhere.md"
        outside.write_text("secrets\n")
        link = self.scratch / "body.md"
        link.symlink_to(outside)
        with self.assertRaises(ValueError) as caught:
            self.run_subject("submit", "--branch", "b", "--title", "t", "--body-file", str(link))
        self.assertIn("resolves outside", str(caught.exception))

    def test_an_empty_body_file_is_refused(self):
        self.prepare()
        empty = self.scratch / "body.md"
        empty.write_text("   \n")
        with self.assertRaises(ValueError) as caught:
            self.run_subject("submit", "--branch", "b", "--title", "t", "--body-file", str(empty))
        self.assertIn("is empty", str(caught.exception))

    # -- the call shapes that outlive a roll -------------------------------

    def test_a_retired_flag_is_ignored_with_a_line_saying_so(self):
        prepared = self.prepare()
        self.edit(prepared)
        self.run_subject(
            "submit", "--branch", "platform-agent/scale-web", "--title", "t", "--body", "b",
            "--workspace", "/opt/data/gitops/t_9f3c/acme__infra",
            "--lease", "t_9f3c",
        )
        self.assertTrue(any("--workspace is no longer read" in line for line in self.logged))
        self.assertTrue(any("--lease is no longer read" in line for line in self.logged))

    def test_an_argv_with_no_subcommand_is_read_as_submit(self):
        self.assertEqual(
            submit_suggestion.normalise_argv(["--branch", "b", "--title", "t"]),
            ["submit", "--branch", "b", "--title", "t"],
        )
        for argv in ([], ["prepare", "--branch", "b"], ["-h"]):
            self.assertEqual(submit_suggestion.normalise_argv(argv), argv)

    def test_the_two_commands_are_the_whole_surface(self):
        # `list` and `fetch` were the read half of a mode where the agent had
        # no checkout. It has one again.
        self.assertEqual(submit_suggestion.COMMANDS, ("prepare", "submit"))
        with self.assertRaises(SystemExit):
            with mock.patch("sys.stderr", io.StringIO()):
                submit_suggestion.build_parser().parse_args(["list", "--handle", "x"])


if __name__ == "__main__":
    unittest.main()
