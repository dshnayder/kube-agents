#!/usr/bin/env python3
"""GitLab's JSON, turned into the concepts every forge has under another name.

Where GitLab's vocabulary stops, as `../github/translate.py` is where GitHub's
does. Four differences from GitHub's shapes are absorbed here:

- **`iid`, never `id`.** A merge request or an issue carries both: `id` is
  global, `iid` is the number a person sees and the one every API path takes.
  `number` is the `iid`.
- **State words.** GitLab says `opened` and `locked`; the neutral states are
  `open`, `closed` and, for a proposal, `merged`.
- **Labels are strings,** not `{name: ...}` objects.
- **Notes.** One notes endpoint carries a conversation, comments on lines of
  the diff, and GitLab's own bookkeeping ("changed the description", "added
  label ..."), told apart by `system`. Bookkeeping is not an utterance, so it
  never becomes a comment. The rest map onto the neutral kinds every consumer
  already reads: a note on a line of the diff is a `review_comment`, any other
  note an `issue` comment -- the conversation, which is what a caller looking
  for its own markers reads.
"""

from __future__ import annotations

import re
from typing import Any

#: The neutral comment kinds a note becomes.
CONVERSATION = "issue"
DIFF_NOTE = "review_comment"
#: The kinds an award emoji can be left on: every note.
ACKNOWLEDGEABLE = frozenset({CONVERSATION, DIFF_NOTE})

#: How GitLab names the bot user behind a project or group access token.
#: `bot` is on the user object only on some endpoints, so the name is the
#: fallback the comment readers can always apply.
#: Current instances name them `project_<id>_bot_<hex>`; older ones numbered
#: each further token's user `project_<id>_bot1`, `_bot2`, and kept the names.
_TOKEN_BOT_RE = re.compile(r"^(project|group)_\d+_bot(_[0-9a-f]+|\d+)?$")

#: The title prefixes GitLab reads as a draft, case-blind. Reported as
#: `draft`, and kept in `title` as GitLab returns it, because a caller that
#: compares a title it wrote with the one it reads back wrote the prefix too.
#: `WIP:` is not here: GitLab stopped reading it in 16.0, so a title that
#: starts with it still needs `Draft:` in front to be one.
DRAFT_PREFIXES = ("Draft:", "[Draft]", "(Draft)")


def is_draft_title(title: str) -> bool:
    """Whether GitLab will read this title as a draft. It matches case-blind."""
    lowered = title.lower()
    return lowered.startswith(tuple(prefix.lower() for prefix in DRAFT_PREFIXES))


def actor(node: dict[str, Any] | None) -> str:
    """A username. GitLab has no suffix to strip."""
    return str((node or {}).get("username") or "").strip()


#: Service accounts, which a group or project adds as a member so a pipeline
#: or an integration can act: `service_account_<...>` by default.
_SERVICE_ACCOUNT_RE = re.compile(r"^service_account_", re.IGNORECASE)

#: GitLab's own automation users. A short list of the ones that comment on
#: issues and merge requests, compared case-blind; an instance that adds its
#: own is covered by `bot: true` where the payload carries it.
GITLAB_BOT_USERS = frozenset(
    {"support-bot", "alert-bot", "gitlab-security-bot", "ghost", "gitlab-duo"}
)


def is_automation(node: dict[str, Any] | None) -> bool:
    """Whether a note's author is an automation rather than a person.

    `bot` is on the user object only on some endpoints -- a note's `author`
    carries none -- so the name is what the comment readers can always apply:
    an access token's bot user, a service account, or one of GitLab's own
    automation users (a `duo-` review bot among them).
    """
    node = node or {}
    if bool(node.get("bot")):
        return True
    login = actor(node)
    lowered = login.lower()
    return bool(
        _TOKEN_BOT_RE.match(login)
        or _SERVICE_ACCOUNT_RE.match(login)
        or lowered in GITLAB_BOT_USERS
        or lowered.startswith("duo-")
    )


def _labels(node: dict[str, Any]) -> list[str]:
    return [str(item) for item in (node.get("labels") or []) if isinstance(item, str)]


def proposal(node: dict[str, Any], repo: str = "") -> dict[str, Any]:
    """A merge request as a proposal.

    `sourceRepo` is the repository the source branch lives in. GitLab gives
    project ids, not paths: a merge request whose source and target project are
    the same is from `repo` itself, and one from a fork answers `""`, which is
    the neutral "the forge does not say it is this repository" every caller
    already reads as not its own.

    `closed` is when it merged or closed (GitLab leaves `closed_at` empty on a
    merge), `""` while open.
    """
    raw_state = str(node.get("state") or "")
    if raw_state == "merged":
        state = "merged"
    elif raw_state in ("opened", "locked"):
        # `locked` is the moment GitLab is merging it: still open, and a
        # caller asking "is there an open proposal for my branch" must not
        # see none and open a second.
        state = "open"
    else:
        state = "closed"
    same_project = (
        node.get("source_project_id") is not None
        and node.get("source_project_id") == node.get("target_project_id")
    )
    draft = node.get("draft")
    if draft is None:
        draft = node.get("work_in_progress")
    return {
        "number": node.get("iid"),
        "title": node.get("title") or "",
        "state": state,
        "draft": bool(draft),
        "author": actor(node.get("author")),
        "labels": _labels(node),
        "source": node.get("source_branch") or "",
        "sourceRepo": repo if same_project else "",
        "sourceRevision": node.get("sha") or "",
        "target": node.get("target_branch") or "",
        "url": node.get("web_url") or "",
        "created": node.get("created_at") or "",
        "updated": node.get("updated_at") or "",
        "closed": node.get("merged_at") or node.get("closed_at") or "",
        "body": node.get("description") or "",
    }


def issue(node: dict[str, Any]) -> dict[str, Any]:
    raw_state = str(node.get("state") or "")
    return {
        "number": node.get("iid"),
        "title": node.get("title") or "",
        "state": "open" if raw_state == "opened" else raw_state,
        "author": actor(node.get("author")),
        "labels": _labels(node),
        "assignees": [actor(person) for person in (node.get("assignees") or [])],
        "url": node.get("web_url") or "",
        "created": node.get("created_at") or "",
        "updated": node.get("updated_at") or "",
        "body": node.get("description") or "",
    }


def comment(node: dict[str, Any]) -> dict[str, Any]:
    """A note as a comment.

    A note on a line of the diff carries `position`, and is a `review_comment`
    whose file and line are `path` and `line`, as a GitHub review comment's
    are; any other note is a conversation (`issue`) comment. Note ids are
    unique across a GitLab instance, so `ref` is unique however the kinds mix.
    """
    ident = node.get("id")
    position = node.get("position") or {}
    kind = DIFF_NOTE if position else CONVERSATION
    return {
        "id": ident,
        "ref": f"{kind}-{ident}",
        "kind": kind,
        "author": actor(node.get("author")),
        "bot": is_automation(node.get("author")),
        "created": node.get("created_at") or "",
        "body": node.get("body") or "",
        "url": "",
        "path": position.get("new_path") or position.get("old_path") or "",
        "line": position.get("new_line") or position.get("old_line"),
    }


def is_system_note(node: dict[str, Any]) -> bool:
    return bool(node.get("system"))


def commit(node: dict[str, Any]) -> dict[str, Any]:
    """One commit. The committer date, for the reason GitHub's translation gives."""
    return {
        "sha": node.get("id") or "",
        "author": node.get("author_name") or "",
        "committed": node.get("committed_date") or "",
        "message": node.get("message") or "",
        "url": node.get("web_url") or "",
    }


def label(node: dict[str, Any]) -> dict[str, Any]:
    """A label. Colour without the `#` GitLab keeps, as the neutral shape has it."""
    return {
        "name": node.get("name") or "",
        "color": str(node.get("color") or "").lstrip("#"),
        "description": node.get("description") or "",
    }
