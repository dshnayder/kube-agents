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
repository specs and refuses everything else. This is the design for the package
that replaces it.

GitLab is the first forge that is not GitHub, so it is the one that finds out
whether the seam is in the right place. Mostly it is: cloning, bundling,
ancestry checking, pushing, the scratch lifecycle, the size ceilings and the
whole sandbox client need no GitLab-specific code at all, which is what the
abstraction was for. The `Forge` interface comes out one member shorter.

Three things are not what the existing design predicts.

**The stub has it backwards about credentials.** `_StubForge` refuses gitlab.com
because "no credential minter is configured; the GitHub App installation flow
has no GitLab equivalent here." That names an absence as a blocker when it is
an absence of a requirement: a GitLab group access token is long-lived, so there
is nothing to mint. The real problem is the other direction — `mint()` is the
wrong seam for *any* forge, because it assumes acquisition is a pipeline every
forge is fitted into, and because it covers only half the credential: **`git`
and the API authenticate through two different mechanisms and the interface has
a seam for one**, GitHub hiding this because `gh auth setup-git` mutates global
git config as a side effect of minting. `mint` is replaced by a
[credential strategy the forge owns](#credentials-belong-to-the-forge), covering
acquisition, the API header and git's helper together. That is the one real
interface change.

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

**And the whole of this belongs in its own directory.** GitLab is the second
forge; Bitbucket is the third, and it should cost less than GitLab did. So the
organising requirement is not "make GitLab work" but **adding a forge is a new
directory and one line in a registration file, touching no shared file and no
other forge's code.** Today `GitHubForge`, its translation, its error parsing,
its verb list and the registry are all in one 1,214-line module with the
broker, so GitLab as written today would be an edit to GitHub's file.
[Modularity](#modularity) is that layout, and the two tests that keep it true.

| Layer                     | Where it goes                                                             |
| ------------------------- | ------------------------------------------------------------------------- |
| The forge packages        | `agents/platform/scripts/providers/{github,gitlab}/`                         |
| The shared contract       | `providers/base.py`, `validate.py`, `errors.py`, `identity.py`, `transport.py`, `credentials.py` |
| Registration              | `providers/registry.py` — the one shared file a new forge edits              |
| The broker                | `vcs_broker.py`, with no forge name left in it                            |
| Its credential            | a `Credential` strategy the forge owns, over a projected Secret           |
| Which hosts are GitLab's  | operator configuration, rendered into the broker's environment            |
| The sandbox client        | unchanged — `vcs.py` needs no GitLab code                                 |

## How to read this document

Each section goes a level deeper than the one before it. A human reader can
stop as soon as they have what they came for; an agent should read all of it.

| Section                                             | What it gives you                                                       |
| --------------------------------------------------- | ------------------------------------------------------------------------ |
| [Why](#why)                                         | what GitLab costs and what the stub gets wrong — stop here if that is it |
| [What GitLab changes](#what-gitlab-changes)         | the four decisions that touch shared code                               |
| [Modularity](#modularity)                           | the package layout, and what makes the boundary hold                    |
| [The interface, revised](#the-interface-revised)    | the seam after those decisions                                          |
| [The GitLab package](#the-gitlab-package)           | identity, credentials, translation, errors                              |
| [What is not built](#what-is-not-built)             | the limits this ships with, deliberately                                |
| [Delivery](#delivery)                               | the three PRs, and how they survive work in flight not landing          |
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
and lasts up to a year rather than an hour. `Forge.mint` already defaults to doing
nothing, and its docstring already anticipates this —

> a forge configured with a long-lived personal token has nothing to do here

— so the correct GitLab implementation of the method the stub calls a blocker
is to not implement it.

That the default is already right is the tell that the seam is in the wrong
place. A method whose correct implementation for the second forge is "inherit
the no-op" is not an abstraction over acquisition; it is GitHub's acquisition
with an escape hatch. What GitLab needs is not an empty `mint` but a
[different strategy](#credentials-belong-to-the-forge), because the parts of the
credential that are *not* empty for GitLab — the `PRIVATE-TOKEN` header, the git
helper, the file it reads — have nowhere to live on the current interface.

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

**The addition:** the credential declares git's config as well as the API's.

```python
def git_config(self, repo: str) -> tuple[tuple[str, str], ...]:
    """Config keys this forge needs on the git invocations the broker makes
    on its behalf. Applied to those invocations only. Default is none."""
    return ()
```

This sits on the credential rather than on `Forge` — see
[Credentials belong to the forge](#credentials-belong-to-the-forge) for why the
two halves are one object. GitHub's `BrokeredCredential` returns `()` and keeps
the helper `gh` installs — no behaviour change, but now the interface says the
coupling exists and the docstring names where it comes from. GitLab's
`StaticFileCredential` returns a credential-helper pin:

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

## Modularity

The four decisions above are what GitLab needs. This section is what the *third*
forge needs, and it is a different question. Bitbucket should cost less than
GitLab, not the same; the way that happens is that GitLab leaves behind a shape
Bitbucket fills in rather than a precedent Bitbucket imitates.

The requirement, stated so it can be checked: **adding a forge is a new
directory and one line in a registration file. It touches no shared module and
no other forge's code.**

Today that is false in both directions. `GitHubForge`, its verb list, its error
parsing, the registry and the broker are one 1,214-line module, so GitLab as
designed in the previous section would be an edit to GitHub's file — and a
GitHub change would be an edit to the file GitLab lives in. Neither is a
correctness problem yet. Both become one at three forges, when "who broke
GitHub" stops having a one-file answer.

### The layout

```text
agents/platform/scripts/
  vcs_broker.py            # broker verbs, clone/publish, scratch, locking, routes
  providers/
    __init__.py            # the public surface: Forge, ForgeUnsupported, resolve_forge
    base.py                # Forge ABC, ForgeUnsupported, StubForge, normalised shapes
    validate.py            # the seven validators
    errors.py              # the status-to-guidance table, forge_error(status, detail)
    identity.py            # _strip_scheme, repository_host, the segment regexes
    transport.py           # Transport protocol, CliTransport, HttpTransport
    credentials.py         # Credential protocol, BrokeredCredential, StaticFileCredential
    registry.py            # AVAILABLE, build_forges(config)
    github/
      __init__.py  forge.py  translate.py  errors.py  fixtures/
    gitlab/
      __init__.py  forge.py  translate.py  fixtures/
```

The split is by *who owns the decision*. `providers/` holds everything a forge
needs to be written against; a forge package holds everything only that forge
knows. `vcs_broker.py` keeps what is true regardless of forge — the workspace
lock, the scratch tree, the bundle size ceiling, the route table — and after the
split contains no forge name at all.

Three of those shared modules are lifts of code that is already forge-neutral
and merely co-located: `validate.py` is the seven validators unchanged,
`identity.py` is `_strip_scheme` and `repository_host` unchanged, `errors.py` is
`_FORGE_ERRORS` unchanged plus the `forge_error(status, detail)` signature from
[leak 3](#the-seven-leaks). This is not a rewrite of GitHub. It is `git mv`
plus imports, and it should read that way in review.

### Registration, in two levels

The tension: the registry should not know anything about a forge, but a
self-managed GitLab is not knowable at import time — there may be zero of them
or four, and their hostnames come from configuration.

Resolved by making the class, not the registry, answer "how many of me exist":

```python
# providers/registry.py — the one shared file a new forge edits
from .github import GitHubForge
from .gitlab import GitLabForge

AVAILABLE = (GitHubForge, GitLabForge)


def build_forges(config) -> tuple[Forge, ...]:
    return tuple(f for cls in AVAILABLE for f in cls.for_config(config))
```

`for_config` is a classmethod returning zero or more instances.
`GitHubForge.for_config` returns exactly one, always, ignoring its argument.
`GitLabForge.for_config` returns one per configured host and an empty tuple when
none are configured. Adding Bitbucket is the import line and the tuple entry;
`build_forges` does not change, and neither does anything downstream of it.

`resolve_forge` then walks the built tuple by host, exactly as it walks `FORGES`
today, and falls back to `StubForge` for a known-but-unconfigured host.

### What holds the boundary

A layout is a convention, and conventions decay under deadline. Two tests turn
it into something that fails CI.

**An import-boundary test**, `ast`-parsing every module under
`agents/platform/scripts/` and asserting three rules:

1. No module outside `providers/` imports `providers.<name>` — only `providers` itself.
2. `registry.py` is the sole exception, and only for names in `AVAILABLE`.
3. A forge package imports only
   `providers.{base,validate,errors,identity,transport,credentials}`
   and the standard library. Not the broker, not another forge.

Rule 3 is the one that matters. It is what makes "Bitbucket cannot reach into
GitHub's translation" a build failure rather than a code-review preference, and
it is the reason the shared modules have to be genuinely forge-neutral: if
`errors.py` kept `_THROTTLE_MARKERS`, GitLab would be importing GitHub's
heuristics through the front door and the test would not notice.

**A forge-name guard**, which the import test cannot catch: the string `github`
must not appear in `vcs_broker.py` or in any module directly under `providers/`.
An `if host == "github.com":` needs no import. This is a grep, it is crude, and
crude is the point — it is the check that catches the special case someone adds
at 6pm.

Both belong with the existing broker tests, and both are cheap enough to run on
every change rather than in a nightly.

### The contract test, parameterised

The verb tests today are written against `GitHubForge` by name. They become one
suite parameterised over `AVAILABLE`, with each forge package supplying a
`fixtures/` directory of recorded API responses — the JSON its host actually
returns for each of the eight verbs.

That inverts the cost. Today, holding a new forge to the same assertions means
editing the shared test file. After, a new forge package ships its fixtures and
the existing suite picks it up: the same assertions about normalised shape,
about `ForgeUnsupported` for unimplemented verbs, about validators rejecting the
same inputs, run against it without anyone touching a shared test.

Fixtures rather than a live API for the usual reason — the tests run in CI with
no credential and no egress — and recorded rather than hand-written because a
hand-written fixture encodes what the author believed the API returns.

### What a forge may not do

The boundary is also a security boundary, and it is worth stating as a
prohibition because every item is something a forge package could plausibly want
to do:

| A forge may not      | Because                                                              |
| -------------------- | -------------------------------------------------------------------- |
| run a subprocess     | it declares `transport`; the broker constructs and executes it        |
| choose a scratch path | path containment is the broker's invariant and is tested there        |
| set a timeout        | a forge could set it to zero and hang the proxy                       |
| bypass the size ceiling | the bundle limit is a resource bound, not a policy a forge tunes   |

The compressed form: **a forge answers questions, it does not do things.**
`clone_url` returns a URL; it does not clone. `verbs` names what is supported;
it does not dispatch. A verb returns a request description and translates a
response; it does not make the call. Every one of those "does not" is currently
true of `GitHubForge` — the split is what keeps it true when the code lives
somewhere a reviewer of the broker will not see.

### Credentials belong to the forge

This is where the requirement bites hardest, and it is the part the current
interface gets most wrong.

Four things about a credential differ per forge, and today they are in four
different places, none of them the forge:

| Concern                     | Where it lives now                                          |
| --------------------------- | ------------------------------------------------------------- |
| whether it expires at all   | implicit — `Forge.mint(refresh, repo)` assumes it might        |
| how it is acquired          | `credential_proxy.py`, hard-coded to `refresh_github_credential` |
| how the API presents it     | inside `gh`, invisible to the broker                          |
| how `git` presents it       | a side effect of `gh auth setup-git`, per [above](#gits-credential-has-no-seam) |

The merged [`multi-forge-support.md`](https://github.com/gke-labs/kube-agents/blob/main/docs/designs/multi-forge-support.md)
§5 states the principle this design should have followed: token acquisition is
**"a strategy selected per provider, not a pipeline every forge is fitted
into."** GitHub App tokens are signature-derived and expire hourly, so Minty
exists; a GitLab group access token is a long-lived string with no minting step,
so for GitLab there is nothing to acquire. Fitting the second into the first
produces a `mint()` that does nothing and a `refresh` argument nobody uses.

**So `mint` is deleted from the interface and replaced by one member the forge
constructs and owns:**

```python
# providers/credentials.py — shared, forge-neutral
class Credential(Protocol):
    def ensure(self, repo: str) -> None: ...
    def headers(self, repo: str) -> dict[str, str]: ...
    def git_config(self, repo: str) -> tuple[tuple[str, str], ...]: ...
```

`ensure` is "make yourself current, if that means anything to you." Two
implementations cover both forges and, as far as anyone has proposed, the third:

| Strategy               | `ensure`                                  | `headers`                | `git_config`             |
| ---------------------- | ----------------------------------------- | ------------------------ | ------------------------ |
| `BrokeredCredential`   | asks the sidecar's refresh route          | none — the CLI carries it | none — the CLI installs a helper |
| `StaticFileCredential` | **nothing** — a long-lived token cannot go stale | reads the file, sends the header | the helper pin from [above](#gits-credential-has-no-seam) |

GitHub takes the first, GitLab the second. **GitLab's `ensure` is `pass`**, and
that is the point: a forge whose credential does not expire says so by choosing
a strategy that has nothing to do, rather than by inheriting a default from an
abstraction shaped around a forge that does.

`Credential` also collects the API header and the git config, which the previous
draft had as a separate `git_config` method on `Forge`. Those are the same
concern seen twice — how this forge's token is presented — and separating them
is what allowed `gh auth setup-git` to become an undeclared side effect in the
first place. One object owns both, and a reader of a forge package can see the
whole credential story without leaving the directory.

**Who does the privileged act.** A forge still may not run a subprocess. It does
not have to: `BrokeredCredential.ensure` POSTs to the sidecar's refresh route —
which `multi-forge-support.md` §5 is already generalising from
`/v1/github/refresh` to `/v1/forge/refresh` with the provider in the body — and
the executor performs the mint. Three roles, cleanly separated: **the forge
chooses the strategy, the strategy names the privileged operation, the executor
performs it.** No shared file names a forge, and nothing in a forge package can
execute anything.

The broker, meanwhile, stops mentioning credentials entirely. It does not call
`mint`; the transport calls `credential.ensure(repo)` before a request and the
git runner asks for `git_config` before an invocation. "Invoked as needed" is
the caller's timing and the strategy's decision, and neither is the broker's
business.

### The two leaks this exposes

Both are outside `vcs_broker.py`, which is why the design of record — which
locates all its leaks in that file — does not have them.

**Leak 6, the refresh path.** `credential_proxy.py` builds the broker with
`refresh=executor.refresh_github_credential`, and that method validates its
argument with `is_valid_repository`, which requires exactly `owner/name`. So a
third forge with any acquisition step cannot be added without editing that file,
and GitLab subgroups fail the validator even with no acquisition step at all.
Closed by the strategy above, plus moving repository validation to `forge.parse`
— which the broker has already run by then, and which knows its own forge's
shape. `is_valid_repository` stays where it is for its other callers.

**Leak 7, the executable allowlist.** `CommandExecutor.ALLOWED_EXECUTABLES` is
`("gcloud", "kubectl", "gh", "git")`, and `credential_proxy_client.py` carries
the same set again as `SUPPORTED_EXECUTABLES` — so it is two files. GitLab has
`glab`; Bitbucket has no CLI at all. `multi-forge-support.md` §5 makes the
allowlist derive from the configured providers, and it is right that the union
of every supported forge's binaries grants every install more than it uses. The
forge-side half is small — the CLI-backed strategy declares its binary, and the
allowlist is built from the constructed forges — but the executor-side half is a
change to how `CommandExecutor` is constructed, and it is shared work that
belongs to that design rather than this one.

Leak 7 is worth naming here anyway, because it is the second place where "adding
a forge is a new directory" is false today, and because it is the one item in
this section that this design does not close.

### Checking it against Bitbucket

The layout is only worth its cost if the third forge is cheaper than the second.
Bitbucket Cloud is the honest test, because it differs from both GitHub and
GitLab in ways this design did not anticipate:

| Bitbucket is different in    | Absorbed by                                            | Shared file changed |
| ---------------------------- | ------------------------------------------------------ | ------------------- |
| `workspace/repo` slugs, and a UUID form | `parse`, `clone_url` in its own package     | none                |
| pull requests, not MRs; comments are on an `/comments` sub-resource | `translate.py` in its own package | none  |
| app passwords / API tokens, Basic auth not a bearer header | `git_config`, and the token file it reads | none            |
| no issue tracker on many workspaces | `verbs` omitting the four issue verbs, `ForgeUnsupported` for free — **but see [below](#not-every-provider-is-a-forge)** | none |
| `values`/`page`/`size` pagination, not `Link` headers | its own translation of listings | none  |
| 401 where GitHub 404s on a private repo | `forge_error(status, detail)` with its own status extraction | none |

The last row is the interesting one, and it is the design's weakest point rather
than a success. `errors.py` holds a shared status-to-guidance table, and
Bitbucket's 401-for-hidden-private-repo means "not found or no access" where
GitHub's 401 means "credential expired". Guidance keyed only on status is
therefore not fully forge-neutral. The narrow fix is a per-forge override map
merged over the shared table, which stays inside the forge package; that is
cheap and it should be written when Bitbucket lands, not speculatively now.

### Not every provider is a forge

The Bitbucket row above says "no issue tracker" is free, absorbed by `verbs`
omitting the issue verbs. That is true and it is not the whole answer, because
a Bitbucket install usually *does* have an issue tracker — it is Jira, on a
different host, behind a different credential.

**Jira is not designed here and is not on the delivery plan.** What belongs in
this document is the one assumption it breaks, because that assumption is cheap
to avoid now and expensive to unpick later.

Everything above assumes **one repository resolves to one provider that answers
every verb**. `resolve_forge(repository)` returns a single object; the routing
key is the repository's host; `parse` returns a repository. All three are false
for an issue tracker:

| Assumption                            | Why Jira breaks it                                        |
| ------------------------------------- | ----------------------------------------------------------- |
| the routing key is the repository host | Jira's host has nothing to do with the code host            |
| one provider answers every verb        | proposals come from Bitbucket, issues from Jira, at once     |
| `parse` yields a repository            | a Jira issue is `PROJ-123` and belongs to no repository      |

So the general shape is not "a forge" but **capabilities bound to a project
context**: code hosting and proposals from one provider, issue tracking from
another, which today happen to be the same object for GitHub and GitLab.

**What this design does about it now: two things, both nearly free.**

1. **Resolution returns a binding, not a forge.** `resolve(repository)` yields a
   small object with a provider per capability group, rather than one provider.
   For GitHub and GitLab every group points at the same instance and nothing
   observable changes. Adding Jira later is then a configuration entry and a new
   package, not a change to how every caller resolves.
2. **The directory is `providers/`, not `providers/`.** "Forge" is the right word
   for GitHub, GitLab and Bitbucket and the wrong one for an issue tracker.
   Renaming a package with three implementations in it is churn nobody will
   schedule; naming it correctly before the first one lands costs a keystroke.
   `multi-forge-support.md` already says "provider" throughout, so this also
   settles a vocabulary split rather than creating one.

**What it does not do:** no Jira package, no second credential plane, no
declarative surface for "issues live over there", and no split of the protocol
into capability groups beyond what `verbs` already expresses. Those are a
design of their own, and the first install that needs one will specify it better
than speculation would.

The point of naming it here is narrow: **whoever reviews the first provider
package should know that "one host, one provider, all verbs" is a convenience of
the first three forges and not a property of the domain.**

What the table shows is that five of six differences land in the forge's own
directory with no shared edit, and the sixth needs one shared mechanism that is
one dict merge. That is the requirement holding. It is also the reason to build
the harness at step 8 rather than after: the sixth difference is exactly the
kind that gets absorbed by a special case in a shared file when there is no test
saying it may not be.

---

## The interface, revised

After the four decisions and the modularity requirement, `Forge` is:

| Member                  | Change    | What it decides                                                    |
| ----------------------- | --------- | -------------------------------------------------------------------- |
| `hosts`                 | —         | which hostnames are this forge's; also the credential allowlist      |
| `parse(url)`            | —         | the repository a URL names — and, now, the only repository validator |
| `clone_url(repo)`       | —         | the URL to clone, composed from validated segments                  |
| `capabilities(repo)`    | —         | what this install can do here, without acquisition or network       |
| `mint(refresh, repo)`   | **gone**  | replaced by `credential`; see [Credentials belong to the forge](#credentials-belong-to-the-forge) |
| `credential`            | **new**   | the acquisition strategy, the API header and the git config — one object |
| `verbs`                 | **moved** | was `_GITHUB_VERBS`, a module constant; now a class attribute       |
| the eight verbs         | signature | receive the neutral `api` above rather than a `gh`-argv callable    |
| `transport`             | **new**   | which transport the broker builds for this forge: `"cli"` or `"http"` |
| `for_config(config)`    | **new**   | how many instances of this forge this install has: 0, 1, or n       |

The interface gets one member shorter, not longer: `mint` and the `git_config`
an earlier draft proposed collapse into `credential`. `for_config` and
`credential` both exist for [Modularity](#modularity) rather than for GitLab —
the first is what lets `registry.py` stay ignorant of any particular forge, the
second is what keeps the next forge's credential out of `credential_proxy.py`.

`transport` is a declaration, not an implementation — the forge names what it
needs and the broker constructs it. That keeps the rule the earlier note was
protecting (a forge says what to call, never how to execute it) while allowing
a transport that is not a subprocess.

### The seven leaks

| Leak                                       | Closed by                                                             |
| ------------------------------------------ | ----------------------------------------------------------------------- |
| 1. `FORGES` tuple literal                  | `AVAILABLE` + `for_config`, because self-managed hosts are not knowable at import |
| 2. `VcsBroker._api` shells `gh api`        | `transport` declaration; `CliTransport` and `HttpTransport`             |
| 3. `_forge_error` parses `(HTTP 404)`      | split: status extraction is per-transport, the guidance table stays shared |
| 4. `_GITHUB_VERBS` module constant         | `Forge.verbs` class attribute                                            |
| 5. the `api` callable's own signature      | neutral `(method, path, *, params, body, raw)`                          |
| 6. `refresh_github_credential` + `is_valid_repository` | `Forge.credential` as a per-forge strategy; validation moves to `parse` |
| 7. `ALLOWED_EXECUTABLES` / `SUPPORTED_EXECUTABLES` | **not closed here** — `multi-forge-support.md` §5 owns it       |

The design of record names four leaks and locates all of them in
`vcs_broker.py`. Leak 5 is the `api` callable's signature, which that document
counts as part of leak 2 and which is worth separating because the transport can
be replaced without the signature changing. Leaks 6 and 7 are outside that file
— in `credential_proxy.py` and `credential_proxy_client.py` — and are
[the ones the modularity requirement exposes](#the-two-leaks-this-exposes).

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

## The GitLab package

Everything below lives in `providers/gitlab/` and is imported by exactly one line
outside it, in `registry.py`.

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

`GitLabForge.credential` is a `StaticFileCredential`, and all three of its
methods are decided by that one sentence:

- `ensure()` does nothing. There is no acquisition step, no minter, no KMS key
  and no policy ConfigMap.
- `headers()` reads the file at call time and returns `PRIVATE-TOKEN`.
- `git_config()` returns the credential-helper pin from
  [above](#gits-credential-has-no-seam), which reads the same file.

This is the whole GitLab credential story, and it is nine lines in
`providers/gitlab/`. Nothing outside that directory knows GitLab has a token.

**"Long-lived" is not "permanent," and the difference has to be designed for.**
GitLab requires every access token to carry an expiry; an unset one defaults to
365 days, and the ceiling is 400. So the token does not go stale between calls —
which is why `ensure()` is still right to do nothing — but it does expire once a
year, with no automatic recovery and no warning from anything in this system.

Two consequences, both small and both easy to omit:

- **Rotation is an operator action**, and it works: the administrator updates
  the Secret, the projected file changes, and the next call reads the new value
  with no restart. That is the per-call file read earning its keep.
- **A GitLab 401 needs its own guidance string.** The shared table maps 401 to
  GitHub's meaning — the credential expired and a refresh will fix it — which
  for GitLab is advice to do something no code path implements. GitLab's 401
  should say the group access token may have expired and name the Secret. This
  is the per-forge guidance override that
  [the Bitbucket check](#checking-it-against-bitbucket) predicted would be
  needed; GitLab needs it first.

One more property of the token that belongs here because it surfaces elsewhere:
**a group access token authenticates as a bot user** that GitLab creates with
it. Anything that asks "did the agent write this?" — the branch-prefix and
`agent:ignore` rules, `viewer_login`, comment attribution — resolves to that bot
on GitLab, not to a human account. It is not a problem, but it is a fact the
agent-side policy has to be told rather than infer.

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
- **OAuth-refreshed tokens.** A group access token is the only supported GitLab
  credential; GitLab's own OIDC token exchange is deferred, which
  [`multi-forge-support.md`](https://github.com/gke-labs/kube-agents/blob/main/docs/designs/multi-forge-support.md)
  §5 also decides and for the same reason. Bitbucket will revisit this — its
  tokens do come from a refresh flow — and the point of the strategy is that
  doing so is a third `Credential` implementation in `providers/bitbucket/`, not a
  change to GitLab's or GitHub's.

## Delivery

**Three pull requests, one per forge, and the first one has no forge in its
title.** This follows the merged design's constraint —
[`multi-forge-support.md`](https://github.com/gke-labs/kube-agents/blob/main/docs/designs/multi-forge-support.md)
§9, *"no-behaviour-change before behaviour change"* and *"everything provable on
GitHub, before anything that needs GitLab"* — and it is also what makes the
modularity claim falsifiable rather than aspirational.

### Sequencing against work already in flight

There are two forge abstractions being built at once. This one is broker-side.
The other is `forge.py`, agent-side, which `multi-forge-support.md` §4 widens
and migrates four consumers onto, and which has a draft stack behind it
(#1159, #1161, #1234, #1241). Both have a provider protocol, a GitHub
implementation, a host resolver, a repository parser, an error taxonomy and a
`(HTTP nnn)` regex. **The decision is that there is one implementation, not
two.**

The sequencing constraint is not that stack — it is a draft, and a plan that
waits on a draft has no trigger. It is this:

> **`vcs_broker.py` is not on `main`.** It exists only on the abstraction
> branch. The duplication therefore does not exist yet, and comes into being
> only if that branch merges as-is.

So the strategy depends on the *merged* design and not on anyone's draft code:

- **PR 1 builds `providers/` against `multi-forge-support.md`**, which is merged
  and is the contract. Nothing in it needs a draft to land.
- **The abstraction branch must not merge ahead of PR 1.** That is the one hard
  ordering rule here. Merging it first puts a second `GitHubForge` on `main`,
  and PR 1 becomes an argument about deleting someone's merged code instead of
  a refactor.
- **If the draft stack lands first**, PR 1 folds `forge.py` in and the four
  consumer migrations come along for free — they are needed under any layout and
  are worth having regardless of who writes them.
- **If it never lands**, PR 1 is the only implementation, and the consumer
  migration happens against `providers/` whenever someone picks it up.

Either branch of that is fine, which is the property being bought. What is *not*
fine is a third state where both merge independently.

**Best of both, if the stack does land.** These are the pieces worth taking from
each side rather than reconciling by seniority:

| From `forge.py`                                       | From `vcs_broker.py`                                   |
| ----------------------------------------------------- | -------------------------------------------------------- |
| `RepoRef` as a value type, not an `owner/name` string | `ForgeUnsupported` → 501, and `verbs`                    |
| typed `PullRequest`/`Comment`/`Commit`/`Issue`        | `capabilities(repo)` answered with no token and no network |
| the executable-allowlist derivation (leak 7)          | the status-to-guidance table                              |
| the wider verb surface the skills actually need       | the seven validators                                      |
| agent policy kept above the provider                  | `clone_url` composed from validated segments only         |
|                                                       | the `Credential` strategy, and this package layout        |

The two halves need each other more than either reads alone: a union protocol is
roughly thirty methods, which is unimplementable without `verbs` and
`ForgeUnsupported` to make partial support a first-class answer — and the eight
broker verbs are too narrow to serve the skills. Neither is the superset.

### PR 1 — the abstraction, with GitHub behind it

No GitLab. Every step verifiable against the running GitHub install by showing
existing behaviour unchanged.

| Step | Change                                                                    | Tested by                                       |
| ---- | ------------------------------------------------------------------------- | ------------------------------------------------- |
| 1    | `Forge.verbs`; `_GITHUB_VERBS` deleted                                    | existing `test_vcs_broker.py`, unchanged behaviour |
| 2    | neutral `api` signature; `transport` declaration; `CliTransport`; port the eight GitHub verbs | existing tests, unchanged behaviour |
| 3    | `forge_error(status, detail)` split; `_THROTTLE_MARKERS` onto GitHub      | existing error tests, unchanged behaviour        |
| 4    | `Credential` protocol; `BrokeredCredential`; `mint` deleted; `git_config` on the credential | new test that the config reaches the git invocation |
| 5    | generic refresh in `credential_proxy.py`; validation moves to `parse`     | existing refresh tests, plus a nested-namespace case |
| 6    | **the package split** — `providers/` and `providers/github/`, imports only      | existing tests, unchanged behaviour               |
| 7    | the import-boundary test and the forge-name guard                         | they are the test                                 |
| 8    | contract harness parameterised over `AVAILABLE`; GitHub fixtures recorded | the GitHub suite, passing through the new harness  |
| 9    | `AVAILABLE` + `for_config`; `FORGES`/`HOSTS` from `build_forges`; stubs retained | new registry tests                          |
| 10   | `resolve` returns a per-capability binding, not a bare forge              | existing tests; see [Not every provider is a forge](#not-every-provider-is-a-forge) |

**Exit criteria — falsifiable, and worth putting in the PR description:**

- `grep -ci github agents/platform/scripts/vcs_broker.py` returns 0, and the
  same for every module directly under `providers/`.
- The pre-existing broker test suite passes with no assertions edited.
- `credential_proxy.py` contains no forge name on the VCS path.
- The import-boundary test passes, and fails if you add
  `from providers.github import …` to the broker.
- There is exactly one GitHub implementation in the tree.

The fourth matters most. Anyone can produce the directory layout; the test is
what says it will still be the layout in six months. The fifth is the one this
whole sequence exists to protect.

### PR 2 — GitLab

| Step | Change                                                                    | Tested by                                       |
| ---- | ------------------------------------------------------------------------- | ------------------------------------------------- |
| 11   | `HttpTransport` — timeout, size cap, redaction, status mapping            | unit tests against a local stub server           |
| 12   | `providers/gitlab/` — identity, `StaticFileCredential`, translation, errors  | the PR-1 harness, with GitLab fixtures           |
| 13   | GitLab's 401 guidance override (the token expiry case)                    | error tests                                      |
| 14   | one line in `registry.py`                                                 | registry tests                                   |
| 15   | operator: render the GitLab config, project the Secret, widen egress      | operator tests                                   |
| 16   | live validation                                                           | see below                                        |

`HttpTransport` is here rather than in PR 1 deliberately: PR 1 declares the
`transport` seam and implements only the one GitHub uses. A transport with no
consumer is a guess about what the second forge will need, and the whole point
of the sequence is to stop guessing.

### PR 3 — Bitbucket

Not designed here. What belongs in this document is the **measure**: PR 3 should
touch `providers/bitbucket/`, one line of `registry.py`, and the operator's
configuration — and nothing else. Every shared file it turns out to need is a
place the seam was in the wrong spot, and
[the Bitbucket check](#checking-it-against-bitbucket) already predicts one such
place, the shared error-guidance table.

If PR 3's diff outside its own directory is more than the registry line and the
operator config, PR 1 did not succeed. That is the honest test, and it arrives
too late to change PR 1 — which is the argument for the import-boundary test
being in PR 1 rather than waiting for a third forge to prove the point.

### Where this gets validated

No environment here has a GitLab. The endpoint-level claims marked
*live-verify* above cannot be closed without one, and neither can step 16. **PR
1 needs none of it**, which is most of the reason the sequence is shaped this
way: the abstraction is not held hostage to an environment question.

Three options, and the middle one is the recommendation:

| Option                                    | Footprint                        | What it does not cover                     |
| ----------------------------------------- | -------------------------------- | ------------------------------------------ |
| a gitlab.com project under a throwaway group | none                          | customer hostname, private CA, egress rule |
| **omnibus GitLab CE container** (`gitlab/gitlab-ce`) | one pod, ~8 GB, one PVC | nothing this design needs                 |
| the GitLab Helm chart                     | ≥8 vCPU / 30 GB cluster          | nothing — and it costs the most            |

The omnibus image is the Linux package in a container: PostgreSQL, Redis,
Sidekiq, Gitaly and NGINX all inside one pod, configured through
`GITLAB_OMNIBUS_CONFIG` and three volumes. That matters because the Helm chart
**removed its bundled PostgreSQL, Redis and MinIO in GitLab 19.0** — the chart
now expects those to be supplied, which turns "stand up a test GitLab" into
"stand up a test GitLab and three datastores."

One pod is enough to exercise everything gitlab.com cannot: an `external_url`
that appears in no shipped literal, a self-signed or private CA, and the egress
path for a host the operator has to render. Everything this design needs from
GitLab is Free-tier — merge requests, issues, notes, API v4, and group access
tokens, which on self-managed are available with any licence.

Recommendation: an omnibus GitLab CE container in the development cluster for
steps 12–15. If standing infrastructure is the blocker, the same image runs
ephemerally in a CI job for the API-shape and credential tests, and the
long-lived instance is deferred to whenever the hostname, CA and egress work
lands. gitlab.com is not on the path at all — it costs nothing but it also
proves the least. This is the same question
[`multi-forge-support.md`](https://github.com/gke-labs/kube-agents/blob/main/docs/designs/multi-forge-support.md)
§10 asks, and it should be answered once for both.

## Open questions

1. ~~**Whether `providers/` and `forge.py` become one thing.**~~ **Decided:
   one implementation.** See
   [Sequencing against work already in flight](#sequencing-against-work-already-in-flight)
   for what that means for the draft stack and for the abstraction branch. What
   remains open is only the coordination — who rebases what, and when — which is
   tracked on issue #1154 rather than settled here.
2. **Where the forge configuration is declared.** This design states what the
   broker must receive; `multi-forge-support.md` §6 owns the CR surface —
   `spec.integration.git` with a provider, a host and a repository. The two have
   to agree before step 15, and the Go half of that surface is that design's
   step 2. #1159 is the draft that does it, so this is a dependency on work that
   may or may not land — if it does not, PR 2 carries the CRD change itself.
3. **Whether `allowed_paths` is this design's field or the shared surface's.**
   GitHub gets the same boundary from Minty's policy ConfigMap.
   `multi-forge-support.md` §10 asks this twice — as "can one field name the
   token's scope boundary on both forges" and as "does the Minty policy
   ConfigMap have an analogue" — and it should be answered there rather than
   here. `allowed_paths` as written is the local fallback if it is not.
4. **Whether gitlab.com and self-managed GitLab are one forge or two.**
   `multi-forge-support.md` §10 raises it, warning that a class branching on
   "is this gitlab.com" repeats the Bitbucket mistake in miniature. The
   `for_config` design here gives a partial answer — n instances of one class,
   each with its own host and credential, no branching — but it does not settle
   whether the token model differs enough to want two classes.
5. **Whether `GitHubForge` should move onto `HttpTransport` afterwards.** Not
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
