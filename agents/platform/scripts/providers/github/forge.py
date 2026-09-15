#!/usr/bin/env python3
"""GitHub: which calls to make, and nothing about how they are made.

Only the API is used, never `gh pr` or `gh issue`. Those subcommands infer the
repository from a nearby `.git/config` -- the one file this whole design exists
to keep out of the credentialed process -- and they format for a human, which
is not something a translation can be written against. So this class is a REST
client's *description* of a REST client: it names paths, parameters and bodies,
and the transport the broker built for it does the calling.

`transport = "cli"` is the only reason `gh` is in the broker image at all: it
was already there for the App installation flow, so borrowing it costs nothing.
That is a fact about this install's history rather than about GitHub, which is
why it is one word here and not a shape the interface has to have.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Mapping
from urllib.parse import quote

import repo_ref

from ..base import COLLABORATION_VERBS, Forge, WorkspaceError, listing
from ..credentials import BrokeredCredential
from ..validate import (
    repo_segments,
    validate_branch,
    validate_labels,
    validate_limit,
    validate_number,
    validate_state,
    validate_text,
)
from . import translate
from .errors import ERROR_OVERRIDES

# The media type that makes the pull-request endpoint answer with a unified
# diff instead of JSON.
DIFF_MEDIA_TYPE = "application/vnd.github.v3.diff"


# `repos/{r}/collaborators/{login}/permission` values that mean "may write".
WRITE_PERMISSIONS = frozenset({"admin", "write", "maintain"})


class GitHubForge(Forge):
    name = "github"
    hosts = ("github.com", "www.github.com")
    proposal_noun = "pull request"
    verbs = COLLABORATION_VERBS
    transport = "cli"
    cli = "gh"
    error_overrides = ERROR_OVERRIDES
    acknowledges = True

    def __init__(self, refresh: Callable[[str, str], None] | None = None) -> None:
        super().__init__()
        self.credential = BrokeredCredential(self.name, refresh)

    @classmethod
    def for_config(cls, config: Mapping[str, Any]) -> Iterable[Forge]:
        """Exactly one, always.

        An install has one GitHub or it has none, and "none" is not a state
        this repository has ever been in -- github.com is where it lives. The
        argument is read only for the refresh operation to hand the credential.
        """
        return (cls(refresh=config.get("refresh")),)

    # -- identity -----------------------------------------------------------

    def parse(self, url: str) -> str:
        try:
            parts = repo_segments(url, self.hosts)
        except repo_ref.RepoRefError as error:
            raise WorkspaceError(
                f"{url!r} is not a GitHub repository; expected owner/name"
            ) from error
        if len(parts) != 2:
            raise WorkspaceError(
                f"{url!r} is not a GitHub repository; expected owner/name"
            )
        return "/".join(parts)

    def clone_url(self, repo: str) -> str:
        return f"https://github.com/{repo}.git"

    # -- shared by two verbs ------------------------------------------------

    def _comments(self, api: Callable, repo: str, number: int, payload: dict) -> list:
        # The conversation tab. For an issue that is the whole discussion; for
        # a proposal it is one of three places -- see `_proposal_comments`.
        limit = validate_limit(payload.get("limit"))
        nodes = api(
            "GET",
            f"repos/{repo}/issues/{number}/comments",
            params={"per_page": limit},
        )
        return [translate.comment(node, "issue") for node in nodes]

    def _proposal_comments(self, api: Callable, repo: str, number: int, payload: dict) -> list:
        # GitHub splits one human-visible conversation across three endpoints:
        # the conversation tab, inline review comments on the diff, and the
        # summary body of a review. A reviewer typing "please fix this" has no
        # idea which one they used, so reading fewer than three means a caller
        # ignores requests at random. Each comment carries which one it came
        # from as `kind`, because that decides whether it can be acknowledged
        # (a review summary has no reaction endpoint) and whether `path` and
        # `line` mean anything. Oldest first, across all three.
        limit = validate_limit(payload.get("limit"))
        params = {"per_page": limit}
        out = self._comments(api, repo, number, payload)
        out += [
            translate.comment(node, "review_comment")
            for node in api("GET", f"repos/{repo}/pulls/{number}/comments", params=params)
        ]
        out += [
            translate.comment(node, "review")
            for node in api("GET", f"repos/{repo}/pulls/{number}/reviews", params=params)
            # A review with no summary body is an approval or a state change,
            # not an utterance.
            if (node.get("body") or "").strip()
        ]
        out.sort(key=lambda c: (c["created"], str(c["id"])))
        return out

    @staticmethod
    def _label_changes(payload: dict) -> tuple[list[str], list[str]]:
        # Validated before the first call an update makes, so a bad label
        # cannot leave a half-applied edit behind.
        return validate_labels(payload.get("labelsAdd")), validate_labels(payload.get("labelsRemove"))

    def _labels(self, api: Callable, repo: str, number: int, payload: dict) -> None:
        # Labels live on the issue side of GitHub's model for proposals too.
        # Adds are one call; each removal is its own, because that is the API.
        add, remove = self._label_changes(payload)
        if add:
            api("POST", f"repos/{repo}/issues/{number}/labels", body={"labels": add})
        for name in remove:
            api("DELETE", f"repos/{repo}/issues/{number}/labels/{quote(name, safe='')}")

    def can_write(self, api: Callable, repo: str, login: str) -> bool | None:
        # The collaborator-permission endpoint rather than `author_association`
        # off a comment: an App installation token sees every association as
        # NONE, which is the blindness forge.py's history records. A 404 is a
        # definitive no; any other failure is not an answer and says so.
        if not login:
            return False
        quoted = quote(login, safe="")
        try:
            data = api("GET", f"repos/{repo}/collaborators/{quoted}/permission")
        except WorkspaceError as exc:
            return False if exc.status == 404 else None
        permission = str((data or {}).get("permission") or "").strip().lower()
        return permission in WRITE_PERMISSIONS

    # -- proposals ----------------------------------------------------------

    def proposal_create(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        body = {
            "title": validate_text(payload.get("title"), "title").strip(),
            "body": validate_text(payload.get("body"), "body", required=False),
            "head": validate_branch(payload.get("source"), "source"),
            "base": validate_branch(payload.get("target"), "target"),
        }
        if payload.get("draft"):
            body["draft"] = True
        node = api("POST", f"repos/{repo}/pulls", body=body)
        return {"proposal": translate.proposal(node)}

    def proposal_list(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        limit = validate_limit(payload.get("limit"))
        params: dict[str, Any] = {
            "state": validate_state(payload.get("state")),
            "per_page": limit,
        }
        source = payload.get("source")
        if source is not None:
            # Asked as a filter rather than by listing everything and matching
            # on `source` here, because "is there an open proposal for the
            # branch I just published" is the question every submitting caller
            # asks, and a page of the newest twenty proposals answers it wrong
            # on a busy repository.
            #
            # The owner qualifier is this repository's own. The bare branch
            # name is also accepted here and matches the same branch on every
            # fork, which would let a fork's proposal answer for ours; a
            # published branch always lives on the repository itself.
            owner = repo.split("/")[0]
            params["head"] = f"{owner}:{validate_branch(source, 'source')}"
        target = payload.get("target")
        if target is not None:
            params["base"] = validate_branch(target, "target")
        nodes = api("GET", f"repos/{repo}/pulls", params=params)
        return listing([translate.proposal(node) for node in nodes], limit, "proposals")

    def proposal_view(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        number = validate_number(payload.get("number"))
        node = api("GET", f"repos/{repo}/pulls/{number}")
        result: dict[str, Any] = {"proposal": translate.proposal(node)}
        if payload.get("comments"):
            result["comments"] = self._proposal_comments(api, repo, number, payload)
        if payload.get("diff"):
            result["diff"] = api(
                "GET", f"repos/{repo}/pulls/{number}", raw=DIFF_MEDIA_TYPE
            )
        return result

    def proposal_comment(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        number = validate_number(payload.get("number"))
        node = api(
            "POST",
            f"repos/{repo}/issues/{number}/comments",
            body={"body": validate_text(payload.get("body"), "body")},
        )
        return {"comment": translate.comment(node, "issue")}

    def proposal_update(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        number = validate_number(payload.get("number"))
        self._label_changes(payload)
        body: dict[str, Any] = {}
        if payload.get("title") is not None:
            body["title"] = validate_text(payload.get("title"), "title").strip()
        if payload.get("body") is not None:
            body["body"] = validate_text(payload.get("body"), "body", required=False)
        # One PATCH whatever was given, so the answer is always the proposal
        # as it now stands; GitHub returns it unchanged for an empty patch.
        # Labels first, then the PATCH: the answer is the proposal as it now
        # stands, and a read taken before the labels landed would report them
        # missing -- seen live on the first run of this verb.
        self._labels(api, repo, number, payload)
        node = api("PATCH", f"repos/{repo}/pulls/{number}", body=body)
        return {"proposal": translate.proposal(node)}

    def proposal_close(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        number = validate_number(payload.get("number"))
        node = api("PATCH", f"repos/{repo}/pulls/{number}", body={"state": "closed"})
        return {"proposal": translate.proposal(node)}

    def proposal_commits(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        number = validate_number(payload.get("number"))
        limit = validate_limit(payload.get("limit"))
        nodes = api(
            "GET", f"repos/{repo}/pulls/{number}/commits", params={"per_page": limit}
        )
        return listing([translate.commit(node) for node in nodes], limit, "commits")

    def proposal_acknowledge(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        # Best-effort by contract: a courtesy so the reviewer sees something
        # inside the tick. A review summary has no reaction endpoint, which is
        # `False` rather than an error.
        comment = payload.get("comment") or {}
        if not isinstance(comment, dict):
            raise WorkspaceError("comment must be the {id, kind} of a comment")
        kind = str(comment.get("kind") or "")
        ident = validate_number(comment.get("id"), "comment.id")
        if kind == "issue":
            path = f"repos/{repo}/issues/comments/{ident}/reactions"
        elif kind == "review_comment":
            path = f"repos/{repo}/pulls/comments/{ident}/reactions"
        else:
            return {"acknowledged": False}
        api("POST", path, body={"content": "eyes"})
        return {"acknowledged": True}

    # -- issues -------------------------------------------------------------

    def issue_create(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        body: dict[str, Any] = {
            "title": validate_text(payload.get("title"), "title").strip(),
            "body": validate_text(payload.get("body"), "body", required=False),
        }
        labels = validate_labels(payload.get("labels"))
        if labels:
            body["labels"] = labels
        node = api("POST", f"repos/{repo}/issues", body=body)
        return {"issue": translate.issue(node)}

    def issue_list(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        limit = validate_limit(payload.get("limit"))
        params: dict[str, Any] = {
            "state": validate_state(payload.get("state")),
            "per_page": limit,
        }
        labels = validate_labels(payload.get("labels"))
        if labels:
            params["labels"] = ",".join(labels)
        query = validate_text(payload.get("query"), "query", required=False).strip()
        if query:
            # Free text means the search API, whose query grammar is GitHub's
            # own: the neutral request is text plus the same state and labels,
            # and this is where they become `repo:`, `is:issue` and `label:`
            # qualifiers. The result envelope is `{items}`, unlike `/issues`.
            terms = [query, f"repo:{repo}", "is:issue"]
            if params["state"] != "all":
                terms.append(f"is:{params['state']}")
            terms += [f'label:"{name}"' for name in labels]
            found = api(
                "GET", "search/issues", params={"q": " ".join(terms), "per_page": limit}
            )
            nodes = (found or {}).get("items") or []
        else:
            nodes = api("GET", f"repos/{repo}/issues", params=params)
        # GitHub's issues endpoint returns pull requests too -- a PR *is* an
        # issue there. Nowhere else models it that way, and a caller that asked
        # for issues and got proposals mixed in would have to know that. The
        # `pull_request` key is how they are told apart.
        issues = [node for node in nodes if "pull_request" not in node]
        # `per_page` bounded what GitHub sent, not what survived the filter, so
        # `truncated` is judged on the page rather than on the remainder.
        return listing(
            [translate.issue(node) for node in issues],
            limit,
            "issues",
            returned=len(nodes),
        )

    def issue_view(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        number = validate_number(payload.get("number"))
        node = api("GET", f"repos/{repo}/issues/{number}")
        if "pull_request" in node:
            raise WorkspaceError(
                f"#{number} is a {self.proposal_noun}, not an issue; "
                "read it with `proposal view`"
            )
        result: dict[str, Any] = {"issue": translate.issue(node)}
        if payload.get("comments"):
            result["comments"] = self._comments(api, repo, number, payload)
        return result

    def issue_comment(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        number = validate_number(payload.get("number"))
        node = api(
            "POST",
            f"repos/{repo}/issues/{number}/comments",
            body={"body": validate_text(payload.get("body"), "body")},
        )
        return {"comment": translate.comment(node, "issue")}

    def issue_update(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        number = validate_number(payload.get("number"))
        self._label_changes(payload)
        body: dict[str, Any] = {}
        if payload.get("title") is not None:
            body["title"] = validate_text(payload.get("title"), "title").strip()
        if payload.get("body") is not None:
            body["body"] = validate_text(payload.get("body"), "body", required=False)
        # Labels first, then the PATCH: the answer is the issue as it now
        # stands, and a read taken before the labels landed would report them
        # missing -- seen live on the first run of this verb.
        self._labels(api, repo, number, payload)
        node = api("PATCH", f"repos/{repo}/issues/{number}", body=body)
        return {"issue": translate.issue(node)}

    def issue_close(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        number = validate_number(payload.get("number"))
        reason = validate_text(payload.get("reason"), "reason", required=False).strip()
        body: dict[str, Any] = {"state": "closed"}
        if reason:
            if reason not in ("completed", "not-planned"):
                raise WorkspaceError("reason must be one of completed, not-planned")
            body["state_reason"] = reason.replace("-", "_")
        node = api("PATCH", f"repos/{repo}/issues/{number}", body=body)
        return {"issue": translate.issue(node)}

    # -- labels -------------------------------------------------------------

    def label_ensure(self, api: Callable, repo: str, payload: dict) -> dict[str, Any]:
        # Read, then create or update. Creating first and reading the 422 back
        # would work on GitHub and nowhere else; a read that 404s is the
        # portable spelling of "does not exist yet".
        name = validate_labels([payload.get("name")])[0]
        body: dict[str, Any] = {"name": name}
        color = validate_text(payload.get("color"), "color", required=False).strip().lstrip("#")
        if color:
            body["color"] = color
        description = validate_text(payload.get("description"), "description", required=False)
        if description:
            body["description"] = description
        quoted = quote(name, safe="")
        try:
            api("GET", f"repos/{repo}/labels/{quoted}")
        except WorkspaceError as exc:
            if exc.status != 404:
                raise
            node = api("POST", f"repos/{repo}/labels", body=body)
        else:
            node = api("PATCH", f"repos/{repo}/labels/{quoted}", body=body)
        return {"label": translate.label(node)}
