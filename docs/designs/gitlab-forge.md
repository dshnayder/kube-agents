# GitLab as a second forge

> **Status:** proposed. Branch `feat/vcs-gitlab-forge`, based on
> `feat/version-control-abstraction`, to be merged into it before that reaches
> upstream. Nothing here is implemented yet. The endpoint-level claims in
> [Translation](#translation) are written from the GitLab v4 API as documented
> and are marked where a live instance has to confirm them — see
> [Where this gets validated](#where-this-gets-validated).

## Summary

[`version-control-abstraction.md`](version-control-abstraction.md) built a forge
seam and registered `gitlab.com` as a `_StubForge` that parses GitLab
repository specs and refuses everything else. This is the design for the module
that replaces it.

GitLab is the first forge that is not GitHub, so it is the one that finds out
whether the seam is in the right place. Mostly it is: cloning, bundling,
ancestry checking, pushing, the scratch lifecycle, the size ceilings and the
whole sandbox client need no GitLab-specific code at all, which is what the
abstraction was for. The `Forge` interface survives with one addition.

Three things are not what the existing design predicts.

**The stub has it backwards about credentials.** `_StubForge` refuses gitlab.com
because "no credential minter is configured; the GitHub App installation flow
has no GitLab equivalent here." That names an absence as a blocker when it is
an absence of a requirement: a GitLab group access token is long-lived, so
`mint()` does nothing and the base-class default is already correct. The
credential work for GitLab is not minting. It is that **`git` and the API
authenticate through two different mechanisms, and the interface only has a
seam for one of them** — GitHub hides this because `gh auth setup-git` mutates
global git config as a side effect of minting. That is a new method on the
interface, and it is the one real change.

**There is a fifth GitHub leak the design does not list.** The four named in
[Where GitHub still shows through](version-control-abstraction.md#where-github-still-shows-through)
are real, but the `api` callable every collaboration verb receives is itself
`gh`-shaped: its third parameter is `list[str]` holding `["-f", "title=…"]`,
which is `gh api` argv, and `proposal_view` passes
`["-H", "Accept: application/vnd.github.v3.diff"]` through the neutral seam. A
second forge cannot use that signature, so it changes.

**Self-managed GitLab is the same module and a different registry.** The API is
identical, the token is the same shape, and only the hostname differs — so it
is one module. But `hosts` is a static tuple today and `HOSTS` is built at
import, and a customer's `gitlab.acme.internal` is known at deploy time. The
registry becomes something built from configuration rather than a literal.

| Layer                     | Where it goes                                                             |
| ------------------------- | ------------------------------------------------------------------------- |
| The module                | `GitLabForge` in `agents/platform/scripts/vcs_broker.py`                  |
| Its transport             | `HttpTransport`, same file, owned by the broker                           |
| Its credential            | a Secret, projected into the credential-proxy container                   |
| Which hosts are GitLab's  | operator configuration, rendered into the broker's environment            |
| The sandbox client        | unchanged — `vcs.py` needs no GitLab code                                 |

## How to read this document

Each section goes a level deeper than the one before it. A human reader can
stop as soon as they have what they came for; an agent should read all of it.

| Section                                             | What it gives you                                                       |
| --------------------------------------------------- | ------------------------------------------------------------------------ |
| [Why](#why)                                         | what GitLab costs and what the stub gets wrong — stop here if that is it |
| [What GitLab changes](#what-gitlab-changes)         | the four decisions that touch shared code                               |
| [The interface, revised](#the-interface-revised)    | the seam after those decisions                                          |
| [The module](#the-module)                           | identity, credentials, translation, errors                              |
| [What is not built](#what-is-not-built)             | the limits this ships with, deliberately                                |
| [Delivery](#delivery)                               | the order, and what each step can be tested against                     |
| [Open questions](#open-questions)                   | what is still undecided                                                 |

---

## Why

GitLab is the forge customers ask for first after GitHub, and
[`version-control-abstraction.md`](version-control-abstraction.md) exists
because of that ask. Everything up to now has been the layer; this is the first
thing that uses it.

It is also the test of the layer. An abstraction with one implementation is a
hypothesis. The measure of this design is not that GitLab works — it is how much
shared code had to change to make it work, and whether Bitbucket will need those
same changes made again.

### What GitLab actually costs

Small, and smaller than the stub suggests. The forge-independent half of the
broker — `clone`, `publish`, the five publish checks, `_enforce_ceiling`,
`_scratch`, `_default_branch`, the bundle transport, the lock — is untouched.
So is the whole of `vcs.py`, 951 lines, because the sandbox never learns which
forge it is talking to. So is the `version-control` skill, the CRD surface, the
sandbox image and the entrypoint.

What is new is one class of roughly the size of `GitHubForge`, one transport,
and the interface changes below.

### What the stub gets wrong

`_StubForge` for gitlab.com names two gaps:

> no credential minter is configured for gitlab.com; the GitHub App
> installation flow has no GitLab equivalent here

> merge requests and issues need a GitLab REST client in the broker; `gh api`
> cannot reach them

The second is right. The first is true and misleading: GitLab has no
App-installation flow because it does not need one. A **group access token** is
created once by an administrator, scoped to a group and everything under it,
and does not expire on an hourly cycle. `Forge.mint` already defaults to doing
nothing, and its docstring already anticipates this —

> a forge configured with a long-lived personal token has nothing to do here

— so the correct GitLab implementation of the method the stub calls a blocker
is to not implement it.

That is worth stating plainly because it changes the sequencing. The credential
work GitLab needs is not a minter. It is [the git seam](#gits-credential-has-no-seam)
and a scope boundary the broker enforces itself, neither of which the stub
mentions.

---

## What GitLab changes

Four decisions, in the order they were forced.

### `git`'s credential has no seam

This is the finding that matters, and it is the one place the existing
interface is actually wrong rather than merely GitHub-flavoured.

The broker authenticates to a forge twice, by two mechanisms:

- **The API**, in the collaboration verbs, through the `api` callable.
- **`git` itself**, in `clone` and `publish`, which run `git clone` and
  `git push` against `forge.clone_url(repo)` — a plain `https://…` URL with no
  credential in it.

Nothing on the `Forge` interface supplies the second one. GitHub gets away with
this because `github_token_refresh.py` ends with:

```python
subprocess.run(["gh", "auth", "login", "--with-token"], input=token, …)
subprocess.run(["gh", "auth", "setup-git"], …)
```

`gh auth setup-git` writes a global git config entry making `gh` a credential
helper for github.com. So `GitHubForge.mint` authenticates git as an
*undeclared side effect* of authenticating the API, through a global config
file, from a script that is not part of the forge module. A reader of the
interface cannot see that `clone` will work, and a second forge inherits
nothing.

**The addition:** a sixth method.

```python
def git_config(self, repo: str) -> tuple[tuple[str, str], ...]:
    """Config keys this forge needs on the git invocations the broker makes
    on its behalf. Applied to those invocations only. Default is none."""
    return ()
```

`GitHubForge` returns `()` and keeps the helper `gh` installs — no behaviour
change, but now the interface says the coupling exists and the docstring names
where it comes from. `GitLabForge` returns a credential-helper pin:

```python
(("credential.helper", f"!f() {{ echo username=oauth2; echo password=$(cat {TOKEN_FILE}); }}; f"),)
```

Applied through the existing `GIT_CONFIG_COUNT` layer, which
`credential_proxy.py` already builds for `GIT_FORCED_CONFIG` and which outranks
system, global and repo-local config.

Three properties this shape has that the alternatives do not:

- **It is per-invocation, not global.** `GIT_FORCED_CONFIG` is one tuple applied
  to every git the broker runs, including the content-workspace git. A
  credential belonging to one forge does not belong on all of them.
- **The token is read from a file at use time, not interpolated into config.**
  A rotated Secret takes effect without restarting anything, and the token is
  not in the process environment, not in `/proc/*/environ`, and not in any
  argv the redactor has to catch.
- **It survives the token not being a bearer header.** GitLab accepts
  `username=oauth2` with the token as the password over HTTPS basic, which is
  what git does natively. No `http.extraheader`, which would have to be set
  per-host and which git logs in `GIT_TRACE`.

### The `api` callable is `gh`-shaped

The signature every collaboration verb receives today:

```python
def _api(self, method: str, path: str, fields: list[str], raw: bool = False)
```

`fields` is `gh api` argv. `GitHubForge.proposal_create` builds
`["-f", f"title={title}", "-f", f"body={body}", …]`; `proposal_view` builds
`["-H", "Accept: application/vnd.github.v3.diff"]`. Both are one CLI's
command-line spelling passing through the seam the design says nothing crosses
in a forge's own vocabulary. It is the same mistake as the four the design
names, in the parameter list rather than in a module constant.

**The change:** the transport takes a request, not argv.

```python
def api(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    raw: str | None = None,   # a media type, when the caller wants bytes
) -> Any
```

`params` are query parameters — today they are string-formatted into `path` by
every verb, `f"repos/{repo}/pulls?state={state}&per_page={limit}"`, which also
means nothing is URL-encoded. `body` is a JSON object. `raw` names the media
type rather than smuggling a header.

This touches all eight `GitHubForge` verbs. They are mechanical edits and no
behaviour changes; `-f`/`-F` construction moves into the CLI transport, which is
where `gh`'s spelling belongs. Doing it in the same change as GitLab is
deliberate — the point of the port is that the second forge proves the seam is
neutral, and a seam only one forge can pass through has not been proved.

### The transport cannot be a CLI

Given a neutral request shape, something has to execute it. GitHub's executor
shells `gh api`. GitLab's options are `glab`, or an HTTP client in the broker
process.

An earlier note on forge modularity settled that "the broker keeps owning
process execution; a forge supplies `api_command(...) -> argv`". That rule was
written when the only transport was a CLI, and GitLab is the case that breaks
it. **`glab` is rejected**:

- It re-imports the problem the design already solved once. `GitHubForge`'s
  docstring explains why only `gh api` is used and never `gh pr` or `gh issue`:
  those subcommands infer the repository from a `.git/config`, "the one file
  this whole design exists to keep out of the credentialed process". `glab` has
  the same subcommands with the same inference.
- It is a second binary in the credential-proxy image, with its own auth state
  on disk, its own config file, its own update-check network call, and its own
  CVE stream — in the container holding the token.
- It buys nothing. `gh` is in the image because it was already there for the
  GitHub App flow. There is no equivalent debt for GitLab.

**So: an in-process `HttpTransport` built on `urllib`.** The broker constructs
it; a forge never does. It is the broker that owns the timeout, the response
size cap, the redaction of the token out of anything logged, and the mapping
from HTTP status to the shared error contract.

Two things this changes that are worth being explicit about, because they are
the cost side:

- **The broker process makes direct outbound HTTPS.** Today its network I/O is
  in subprocesses. At the NetworkPolicy layer nothing changes — egress is per
  pod, and `git clone` already leaves that pod for the same host — but the
  egress policy needs the GitLab host, and for self-managed that host is
  customer-chosen and cannot be a literal in the repository. See
  [Open questions](#open-questions).
- **Timeouts and output caps stop being inherited.** The subprocess runner
  enforces both today. `HttpTransport` has to enforce them itself, and a test
  has to hold it to that, because "the CLI runner did it" is exactly the kind
  of property that quietly stops being true when the transport changes.

In exchange the token never enters an argv, an environment variable, or a child
process at all. That is strictly better than `gh`, and it is worth noting that
migrating `GitHubForge` onto `HttpTransport` later becomes a small change once
the App token is available to the broker directly — not proposed here, but the
shape does not foreclose it.

### One module, many hosts

`gitlab.com` and a self-managed GitLab are **one module**. The REST API is the
same `/api/v4`, the objects are the same, the token is the same shape. What
differs is the hostname, the network path to it, and where the token came from
— configuration, not code. Two classes would be one class and a copy that
drifts.

But `hosts` is a `tuple[str, ...]` class attribute and `HOSTS` is a dict
comprehension over `FORGES` evaluated at import. A customer's
`gitlab.acme.internal` is not knowable then. So:

```python
def build_forges(config: ForgeConfig) -> tuple[Forge, ...]
```

`FORGES` and `HOSTS` become what this returns, built once at broker
construction. `GitHubForge()` is unconditional. A `GitLabForge` is constructed
per configured host, or not at all when none is configured — and when none is
configured, `gitlab.com` falls back to the `_StubForge` that is there today, so
an install that has not set GitLab up gets the same named refusal it gets now
rather than a confusing authentication failure.

This also closes leak 1 — `FORGES` as a tuple literal — for the reason the
second forge forces rather than as tidying.

Where the configuration comes from is the reconciliation point with
[`multi-forge-support.md`](https://github.com/gke-labs/kube-agents/blob/main/docs/designs/multi-forge-support.md), which owns the declarative
surface (`spec.integration.git`). This design does not re-decide that. It states
what the broker needs to receive, in whatever form that surface renders:

| Field           | What it is                                                                   |
| --------------- | ------------------------------------------------------------------------------ |
| `host`          | the GitLab hostname                                                            |
| `tokenPath`     | file the projected Secret lands at                                             |
| `allowedPaths`  | namespace prefixes this token may be spent on — see [The credential](#the-credential) |

One caveat on hostnames, inherited rather than introduced: `repository_host`
treats a first segment with no dot in it as *not a host*, so a bare `owner/name`
resolves to GitHub. An in-cluster GitLab reached as `gitlab` with no domain
would be read as a repository named `gitlab`. Configure a dotted name; the
refusal is otherwise silent and confusing.

---

## The interface, revised

After the four decisions, `Forge` is:

| Member                  | Change    | What it decides                                                    |
| ----------------------- | --------- | -------------------------------------------------------------------- |
| `hosts`                 | —         | which hostnames are this forge's; also the credential allowlist      |
| `parse(url)`            | —         | the repository a URL names                                          |
| `clone_url(repo)`       | —         | the URL to clone, composed from validated segments                  |
| `capabilities(repo)`    | —         | what this install can do here, without minting or network           |
| `mint(refresh, repo)`   | —         | make the credential current, if it expires; default nothing         |
| `git_config(repo)`      | **new**   | config keys for the git invocations made on this forge's behalf     |
| `verbs`                 | **moved** | was `_GITHUB_VERBS`, a module constant; now a class attribute       |
| the eight verbs         | signature | receive the neutral `api` above rather than a `gh`-argv callable    |
| `transport`             | **new**   | which transport the broker builds for this forge: `"cli"` or `"http"` |

`transport` is a declaration, not an implementation — the forge names what it
needs and the broker constructs it. That keeps the rule the earlier note was
protecting (a forge says what to call, never how to execute it) while allowing
a transport that is not a subprocess.

### The five leaks, closed

| Leak                                       | Closed by                                                             |
| ------------------------------------------ | ----------------------------------------------------------------------- |
| 1. `FORGES` tuple literal                  | `build_forges(config)`, because self-managed hosts are not knowable at import |
| 2. `VcsBroker._api` shells `gh api`        | `transport` declaration; `CliTransport` and `HttpTransport`             |
| 3. `_forge_error` parses `(HTTP 404)`      | split: status extraction is per-transport, the guidance table stays shared |
| 4. `_GITHUB_VERBS` module constant         | `Forge.verbs` class attribute                                            |
| 5. the `api` callable's own signature      | neutral `(method, path, *, params, body, raw)`                          |

Leak 3 splits rather than moves. `_FORGE_ERRORS` — the status-to-guidance table
— is forge-neutral prose about what an agent should do next, and it stays
shared. What is GitHub-specific is *recovering the status at all*: `gh` prints
`(HTTP 404)` into stderr and a regex digs it out. `HttpTransport` has the status
as an integer. So `_forge_error(output)` becomes `forge_error(status, detail)`
with a `CliTransport`-local `status_from_output`. `_THROTTLE_MARKERS` moves onto
`GitHubForge`, because splitting 403 into "missing scope" and "throttled" on
message text is GitHub's problem — GitLab returns 429 with `RateLimit-*`
headers and does not need the heuristic.

---

## The module

### Repository identity

GitLab namespaces nest: `group/subgroup/project`, arbitrarily deep. This is the
one place GitLab is structurally different from GitHub rather than differently
spelled, and the abstraction already handles it — `_StubForge.parse` accepts
`len(parts) >= 2` with `_SEGMENT_RE` per segment, where `GitHubForge.parse`
demands exactly 2. `GitLabForge.parse` keeps the stub's version.

`clone_url` composes `https://{self.host}/{path}.git` from validated segments,
never from the caller's URL — same rule as GitHub, same reason: the caller's URL
decides which forge, not which host a credential is presented to.

API paths take the namespace URL-encoded as a single opaque segment:
`/api/v4/projects/{quote(path, safe='')}/merge_requests`. Note `safe=''` —
the default `quote` leaves `/` alone, which yields a path GitLab reads as a
different route. That is a one-character bug with a confusing 404 and it is
worth a test of its own.

**Numbering is the trap.** GitLab merge requests and issues each carry an `id`
(globally unique) and an `iid` (per-project, the number a human sees and the
one in the web URL). Every caller-facing `number` and every API path segment
must be the `iid`. Using `id` produces a route that resolves to a different
project's item or 404s, and the failure is silent in the sense that it looks
like a permissions problem. `GitLabForge` reads `iid` in translation and sends
`iid` in paths; nothing in the module touches `id`.

### The credential

A GitLab **group access token** with `api` and `write_repository` scope, created
by an administrator, stored in a Kubernetes Secret, projected into the
credential-proxy container as a file.

- `mint()` is not implemented — the base class default is correct.
- The API reads it at call time from the file and sends it as a
  `PRIVATE-TOKEN` header.
- `git` reads it at use time through the credential helper from
  [above](#gits-credential-has-no-seam).

Reading from the file per call rather than caching at construction is
deliberate: a rotated Secret updates the projected file, and the next call picks
it up with no restart. The cost is a file read per API call, which is nothing
next to the HTTPS round trip.

**Scope enforcement is the broker's job here, and this is a real difference from
GitHub.** Minty enforces a per-repository permission policy at mint time, so the
token the broker holds is already narrowed. A group access token is narrowed to
its group at creation and nothing narrows it further. Two repositories in the
same group are both reachable with it, and `resolve_forge`'s host allowlist does
not care which project inside the host a call names.

So `GitLabForge` carries `allowed_paths`, a tuple of namespace prefixes, and
refuses a repository outside them before the credential is spent — the same
placement as the host allowlist and for the same reason. An empty
`allowed_paths` means the whole host, which must be a deliberate configuration
rather than the default that appears when someone omits a field.

Prefix matching is on **path segments, not string prefix**. `acme/infra-secret`
starts with the string `acme/infra` and is a different project.

### Translation

The normalised shapes are the ones `GitHubForge` already produces. What follows
is the GitLab side of each.

| Neutral field       | GitLab source                     | Note                                                             |
| ------------------- | --------------------------------- | ------------------------------------------------------------------ |
| `number`            | `iid`                             | never `id`                                                        |
| `state` (proposal)  | `state`                           | `opened`→`open`, `merged`→`merged`, `closed`/`locked`→`closed`     |
| `draft`             | `draft`                           | `work_in_progress` on older instances; read `draft`, fall back     |
| `author`            | `author.username`                 | no `[bot]` suffix to strip                                        |
| `source` / `target` | `source_branch` / `target_branch` | direct                                                            |
| `url`               | `web_url`                         |                                                                   |
| `created`/`updated` | `created_at` / `updated_at`       | both ISO-8601, same as GitHub                                     |
| `body`              | `description`                     | GitLab's name for it                                              |
| `labels`            | `labels`                          | plain strings, not GitHub's `{name: …}` dicts                     |

Three asymmetries are worth their own note, because each one is a place the
neutral contract was shaped by GitHub and GitLab shows the shape.

**Issues are not proposals.** `GitHubForge.issue_list` filters out nodes
carrying a `pull_request` key, because on GitHub a PR *is* an issue and the
issues endpoint returns both. Nowhere else models it that way. GitLab's
`/issues` returns issues, so `GitLabForge.issue_list` has no filter and
`issue_view` needs no "this is a merge request, read it with `proposal view`"
refusal. The neutral contract is right and GitHub is the odd one; the filter
stays where it belongs, inside `GitHubForge`.

**Comments are notes, and most notes are not comments.** GitLab's
`/merge_requests/{iid}/notes` returns the discussion *and* system notes —
"changed the description", "assigned to @someone", "marked as draft" — each
carrying `system: true`. Returned unfiltered, a caller reading a proposal's
conversation gets mostly bookkeeping, and an agent deciding whether it has
already replied reads its own status changes as replies. `GitLabForge` filters
`system` notes out. This is GitLab's exact analogue of the `pull_request` filter
above: one forge's model leaking items the neutral concept does not include.

**State vocabulary differs on the way in as well as out.** `validate_state`
accepts `open`, `closed`, `all` and the verbs pass the result straight into a
query string. GitLab's parameter values are `opened`, `closed`, `all`. The
mapping belongs in `GitLabForge`, on both directions, and `validate_state`'s
neutral vocabulary does not change.

Two endpoints need naming because they are not a rename of GitHub's:

- **Diff.** GitHub serves a diff from the PR endpoint under an `Accept` media
  type. GitLab does not; the closest is
  `/merge_requests/{iid}/raw_diffs`. *(Live-verify: the exact path and whether
  it needs a size guard on a large MR.)*
- **Proposal creation** posts `source_branch`, `target_branch`, `title`,
  `description` to `/merge_requests`. GitLab's draft flag on creation has
  historically been a `Draft:` title prefix rather than a field; current
  instances accept neither reliably across versions. *(Live-verify: whether
  `draft` is settable at creation on the target version, and if not, whether
  `proposal_create` sets it in a second call or reports it unsupported.)*

Both are marked because getting them wrong is a working-looking module that
silently drops a field, which is worse than an unimplemented verb.

### Errors

GitLab returns conventional statuses, which the shared `_FORGE_ERRORS` table
already covers: 401, 403, 404, 409, 422, 429. Two specifics:

- The message body is `{"message": …}` or `{"error": …}` depending on endpoint,
  and sometimes a dict of per-field arrays. `detail` takes the first string it
  can find and truncates at 400 characters like the CLI path does.
- **404 hides 403.** GitLab answers 404 rather than 403 for a project the token
  cannot see, deliberately. The shared `FORGE_NOT_FOUND` guidance already says
  this — "A private repository this install's credential cannot see also answers
  404, so this does not prove the thing does not exist" — written for GitHub and
  correct here unchanged. Nothing to add.

---

## What is not built

Deliberately, and each of these should be a named refusal rather than a
surprise:

- **Self-managed instances behind a private CA.** The token and the API are the
  same; what differs is trust of the TLS chain. `HttpTransport` uses the
  container's CA bundle and nothing mounts a custom one.
- **GitLab groups as an issue tracker.** Group-level issues and epics are a
  different endpoint namespace. `issue_*` is project-scoped, matching the
  neutral concept.
- **Approvals.** GitLab's approval rules have no GitHub equivalent and no
  neutral verb. `proposal_view` does not report approval state.
- **Merging a proposal.** No forge implements this, on purpose — the neutral
  verb set stops at opening and commenting.
- **OAuth-refreshed tokens.** A group access token is the only supported
  credential. This is the decision Bitbucket will have to revisit, since its
  tokens come from a refresh flow, and `mint()` is where that goes.

## Delivery

Ordered so each step is testable before the next, and so nothing needs a live
GitLab until the last one.

| Step | Change                                                                    | Tested by                                       |
| ---- | ------------------------------------------------------------------------- | ------------------------------------------------- |
| 1    | `Forge.verbs`; `_GITHUB_VERBS` deleted                                    | existing `test_vcs_broker.py`, unchanged behaviour |
| 2    | neutral `api` signature; `CliTransport`; port the eight GitHub verbs      | existing tests, unchanged behaviour               |
| 3    | `forge_error(status, detail)` split; `_THROTTLE_MARKERS` onto `GitHubForge` | existing error tests, unchanged behaviour        |
| 4    | `Forge.git_config`; `GitHubForge` returns `()`                            | new test that the pins reach the git invocation   |
| 5    | `HttpTransport` — timeout, size cap, redaction, status mapping            | new unit tests against a local stub server        |
| 6    | `build_forges(config)`; `FORGES`/`HOSTS` from it; stub retained when unconfigured | new registry tests                        |
| 7    | `GitLabForge` — identity, credential, translation, errors                 | fixture-driven unit tests                        |
| 8    | operator: render the GitLab config and project the Secret                 | operator tests                                   |
| 9    | live validation                                                           | see below                                        |

Steps 1–4 are pure refactors of shared code with no GitLab in them. They are
worth landing as their own commits regardless of what happens to the rest,
because they are the design's own stated debt.

### Where this gets validated

No environment here has a GitLab. The endpoint-level claims marked
*live-verify* above cannot be closed without one, and neither can step 9.

The cheapest sufficient answer is a **gitlab.com project under a throwaway
group with a group access token** — it exercises the credential path, the
namespace nesting, `iid` handling, notes filtering and the real error shapes.
It does not exercise the customer-chosen hostname, the private CA, or the
egress policy for a host that is not in any repository literal.

A **self-managed GitLab CE in the development cluster** exercises all of it,
including the parts most likely to break at a customer, at the cost of standing
infrastructure. This is the same question
[`multi-forge-support.md`](https://github.com/gke-labs/kube-agents/blob/main/docs/designs/multi-forge-support.md) §10 asks and it should be
answered once for both.

Recommendation: gitlab.com for steps 7–8, and treat self-managed as a
prerequisite for calling GitLab supported rather than for merging the module.

## Open questions

1. **Where the forge configuration is declared.** This design states what the
   broker must receive; `multi-forge-support.md` owns the CR surface. The two
   have to agree before step 8, and that is the same unreconciled boundary
   issue #1154 names.
2. **Egress for a customer-chosen host.** The broker's NetworkPolicy needs the
   GitLab host, which for self-managed is not a literal anyone can ship. Either
   the operator renders it from the same configuration, or the policy widens in
   a way that should be argued for explicitly rather than by omission.
3. **Whether `allowed_paths` is this design's field or the shared surface's.**
   GitHub gets the same boundary from Minty's policy ConfigMap. One field that
   means "what this credential may be spent on" across forges would be better
   than two mechanisms, but the enforcement points genuinely differ.
4. **Whether `GitHubForge` should move onto `HttpTransport` afterwards.** Not
   proposed here. It would remove `gh` from the broker entirely, which
   [Replacing `gh`](version-control-abstraction.md#replacing-gh) wants for other
   reasons, and the transport split makes it a small change. It needs the App
   token reachable by the broker without `gh auth`.

## Related

- [`version-control-abstraction.md`](version-control-abstraction.md) — the seam
  this fills in, and the experiment behind it.
- [`multi-forge-support.md`](https://github.com/gke-labs/kube-agents/blob/main/docs/designs/multi-forge-support.md) — the agent-side provider
  contract and the declarative surface. Unreconciled with the above over
  `forge.py`.
- [`pr-comment-conversation.md`](pr-comment-conversation.md) §3 — the seven
  provider operations `forge.py` implements today, and the three normalisations
  that already exist because of a non-GitHub forge.
- Issue #1154 — the GitLab/Bitbucket tracking issue.
