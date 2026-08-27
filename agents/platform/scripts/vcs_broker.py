#!/usr/bin/env python3
"""Version control as concepts: the `/v1/vcs/*` broker routes.

The credential is here and the working copy is not. A caller in the sandbox
names a repository by URL and asks for the things version control is for; every
one of them is answered by this process, which holds the token, on behalf of a
process that does not.

Three properties are the whole design.

*The forge is decided here.* Which forge a URL belongs to and which credential
opens it are the same question, and the answer belongs beside the credential. A
sandbox that had to know GitHub from GitLab would be a second place that has to
agree, and adding a forge would mean shipping two images instead of one. So the
caller sends a URL, `resolve_forge` maps its host through a configured table,
and a host with no entry is refused by name rather than attempted with whatever
credential happens to be loaded. That table is also the security boundary: a
caller-chosen URL decides where a minted token gets sent, and an allowlist is
what stops "clone this repository" from meaning "post my credential there".

*Nothing crosses this seam in a forge's own vocabulary.* `gh` runs in this
process because this is where the GitHub credential lives, and its JSON stops
here. What goes back is a normalised proposal, issue or comment — the concepts
every forge has under a different name. A caller that received GitHub's
`head.ref`, `author_association` and `merged_at` would be a GitHub client
wearing a neutral URL, and the second forge would be a second client rather than
a second class in this file.

*History moves as bundles, in both directions, and is never checked out here.*
`clone` clones, bundles, and deletes the tree before it answers. `publish` takes
a bundle of the caller's new revisions, fetches it into a scratch repository,
checks that it says what it claims to say, and pushes the branch — without ever
running a `checkout`. That last part is what makes accepting caller-supplied
objects safe: a `.gitattributes` naming a filter driver, a `.gitmodules`, a file
called `.gitconfig` are all inert as long as nothing materialises them into a
working copy beside the token. Objects and refs are data; a checkout is what
turns them into behaviour.

The routes are stateless. There is no handle, nothing survives a request, and
two concurrent requests share nothing but the lock the HTTP layer holds.

On the vocabulary
-----------------
The verb names are the version-control concepts rather than one system's
spelling of them, because the caller is a language model and the concepts are
what it was trained on. Where the systems disagree the neutral name wins and the
familiar one is an alias: `annotate` over `blame`, which is what Mercurial,
Subversion, Bazaar and jj all call per-line attribution and which git itself
accepts; `publish` over `push`, because sending revisions to the shared
repository is the concept and `push` is the DVCS spelling that invites `--force`
and an `origin` this design does not have. On the collaboration side the neutral
noun is `proposal`, after Launchpad's "merge proposal" — the term `breezy` and
`silver-platter` settled on for exactly this problem — with `pr` and `mr` as
aliases, since "pull request" carries GitHub's fork-and-branch assumption and
Gerrit's unit of review is a single revision.
`docs/designs/version-control-abstraction.md` §The vocabulary, and where it
comes from records the sources.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable

from workspace_paths import WorkspaceError

LOGGER = logging.getLogger("credential-proxy.vcs")

# The same shape of ceiling `content_workspace` applies, for the same reason:
# the broker's scratch volume is an emptyDir sized for manifests, and a
# repository that does not fit should say so rather than fill the disk out from
# under everything else.
DEFAULT_MAX_CLONE_BYTES = 256 << 20  # 256 MiB
DEFAULT_MAX_BUNDLE_BYTES = 64 << 20  # 64 MiB

# How many items a listing returns. One page, deliberately: paginating walks
# every page of an issue tracker, which is minutes of API calls and a response
# no caller reads to the end. A truncated listing says that it is truncated.
DEFAULT_PAGE_SIZE = 30
MAX_PAGE_SIZE = 100

# A branch name git will accept and that cannot be read as an option or as
# revision syntax. Deliberately narrower than `git check-ref-format`: every name
# this has to carry is one a person typed.
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# The ref an incoming bundle is fetched into. Under `refs/vcs/` rather than
# `refs/heads/` so nothing here can be confused with a branch, and so a publish
# of a leftover ref cannot happen by naming a plausible branch.
_INCOMING = "refs/vcs/incoming"


class ForgeUnsupported(WorkspaceError):
    """This host is not one this install has a credential and a client for."""

    def __init__(self, message: str) -> None:
        super().__init__(message, status=501, code="FORGE_UNSUPPORTED")


def vcs_enabled() -> bool:
    """Off by default, like every other route that spends a credential."""
    return os.getenv("CREDENTIAL_PROXY_VCS", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        LOGGER.warning("%s=%r is not an integer; using %d", name, raw, default)
        return default
    if value <= 0:
        LOGGER.warning("%s=%r is not positive; using %d", name, raw, default)
        return default
    return value


# ---- validation -----------------------------------------------------------


def validate_branch(value: Any, field: str = "branch") -> str:
    if not isinstance(value, str) or not _BRANCH_RE.match(value.strip()):
        raise WorkspaceError(f"{field} is not an acceptable branch name")
    value = value.strip()
    if (
        value.startswith("-")
        or ".." in value
        or "@{" in value
        or value.endswith(".lock")
    ):
        raise WorkspaceError(f"{field} is not an acceptable branch name")
    return value


def validate_revision(value: Any, field: str = "baseRevision") -> str:
    if not isinstance(value, str) or not _SHA_RE.match(value.strip()):
        raise WorkspaceError(f"{field} must be a full 40-character revision id")
    return value.strip()


def validate_text(value: Any, field: str, required: bool = True) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise WorkspaceError(f"{field} must be a string")
    if required and not value.strip():
        raise WorkspaceError(f"{field} must not be empty")
    return value


def validate_number(value: Any, field: str = "number") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise WorkspaceError(f"{field} must be a positive item number")
    return value


def validate_limit(value: Any) -> int:
    if value is None:
        return DEFAULT_PAGE_SIZE
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise WorkspaceError("limit must be a positive number of items")
    return min(value, MAX_PAGE_SIZE)


def validate_state(value: Any) -> str:
    state = value.strip().lower() if isinstance(value, str) and value else "open"
    if state not in {"open", "closed", "all"}:
        raise WorkspaceError("state must be one of open, closed, all")
    return state


def validate_labels(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(
        isinstance(label, str) and label.strip() for label in value
    ):
        raise WorkspaceError("labels must be a list of non-empty strings")
    return [label.strip() for label in value]


# ---- forges ---------------------------------------------------------------


class Forge:
    """What differs between hosts, which is less than it looks.

    Cloning, bundling and pushing are the same everywhere and live in the
    broker. A forge decides four things: whether a URL is one of its
    repositories, what URL to clone (which is where a credential is applied),
    how to reach its collaboration API, and how to translate that API's objects
    into the concepts every forge shares.
    """

    name = "abstract"
    hosts: tuple[str, ...] = ()
    # What this forge calls a change proposal, for messages the caller reads.
    proposal_noun = "change proposal"

    def parse(self, url: str) -> str:
        """The repository this URL names, in whatever form `clone_url` wants."""
        raise NotImplementedError

    def clone_url(self, repo: str) -> str:
        raise NotImplementedError

    def capabilities(self, repo: str) -> dict[str, Any]:
        raise NotImplementedError

    def mint(self, refresh: Callable[[str], None] | None, repo: str) -> None:
        """Make this forge's credential current, if it has one that expires.

        A forge decides this because the answer is forge-shaped: GitHub's is an
        App installation token good for under an hour, and a forge configured
        with a long-lived personal token has nothing to do here. The default is
        nothing.
        """

    # The collaboration verbs. A forge that cannot serve one raises
    # ForgeUnsupported naming what is missing, which is an answer.
    def proposal_create(self, api, repo, payload) -> dict[str, Any]:
        raise NotImplementedError

    def proposal_list(self, api, repo, payload) -> dict[str, Any]:
        raise NotImplementedError

    def proposal_view(self, api, repo, payload) -> dict[str, Any]:
        raise NotImplementedError

    def proposal_comment(self, api, repo, payload) -> dict[str, Any]:
        raise NotImplementedError

    def issue_list(self, api, repo, payload) -> dict[str, Any]:
        raise NotImplementedError

    def issue_view(self, api, repo, payload) -> dict[str, Any]:
        raise NotImplementedError

    def issue_create(self, api, repo, payload) -> dict[str, Any]:
        raise NotImplementedError

    def issue_comment(self, api, repo, payload) -> dict[str, Any]:
        raise NotImplementedError


def _listing(items: list[dict], limit: int, key: str) -> dict[str, Any]:
    """A page of results that says when it is a page rather than the answer."""
    return {key: items, "count": len(items), "truncated": len(items) >= limit}


class GitHubForge(Forge):
    """GitHub, reached through `gh api` and translated on the way out.

    Only `gh api` is used, never `gh pr` or `gh issue`. Those subcommands infer
    the repository from a `.git/config`, which is the one file this whole design
    exists to keep out of the credentialed process, and they format for a human.
    `gh api` takes an explicit path and returns the API's own JSON, so this class
    is a REST client that borrows `gh` for authentication — and the day `gh`
    leaves the broker image, `VcsBroker._api` is the only thing that changes.
    """

    name = "github"
    hosts = ("github.com", "www.github.com")
    proposal_noun = "pull request"

    def parse(self, url: str) -> str:
        text = _strip_scheme(url).removesuffix(".git")
        text = text.replace("github.com:", "github.com/")
        parts = [part for part in text.split("/") if part]
        if parts and parts[0].lower().removeprefix("www.") == "github.com":
            parts = parts[1:]
        if len(parts) != 2 or not all(_SEGMENT_RE.match(part) for part in parts):
            raise WorkspaceError(
                f"{url!r} is not a GitHub repository; expected owner/name"
            )
        return "/".join(parts)

    def clone_url(self, repo: str) -> str:
        # Composed here from two validated path segments, never taken from the
        # caller. The caller's URL decided *which forge*; it does not get to
        # decide the host the credential is presented to.
        return f"https://github.com/{repo}.git"

    def capabilities(self, repo: str) -> dict[str, Any]:
        return {
            "forge": self.name,
            "repo": repo,
            "proposalNoun": self.proposal_noun,
            "verbs": sorted(_GITHUB_VERBS),
            "missing": [],
        }

    def mint(self, refresh: Callable[[str], None] | None, repo: str) -> None:
        """Refresh before spending, rather than inferring expiry from a failure.

        The credential here is a GitHub App installation token that expires
        within the hour. An expired one surfaces as `Authentication failed` from
        inside the broker's own clone, which reaches the caller as a clone
        failure and reads like the repository is gone. Minting is idempotent and
        costs one local process, so it happens before every credentialed verb;
        the alternative is that the first verb after an idle hour fails once,
        for a reason the caller cannot act on.

        A failure here is logged and not raised. The broker may already hold a
        valid token, in which case the verb about to run succeeds and a refusal
        would have been the only thing that failed.
        """
        if refresh is None:
            return
        try:
            refresh(repo)
        except Exception as exc:  # noqa: BLE001 - the verb's own error is better
            LOGGER.warning(
                "github: credential refresh for %s failed: %s",
                repo,
                type(exc).__name__,
            )

    # -- translation --------------------------------------------------------

    @staticmethod
    def _actor(node: dict[str, Any] | None) -> str:
        # `[bot]` comes off here rather than at the caller. GitHub's REST and
        # GraphQL APIs disagree about whether an App login carries the suffix,
        # and `forge.py` records what comparing an unnormalised one costs: an
        # agent that answers its own comments forever.
        login = ((node or {}).get("login") or "").strip()
        return login.removesuffix("[bot]")

    @classmethod
    def _proposal(cls, node: dict[str, Any]) -> dict[str, Any]:
        # Three states, not GitHub's two plus a timestamp. Closed and merged are
        # different outcomes on every forge, and a caller should not have to
        # know that GitHub encodes the difference in a nullable date field.
        if node.get("merged_at"):
            state = "merged"
        else:
            state = "open" if node.get("state") == "open" else "closed"
        return {
            "number": node.get("number"),
            "title": node.get("title") or "",
            "state": state,
            "draft": bool(node.get("draft")),
            "author": cls._actor(node.get("user")),
            "source": ((node.get("head") or {}).get("ref")) or "",
            "target": ((node.get("base") or {}).get("ref")) or "",
            "url": node.get("html_url") or "",
            "created": node.get("created_at") or "",
            "updated": node.get("updated_at") or "",
            "body": node.get("body") or "",
        }

    @classmethod
    def _issue(cls, node: dict[str, Any]) -> dict[str, Any]:
        return {
            "number": node.get("number"),
            "title": node.get("title") or "",
            "state": node.get("state") or "",
            "author": cls._actor(node.get("user")),
            "labels": [
                label.get("name", "")
                for label in (node.get("labels") or [])
                if isinstance(label, dict)
            ],
            "assignees": [
                cls._actor(person) for person in (node.get("assignees") or [])
            ],
            "url": node.get("html_url") or "",
            "created": node.get("created_at") or "",
            "updated": node.get("updated_at") or "",
            "body": node.get("body") or "",
        }

    @classmethod
    def _comment(cls, node: dict[str, Any]) -> dict[str, Any]:
        return {
            "author": cls._actor(node.get("user")),
            "created": node.get("created_at") or "",
            "body": node.get("body") or "",
            "url": node.get("html_url") or "",
        }

    def _comments(self, api, repo, number, payload) -> list[dict[str, Any]]:
        # The issue-comments endpoint, and for a proposal too: on GitHub that is
        # the conversation, while `pulls/{n}/comments` is line notes on the
        # diff. A caller asking to read the discussion means the former.
        limit = validate_limit(payload.get("limit"))
        nodes = api(
            "GET", f"repos/{repo}/issues/{number}/comments?per_page={limit}", []
        )
        return [self._comment(node) for node in nodes]

    # -- proposals ----------------------------------------------------------

    def proposal_create(self, api, repo, payload) -> dict[str, Any]:
        source = validate_branch(payload.get("source"), "source")
        target = validate_branch(payload.get("target"), "target")
        title = validate_text(payload.get("title"), "title").strip()
        body = validate_text(payload.get("body"), "body", required=False)
        fields = [
            "-f", f"title={title}",
            "-f", f"body={body}",
            "-f", f"head={source}",
            "-f", f"base={target}",
        ]
        if payload.get("draft"):
            fields += ["-F", "draft=true"]
        node = api("POST", f"repos/{repo}/pulls", fields)
        return {"proposal": self._proposal(node)}

    def proposal_list(self, api, repo, payload) -> dict[str, Any]:
        state = validate_state(payload.get("state"))
        limit = validate_limit(payload.get("limit"))
        nodes = api("GET", f"repos/{repo}/pulls?state={state}&per_page={limit}", [])
        return _listing([self._proposal(node) for node in nodes], limit, "proposals")

    def proposal_view(self, api, repo, payload) -> dict[str, Any]:
        number = validate_number(payload.get("number"))
        node = api("GET", f"repos/{repo}/pulls/{number}", [])
        result: dict[str, Any] = {"proposal": self._proposal(node)}
        if payload.get("comments"):
            result["comments"] = self._comments(api, repo, number, payload)
        if payload.get("diff"):
            result["diff"] = api(
                "GET",
                f"repos/{repo}/pulls/{number}",
                ["-H", "Accept: application/vnd.github.v3.diff"],
                raw=True,
            )
        return result

    def proposal_comment(self, api, repo, payload) -> dict[str, Any]:
        number = validate_number(payload.get("number"))
        body = validate_text(payload.get("body"), "body")
        node = api(
            "POST", f"repos/{repo}/issues/{number}/comments", ["-f", f"body={body}"]
        )
        return {"comment": self._comment(node)}

    # -- issues -------------------------------------------------------------

    def issue_list(self, api, repo, payload) -> dict[str, Any]:
        state = validate_state(payload.get("state"))
        limit = validate_limit(payload.get("limit"))
        path = f"repos/{repo}/issues?state={state}&per_page={limit}"
        labels = validate_labels(payload.get("labels"))
        if labels:
            path += "&labels=" + ",".join(labels)
        nodes = api("GET", path, [])
        # GitHub's issues endpoint returns pull requests too — a PR *is* an
        # issue there. Nowhere else models it that way, and a caller that asked
        # for issues and got proposals mixed in would have to know that. The
        # `pull_request` key is how they are told apart.
        issues = [node for node in nodes if "pull_request" not in node]
        return _listing([self._issue(node) for node in issues], limit, "issues")

    def issue_view(self, api, repo, payload) -> dict[str, Any]:
        number = validate_number(payload.get("number"))
        node = api("GET", f"repos/{repo}/issues/{number}", [])
        if "pull_request" in node:
            raise WorkspaceError(
                f"#{number} is a {self.proposal_noun}, not an issue; "
                "read it with `proposal view`"
            )
        result: dict[str, Any] = {"issue": self._issue(node)}
        if payload.get("comments"):
            result["comments"] = self._comments(api, repo, number, payload)
        return result

    def issue_create(self, api, repo, payload) -> dict[str, Any]:
        title = validate_text(payload.get("title"), "title").strip()
        body = validate_text(payload.get("body"), "body", required=False)
        fields = ["-f", f"title={title}", "-f", f"body={body}"]
        for label in validate_labels(payload.get("labels")):
            fields += ["-f", f"labels[]={label}"]
        node = api("POST", f"repos/{repo}/issues", fields)
        return {"issue": self._issue(node)}

    def issue_comment(self, api, repo, payload) -> dict[str, Any]:
        number = validate_number(payload.get("number"))
        body = validate_text(payload.get("body"), "body")
        node = api(
            "POST", f"repos/{repo}/issues/{number}/comments", ["-f", f"body={body}"]
        )
        return {"comment": self._comment(node)}


class _StubForge(Forge):
    """A host this install recognises and cannot yet serve.

    Present rather than absent on purpose. A caller asking about a GitLab URL
    gets the gap named — no credential minter, no merge-request client — which
    is an answer it can report; falling through to the default forge would
    answer a GitLab question with "not a GitHub repository", which is not.
    """

    def __init__(self, name, hosts, proposal_noun, missing) -> None:
        self.name = name
        self.hosts = hosts
        self.proposal_noun = proposal_noun
        self.missing = missing

    def parse(self, url: str) -> str:
        text = _strip_scheme(url).removesuffix(".git")
        for host in self.hosts:
            text = text.replace(f"{host}:", f"{host}/")
        parts = [part for part in text.split("/") if part]
        if parts and parts[0].lower() in self.hosts:
            parts = parts[1:]
        if len(parts) < 2 or not all(_SEGMENT_RE.match(part) for part in parts):
            raise WorkspaceError(f"{url!r} is not a {self.name} repository")
        return "/".join(parts)

    def clone_url(self, repo: str) -> str:
        raise ForgeUnsupported(f"{self.name}: {self.missing[0]}")

    def capabilities(self, repo: str) -> dict[str, Any]:
        return {
            "forge": self.name,
            "repo": repo,
            "proposalNoun": self.proposal_noun,
            "verbs": [],
            "missing": list(self.missing),
        }

    def _refuse(self, *_args, **_kwargs):
        raise ForgeUnsupported(f"{self.name}: {self.missing[-1]}")

    proposal_create = proposal_list = proposal_view = proposal_comment = _refuse
    issue_create = issue_list = issue_view = issue_comment = _refuse


_GITHUB_VERBS = {
    "capabilities",
    "clone",
    "publish",
    "proposal-create",
    "proposal-list",
    "proposal-view",
    "proposal-comment",
    "issue-list",
    "issue-view",
    "issue-create",
    "issue-comment",
}


FORGES: tuple[Forge, ...] = (
    GitHubForge(),
    _StubForge(
        "gitlab",
        ("gitlab.com",),
        "merge request",
        [
            "no credential minter is configured for gitlab.com; the GitHub App "
            "installation flow has no GitLab equivalent here",
            "merge requests and issues need a GitLab REST client in the broker; "
            "`gh api` cannot reach them",
        ],
    ),
    _StubForge(
        "bitbucket",
        ("bitbucket.org",),
        "pull request",
        [
            "no credential minter is configured for bitbucket.org",
            "Bitbucket Cloud ships no CLI, so its collaboration verbs need a "
            "REST client in the broker rather than an allowlist entry",
        ],
    ),
)

# The allowlist, built from the forges rather than kept beside them. A host that
# is not a key is refused before any credential is minted.
HOSTS: dict[str, Forge] = {host: forge for forge in FORGES for host in forge.hosts}


def _strip_scheme(url: str) -> str:
    text = url.strip()
    for prefix in ("https://", "http://", "ssh://", "git+ssh://", "git@"):
        text = text.removeprefix(prefix)
    return text


def repository_host(url: str) -> str:
    """The host a repository spec names, lowercased, or "" if it names none.

    The scheme comes off before the split rather than after. Splitting first and
    stripping the pieces reads plausibly and matches nothing: the first segment
    of `https://gitlab.com/acme/infra` is `https:`.

    The order of the two splits that follow is the whole of the function. Taking
    the `:` field first reads `oauth2:token@evil.example` as the host `oauth2`,
    which is not a key in the allowlist and so falls through to the bare-name
    default — a URL naming one host resolved as a GitHub repository. Userinfo
    comes off first, then the port.
    """
    text = url.strip()
    remainder = _strip_scheme(text)
    first = remainder.split("/", 1)[0]
    head = first.rsplit("@", 1)[1] if "@" in first else first
    head = head.split(":", 1)[0].lower()
    # Whether that first segment is a host at all. An explicit scheme settles
    # it; otherwise a dot, a colon or a userinfo marker distinguishes
    # `github.com/acme/infra` from the bare `acme/infra` every skill in this
    # repository writes.
    if remainder != text or "." in head or ":" in first or "@" in first:
        return head
    return ""


def resolve_forge(url: Any) -> tuple[Forge, str]:
    """The forge for this URL and the repository it names, or a refusal.

    A bare `owner/name` means GitHub, which is what every skill in this
    repository has always meant by it. Anything with a host must have that host
    in the allowlist; there is no default for a URL, because defaulting is how a
    token reaches a host nobody configured.
    """
    if not isinstance(url, str) or not url.strip():
        raise WorkspaceError("repository must be a URL or owner/name")
    host = repository_host(url)
    forge = HOSTS.get(host) if host else HOSTS.get("github.com")
    if forge is None:
        known = ", ".join(sorted(HOSTS))
        raise ForgeUnsupported(
            f"{host} is not a forge this install serves. Configured: {known}."
        )
    return forge, forge.parse(url)


def _remove_tree(path: Path) -> None:
    for entry in sorted(path.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        try:
            if entry.is_dir() and not entry.is_symlink():
                entry.rmdir()
            else:
                entry.unlink()
        except OSError:
            pass
    try:
        path.rmdir()
    except OSError:
        pass


class VcsBroker:
    """The verbs, each one request long.

    `scratch_root` is on the broker's own volume. Nothing under it outlives a
    request, which is what makes these routes stateless: there is no handle to
    leak, no tree to collide with another caller's, and no cleanup an
    interrupted client can skip.
    """

    def __init__(
        self,
        scratch_root: str | Path,
        git_runner: Callable[..., subprocess.CompletedProcess],
        cli_runner: Callable[..., subprocess.CompletedProcess] | None = None,
        refresh: Callable[[str], None] | None = None,
        timeout_seconds: int = 600,
    ) -> None:
        self.scratch_root = Path(scratch_root)
        self.scratch_root.mkdir(parents=True, exist_ok=True)
        self._git_runner = git_runner
        self._refresh = refresh
        # `gh` needs the broker's credential environment but no repository. When
        # the caller does not separate the two, the git runner serves both.
        self._cli_runner = cli_runner or git_runner
        self.timeout_seconds = timeout_seconds
        self.max_clone_bytes = _positive_int(
            "CREDENTIAL_PROXY_MAX_CLONE_BYTES", DEFAULT_MAX_CLONE_BYTES
        )
        self.max_bundle_bytes = _positive_int(
            "CREDENTIAL_PROXY_MAX_BUNDLE_BYTES", DEFAULT_MAX_BUNDLE_BYTES
        )
        self._sequence = 0

    # ---- plumbing ------------------------------------------------------

    def _git(self, cwd: Path, *args: str, check: bool = True):
        return self._git_runner(["git", *args], cwd, check)

    def _api(self, method: str, path: str, fields: list[str], raw: bool = False):
        """One forge API call, parsed.

        Every collaboration verb reaches the forge through here, so this is the
        single place that knows the call is currently made by shelling `gh`.
        """
        done = self._cli_runner(["gh", "api", "--method", method, path, *fields])
        if done.returncode != 0:
            # The forge's own first line, trimmed. It says which field was
            # rejected or that the branch has no commits; a caller told only
            # "the forge refused it" has to guess.
            detail = (done.stderr or done.stdout or "").strip().splitlines()
            raise WorkspaceError(
                detail[0][:400] if detail else f"the forge refused {method} {path}",
                status=502,
                code="FORGE_CALL_FAILED",
            )
        if raw:
            return done.stdout or ""
        try:
            return json.loads(done.stdout or "null")
        except json.JSONDecodeError as exc:
            raise WorkspaceError(
                "the forge returned something that is not JSON",
                status=502,
                code="FORGE_CALL_FAILED",
            ) from exc

    def _scratch(self, kind: str) -> Path:
        """A fresh directory under the broker's root.

        Named from a counter rather than from anything the caller sent. A
        directory named after a repository is a directory two requests for the
        same repository collide in, and the name is also a place a caller-chosen
        string would reach the filesystem.
        """
        self._sequence += 1
        path = self.scratch_root / f"{kind}-{os.getpid()}-{self._sequence}"
        if path.exists():
            _remove_tree(path)
        path.mkdir(parents=True)
        return path

    def _enforce_ceiling(self, root: Path, repo: str) -> None:
        total = 0
        for directory, _subdirs, filenames in os.walk(root):
            for filename in filenames:
                try:
                    total += os.lstat(os.path.join(directory, filename)).st_size
                except OSError:
                    continue
                if total > self.max_clone_bytes:
                    raise WorkspaceError(
                        f"{repo} is larger than the {self.max_clone_bytes}-byte "
                        "ceiling for a broker-side clone. Name a `branch` to "
                        "fetch one line of development.",
                        status=413,
                        code="CLONE_TOO_LARGE",
                    )

    def _default_branch(self, root: Path) -> str:
        result = self._git(
            root, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD",
            check=False,
        )
        ref = (result.stdout or "").strip()
        if result.returncode == 0 and ref:
            return ref.split("/", 1)[1] if ref.startswith("origin/") else ref
        local = self._git(
            root, "symbolic-ref", "--quiet", "--short", "HEAD", check=False
        )
        return (local.stdout or "").strip() or "main"

    # ---- repository verbs ----------------------------------------------

    def capabilities(self, payload: dict[str, Any]) -> dict[str, Any]:
        """What this install can do with this repository, before anything is spent.

        Answered without minting a credential or touching the network. A caller
        that discovers the gap by failing halfway through a publish has already
        written the revision it cannot deliver.
        """
        try:
            forge, repo = resolve_forge(payload.get("repository"))
        except ForgeUnsupported as exc:
            return {
                "forge": None,
                "repo": None,
                "proposalNoun": None,
                "verbs": [],
                "missing": [str(exc)],
            }
        return forge.capabilities(repo)

    def clone(self, payload: dict[str, Any]) -> dict[str, Any]:
        """The repository's history, as a bundle, with nothing left behind.

        The tree is removed before the response is composed rather than on a
        later `close`, because there is no later: these routes hold no state, so
        a caller that dies mid-request costs the broker nothing.

        There is no `depth`, and this is a property of the transport rather than
        an omission. `git bundle create` in a shallow repository succeeds and
        writes a bundle whose boundary revisions name parents the bundle does
        not carry; cloning it fails with "remote did not send all necessary
        objects". Naming a `branch` is the size control that does work, because
        it makes the clone single-branch.
        """
        forge, repo = resolve_forge(payload.get("repository"))
        branch = payload.get("branch")
        branch = validate_branch(branch) if branch is not None else None
        if payload.get("depth") is not None:
            raise WorkspaceError(
                "history is transferred as a bundle, which cannot carry a "
                "shallow boundary. Name a `branch` to fetch one line of "
                "development instead."
            )
        forge.mint(self._refresh, repo)

        root = self._scratch("clone")
        bundle = root.parent / f"{root.name}.bundle"
        try:
            argv = ["clone", "--quiet", "--no-recurse-submodules"]
            if branch is not None:
                argv += ["--single-branch", "--branch", branch]
            argv += [forge.clone_url(repo), "."]
            self._git(root, *argv)
            self._enforce_ceiling(root, repo)
            if branch is None:
                branch = self._default_branch(root)
            self._git(root, "checkout", "--force", "-B", branch, f"origin/{branch}")
            head = self._git(root, "rev-parse", "HEAD").stdout.strip()
            # `HEAD` as well as the branch, and not redundantly: a bundle
            # written from a named branch alone carries no HEAD ref, and a clone
            # from it lands with an unborn HEAD and nothing checked out. The
            # reader then holds a repository whose log says it has no revisions.
            self._git(root, "bundle", "create", str(bundle), "HEAD", branch)
            size = bundle.stat().st_size
            if size > self.max_bundle_bytes:
                raise WorkspaceError(
                    f"{repo}'s history is {size} bytes, over the "
                    f"{self.max_bundle_bytes}-byte ceiling. Name a `branch` to "
                    "fetch one line of development.",
                    status=413,
                    code="BUNDLE_TOO_LARGE",
                )
            blob = base64.b64encode(bundle.read_bytes()).decode("ascii")
        finally:
            bundle.unlink(missing_ok=True)
            _remove_tree(root)
        return {
            "forge": forge.name,
            "repo": repo,
            "branch": branch,
            "revision": head,
            "size": size,
            "bundleBase64": blob,
        }

    def publish(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Take the caller's revisions as a bundle and put them on the remote.

        Four checks stand between the bundle and the remote, and each one exists
        because the objects came from the sandbox:

        The bundle must carry exactly the branch it claims. A bundle holding a
        second ref would publish something the caller did not declare, and a
        fetch of one ref would leave the rest unmentioned in the answer.

        Its tip must descend from the revision the caller was handed by `clone`.
        That is what makes this an extension of known history rather than a
        replacement of it.

        The target branch's current tip must also be an ancestor, checked after
        the fetch that learns it. Between `clone` and `publish` somebody else may
        have pushed, and without this the caller's branch would silently discard
        that work.

        And nothing is ever checked out. The scratch repository is fetched into
        and pushed from, never materialised into a working copy, so no
        `.gitattributes`, hook, or `.gitmodules` among the incoming objects has
        anything to act on.
        """
        forge, repo = resolve_forge(payload.get("repository"))
        branch = validate_branch(payload.get("branch"))
        target = validate_branch(payload.get("target"), "target")
        base_revision = validate_revision(payload.get("baseRevision"))
        raw = payload.get("bundleBase64")
        if not isinstance(raw, str) or not raw:
            raise WorkspaceError("bundleBase64 must be a base64 bundle")
        try:
            blob = base64.b64decode(raw, validate=True)
        except Exception as exc:  # noqa: BLE001 - binascii.Error and TypeError
            raise WorkspaceError("bundleBase64 is not valid base64") from exc
        if len(blob) > self.max_bundle_bytes:
            raise WorkspaceError(
                f"the bundle is {len(blob)} bytes, over the "
                f"{self.max_bundle_bytes}-byte ceiling",
                status=413,
                code="BUNDLE_TOO_LARGE",
            )
        forge.mint(self._refresh, repo)

        root = self._scratch("publish")
        bundle = root.parent / f"{root.name}.bundle"
        try:
            bundle.write_bytes(blob)
            self._git(root, "init", "--quiet")
            self._git(root, "remote", "add", "origin", forge.clone_url(repo))
            # The target first, so the ancestry checks below have something to
            # be about.
            self._git(root, "fetch", "--quiet", "--no-tags", "origin", target)
            remote_target = self._git(root, "rev-parse", "FETCH_HEAD").stdout.strip()

            # Then the branch itself, when the remote already has it. A second
            # publish onto a branch this caller opened earlier carries
            # prerequisites that sit on that branch and nowhere near the
            # target, so fetching only the target leaves the bundle unreadable
            # -- which reaches the caller as a git failure rather than as an
            # answer about their revisions.
            existing_head = ""
            existing = self._git(
                root, "ls-remote", "--exit-code", "origin", f"refs/heads/{branch}",
                check=False,
            )
            if existing.returncode == 0:
                existing_head = (existing.stdout or "").split("\t", 1)[0].strip()
                self._git(
                    root, "fetch", "--quiet", "--no-tags", "origin",
                    f"refs/heads/{branch}",
                )

            listed = self._git(root, "bundle", "list-heads", str(bundle)).stdout
            refs = [
                line.split(" ", 1)[1].strip()
                for line in listed.splitlines()
                if " " in line
            ]
            wanted = {f"refs/heads/{branch}", branch}
            if len(refs) != 1 or refs[0] not in wanted:
                raise WorkspaceError(
                    f"the bundle carries {refs or 'no refs'}; it must carry "
                    f"exactly refs/heads/{branch}"
                )
            self._git(root, "fetch", "--quiet", str(bundle), f"+{refs[0]}:{_INCOMING}")
            tip = self._git(root, "rev-parse", _INCOMING).stdout.strip()

            if not self._is_ancestor(root, base_revision, tip):
                raise WorkspaceError(
                    f"the bundle's tip {tip[:12]} does not descend from "
                    f"{base_revision[:12]}, the revision this copy was cloned at",
                    status=409,
                    code="NOT_FAST_FORWARD",
                )
            if not self._is_ancestor(root, remote_target, tip):
                raise WorkspaceError(
                    f"{target} has moved on the remote since this copy was "
                    "cloned. Clone again and reapply the change.",
                    status=409,
                    code="BASE_MOVED",
                )
            if existing_head and not self._is_ancestor(root, existing_head, tip):
                raise WorkspaceError(
                    f"{branch} exists on the remote at {existing_head[:12]} and "
                    "the bundle does not build on it",
                    status=409,
                    code="BRANCH_DIVERGED",
                )
            self._git(root, "push", "origin", f"{_INCOMING}:refs/heads/{branch}")
        finally:
            bundle.unlink(missing_ok=True)
            _remove_tree(root)
        return {"forge": forge.name, "repo": repo, "branch": branch, "revision": tip}

    def _is_ancestor(self, root: Path, ancestor: str, descendant: str) -> bool:
        if not ancestor:
            return False
        result = self._git(
            root, "merge-base", "--is-ancestor", ancestor, descendant, check=False
        )
        return result.returncode == 0

    # ---- collaboration verbs -------------------------------------------

    def _forge_verb(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        forge, repo = resolve_forge(payload.get("repository"))
        forge.mint(self._refresh, repo)
        result = getattr(forge, method)(self._api, repo, payload)
        result.update({"forge": forge.name, "repo": repo})
        return result

    def proposal_create(self, payload):
        return self._forge_verb("proposal_create", payload)

    def proposal_list(self, payload):
        return self._forge_verb("proposal_list", payload)

    def proposal_view(self, payload):
        return self._forge_verb("proposal_view", payload)

    def proposal_comment(self, payload):
        return self._forge_verb("proposal_comment", payload)

    def issue_list(self, payload):
        return self._forge_verb("issue_list", payload)

    def issue_view(self, payload):
        return self._forge_verb("issue_view", payload)

    def issue_create(self, payload):
        return self._forge_verb("issue_create", payload)

    def issue_comment(self, payload):
        return self._forge_verb("issue_comment", payload)


def route_table(broker: VcsBroker) -> dict[str, Callable[[dict], dict]]:
    """The verbs `POST /v1/vcs/<verb>` dispatches to.

    Hyphens in the URL, underscores in the method names. The dispatcher
    normalises the two, so `proposal-create` and `proposal_create` reach the
    same route and no caller fails on punctuation.
    """
    return {
        "capabilities": broker.capabilities,
        "clone": broker.clone,
        "publish": broker.publish,
        "proposal-create": broker.proposal_create,
        "proposal-list": broker.proposal_list,
        "proposal-view": broker.proposal_view,
        "proposal-comment": broker.proposal_comment,
        "issue-list": broker.issue_list,
        "issue-view": broker.issue_view,
        "issue-create": broker.issue_create,
        "issue-comment": broker.issue_comment,
    }


__all__ = [
    "FORGES",
    "HOSTS",
    "ForgeUnsupported",
    "GitHubForge",
    "VcsBroker",
    "repository_host",
    "resolve_forge",
    "route_table",
    "validate_branch",
    "validate_revision",
    "vcs_enabled",
]
