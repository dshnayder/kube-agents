# GitLab as a second forge

> **Status:** proposed; nothing here is implemented, and neither is the seam it
> fills in — see [`version-control-abstraction.md`](version-control-abstraction.md),
> which is likewise not on `main`. The endpoint-level claims in
> [Translation](#translation) are written from the GitLab v4 API as documented
> and are marked where a live instance has to confirm them — see
> [Where this gets validated](#where-this-gets-validated).

## Summary

[`version-control-abstraction.md`](version-control-abstraction.md) is the seam:
forge-neutral verbs served to a sandbox that holds no credential, with a
per-provider package behind them. This is the design of the GitLab package, and
of the four decisions in the shared contract that only a second forge could
settle.

GitLab is the first forge that is not GitHub, so it is the one that establishes
whether the seam is in the right place. Mostly it is. Cloning, bundling, ancestry
checking, pushing, the scratch lifecycle, the size ceilings and the whole sandbox
client need no GitLab-specific code at all, which is what the abstraction was
for. What is new is one class of roughly the size of `GitHubForge`, one
transport, and four things in the shared contract:

**The credential is one object per forge, and it covers `git` as well as the
API.** The broker authenticates to a forge twice, by two mechanisms — the API in
the collaboration verbs, and `git` itself in `clone` and `publish` — and a design
that gives the forge a seam for the first only works as long as something else
quietly handles the second. On GitHub that something is `gh auth setup-git`,
writing a global credential helper from a script outside the forge package.
GitLab has no such accident available: its token is a long-lived group access
token with no acquisition step at all, so an interface built around acquisition
gives it nowhere to say how the token is presented.
[Credentials belong to the forge](#credentials-belong-to-the-forge) is one object
covering acquisition, the API header and git's config together.

**The request the verbs hand to the transport is an HTTP request, not argv.**
`(method, path, params, body, raw)`, where `raw` names a media type. Query
parameters are a dict rather than string-formatted into the path — which is also
what gets them URL-encoded — and a CLI's `-f`/`-F` spelling lives inside the CLI
transport, which is where it belongs.

**The transport cannot be assumed to be a CLI.** GitLab's options were `glab` in
the container holding the token, or an HTTP client in the broker process. The
second wins on every count that matters, and the seam has to allow it: a forge
_declares_ a transport and the broker builds it.

**Self-managed GitLab is the same package and a different number of instances.**
The API is identical, the token is the same shape, only the hostname differs. But
a customer's `gitlab.acme.internal` is known at deploy time, not at import time,
so the registry names classes and asks each one how many instances this install
has.

**And the whole of this belongs in its own directory.** GitLab is the second
forge; Bitbucket is the third, and it should cost less than GitLab did. So the
organising requirement is not "make GitLab work" but **adding a forge is a new
directory and one line in a registration file, touching no shared file and no
other forge's code.** [Modularity](#modularity) is that layout, and the two tests
that keep it true.

| Layer                    | Where it goes                                                                                    |
| ------------------------ | ------------------------------------------------------------------------------------------------ |
| The forge packages       | `agents/platform/scripts/providers/{github,gitlab}/`                                             |
| The shared contract      | `providers/base.py`, `validate.py`, `errors.py`, `identity.py`, `transport.py`, `credentials.py` |
| Registration             | `providers/registry.py` — the one shared file a new forge edits                                  |
| The broker               | `vcs_broker.py`, with no forge name left in it                                                   |
| Its credential           | a `Credential` strategy the forge owns, over a projected Secret                                  |
| Which hosts are GitLab's | operator configuration, rendered into the broker's environment                                   |
| The sandbox client       | unchanged — `vcs.py` needs no GitLab code                                                        |

## How to read this document

Each section goes a level deeper than the one before it. A human reader can
stop as soon as they have what they came for; an agent should read all of it.

| Section                                                            | What it gives you                                                                      |
| ------------------------------------------------------------------ | -------------------------------------------------------------------------------------- |
| [Why](#why)                                                        | what GitLab costs, and where the intuition about it is wrong — stop here if that is it |
| [What GitLab settles](#what-gitlab-settles-in-the-shared-contract) | the four decisions that touch shared code                                              |
| [Modularity](#modularity)                                          | the package layout, and what makes the boundary hold                                   |
| [The interface](#the-interface)                                    | the seam after those decisions                                                         |
| [The GitLab package](#the-gitlab-package)                          | identity, credentials, translation, errors                                             |
| [What is not built](#what-is-not-built)                            | the limits this ships with, deliberately                                               |
| [Delivery](#delivery)                                              | the three PRs, and how they survive work in flight not landing                         |
| [Open questions](#open-questions)                                  | what is still undecided                                                                |

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

Small. The forge-independent half of the broker — `clone`, `publish`, the five
publish checks, the scratch lifecycle, the size ceilings, the bundle transport,
the workspace lock — is untouched. So is the whole of the sandbox client, because
the sandbox never learns which forge it is talking to. So is the
`version-control` skill, the CRD surface, the sandbox image and the entrypoint.

One class of roughly the size of `GitHubForge`, one transport, the four contract
decisions below.

### Where the intuition about GitLab is wrong

The expectation a reader arrives with is that GitLab's credential is the hard
part, because GitHub's is: an App installation, a signing key, a mint step, a
token that expires within the hour, and a policy ConfigMap enforcing which
repositories it may be spent on. GitLab has none of that, and it is tempting to
read the absence as a gap to be filled.

It is not a gap. A **group access token** is created once by an administrator,
scoped to a group and everything under it, and lasts up to a year. There is
nothing to acquire, nothing to sign, and nothing to refresh. GitLab's credential
work is somewhere else entirely, in two places GitHub's arrangement does not
force anyone to look at:

- **How the token reaches `git`.** GitHub's reaches `git` as a side effect of
  minting — `gh auth setup-git` writes a global credential helper — so the forge
  interface never had to carry it. GitLab needs to say it. See
  [`git`'s credential has no seam](#gits-credential-has-no-seam).
- **What narrows the token's blast radius.** Minty enforces a per-repository
  permission policy at mint time, so GitHub's token arrives already narrow. A
  group access token is narrowed once at creation and nothing narrows it further,
  so every project in the group is reachable with it and the broker has to
  enforce the boundary itself. See [The credential](#the-credential).

Both are cheap. Neither is a minter, and a plan that budgets for a minter budgets
for the wrong thing.

---

## What GitLab settles in the shared contract

Four decisions, in the order the second forge forces them.

### `git`'s credential has no seam

This is the one the interface would otherwise get wrong, and it is worth taking
first because it is invisible from GitHub.

The broker authenticates to a forge twice, by two mechanisms:

- **The API**, in the collaboration verbs, through the transport.
- **`git` itself**, in `clone` and `publish`, which run `git clone` and
  `git push` against `forge.clone_url(repo)` — a plain `https://…` URL with no
  credential in it.

The natural shape of a forge interface supplies the first and forgets the second,
because on GitHub the second happens by itself. `github_token_refresh.py` ends
with:

```python
subprocess.run(["gh", "auth", "login", "--with-token"], input=token, …)
subprocess.run(["gh", "auth", "setup-git"], …)
```

`gh auth setup-git` writes a global git config entry making `gh` a credential
helper for github.com. So authenticating `git` is an _undeclared side effect_ of
authenticating the API, through a global config file, from a script that is not
part of the forge package. A reader of the interface cannot see why `clone`
works, and a second forge inherits nothing.

**So the credential declares git's config as well as the API's.**

```python
def git_config(self, repo: str) -> tuple[tuple[str, str], ...]:
    """Config keys this forge needs on the git invocations the broker makes
    on its behalf. Applied to those invocations only. Default is none."""
    return ()
```

This sits on the credential rather than on `Forge` — see
[Credentials belong to the forge](#credentials-belong-to-the-forge) for why the
two halves are one object. GitHub's `BrokeredCredential` returns `()` and relies
on the helper `gh` installs, which is fine as behaviour and is now a thing the
interface says out loud with a docstring naming where it comes from. GitLab's
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

### The request is a request, not argv

A verb describes the call it wants; the transport makes it. So what a verb hands
across is an HTTP request:

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

This is worth stating explicitly because the obvious alternative is very
attractive and it is a trap. When the only transport is `gh api`, the cheapest
signature is `(method, path, fields: list[str])` where `fields` is that CLI's
argv — `["-f", "title=…"]` for a body, `["-H", "Accept: …"]` for a media type.
That is one CLI's command-line spelling crossing a seam whose entire premise is
that nothing crosses it in a forge's own vocabulary, and no second forge can use
it. A seam only one forge can pass through has not been shown to be a seam.

Two things fall out of the shape above rather than being separately argued.
`params` as a dict is what gets query parameters URL-encoded, where formatting
them into the path — `f"repos/{repo}/pulls?state={state}&per_page={limit}"` — does
not. And `raw` names the media type instead of smuggling it through as a header,
so a transport that is not HTTP-header-shaped can still honour it.

### The transport cannot be a CLI

Given a neutral request shape, something has to execute it. GitHub's is a
subprocess running `gh api`, which is there because `gh` is already in the image
for the App flow. GitLab's options are `glab`, or an HTTP client in the broker
process.

The rule this has to respect is that the broker owns process execution and a
forge only ever says what to call. That rule is right and it is easy to overfit
to — stated as "a forge supplies `api_command(...) -> argv`" it silently assumes
every transport is a subprocess, which GitLab is the case that breaks.
**`glab` is rejected**:

- It re-imports a problem the design already solved once. `GitHubForge` uses only
  `gh api` and never `gh pr` or `gh issue`, because those subcommands infer the
  repository from a `.git/config` — the one file this whole design exists to keep
  out of the credentialed process. `glab` has the same subcommands with the same
  inference.
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

- **The broker process makes direct outbound HTTPS**, where its other network
  I/O is in subprocesses. At the NetworkPolicy layer nothing changes — egress is
  per pod, and `git clone` already leaves that pod for the same host — but the
  egress policy needs the GitLab host, and for self-managed that host is
  customer-chosen and cannot be a literal in the repository. See
  [Open questions](#open-questions).
- **Timeouts and output caps are not inherited.** A subprocess runner enforces
  both for the CLI path. `HttpTransport` has to enforce them itself, and a test
  has to hold it to that, because "the runner did it" is exactly the kind of
  property that quietly stops being true when the transport changes.

In exchange the token never enters an argv, an environment variable, or a child
process at all. That is strictly better than `gh`, and it is worth noting that
migrating `GitHubForge` onto `HttpTransport` later becomes a small change once
the App token is available to the broker directly — not proposed here, but the
shape does not foreclose it.

### One package, many hosts

`gitlab.com` and a self-managed GitLab are **one package**. The REST API is the
same `/api/v4`, the objects are the same, the token is the same shape. What
differs is the hostname, the network path to it, and where the token came from
— configuration, not code. Two classes would be one class and a copy that
drifts.

The consequence is that the set of forges cannot be a literal, because a
customer's `gitlab.acme.internal` is not knowable at import time. So the registry
is built once at broker construction:

```python
def build_forges(config: ForgeConfig) -> tuple[Forge, ...]
```

`GitHubForge` yields exactly one instance, unconditionally. `GitLabForge` yields
one per configured host, and none when none is configured — in which case
`gitlab.com` resolves to the `StubForge`, so an install that has not set GitLab
up gets a named refusal rather than a confusing authentication failure. The host
lookup `resolve` uses is derived from this tuple rather than from a module
constant.

Where the configuration comes from is the reconciliation point with
[`multi-forge-support.md`](multi-forge-support.md), which owns the declarative
surface (`spec.integration.git`). This design does not re-decide that. It states
what the broker needs to receive, in whatever form that surface renders:

| Field          | What it is                                                                            |
| -------------- | ------------------------------------------------------------------------------------- |
| `host`         | the GitLab hostname                                                                   |
| `tokenPath`    | file the projected Secret lands at                                                    |
| `allowedPaths` | namespace prefixes this token may be spent on — see [The credential](#the-credential) |

One caveat on hostnames, inherited rather than introduced: `repository_host`
treats a first segment with no dot in it as _not a host_, so a bare `owner/name`
resolves to GitHub. An in-cluster GitLab reached as `gitlab` with no domain
would be read as a repository named `gitlab`. Configure a dotted name; the
refusal is otherwise silent and confusing.

---

## Modularity

The four decisions above are what GitLab needs. This section is what the _third_
forge needs, and it is a different question. Bitbucket should cost less than
GitLab, not the same; the way that happens is that GitLab leaves behind a shape
Bitbucket fills in rather than a precedent Bitbucket imitates.

The requirement, stated so it can be checked: **adding a forge is a new
directory and one line in a registration file. It touches no shared module and
no other forge's code.**

The failure mode this is against is not exotic. A forge, its verb list, its error
parsing and the registry all fit comfortably in one module with the broker, and
at one forge that is the right call. At two it means the second forge arrives as
an edit to the first forge's file. At three, "who broke GitHub" stops having a
one-file answer, and by then the layout is expensive to change and nobody
schedules it.

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

The split is by _who owns the decision_. `providers/` holds everything a forge
needs to be written against; a forge package holds everything only that forge
knows. `vcs_broker.py` keeps what is true regardless of forge — the workspace
lock, the scratch tree, the bundle size ceiling, the route table — and contains
no forge name at all.

Most of `providers/` is not new logic. The validators, the scheme stripping and
host resolution, and the status-to-guidance table are forge-neutral already;
what the layout does is put them somewhere a forge package can import without
importing a forge. That distinction is the whole point of the boundary test
below, and it is why `errors.py` holds the guidance table but not the throttle
heuristics that read GitHub's message text: a shared module that keeps one
forge's heuristics is a shared module the next forge inherits through the front
door.

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

`resolve` walks the built tuple by host and falls back to `StubForge` for a
known-but-unconfigured one.

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

The verb tests are one suite parameterised over `AVAILABLE`, not a file per
forge. Each forge package supplies a `fixtures/` directory of recorded API
responses — the JSON its host actually returns for each of the eight verbs — and
the suite reads them.

That inverts where the cost falls. A per-forge test file means holding a new
forge to the same assertions is a shared-test edit somebody has to remember to
make; here a new package ships its fixtures and the existing suite picks it up.
The same assertions about normalised shape, about `ForgeUnsupported` for
unimplemented verbs, about validators rejecting the same inputs, run against it
without anyone touching a shared test.

Fixtures rather than a live API for the usual reason — the tests run in CI with
no credential and no egress — and recorded rather than hand-written because a
hand-written fixture encodes what the author believed the API returns.

### What a forge may not do

The boundary is also a security boundary, and it is worth stating as a
prohibition because every item is something a forge package could plausibly want
to do:

| A forge may not         | Because                                                          |
| ----------------------- | ---------------------------------------------------------------- |
| run a subprocess        | it declares `transport`; the broker constructs and executes it   |
| choose a scratch path   | path containment is the broker's invariant and is tested there   |
| set a timeout           | a forge could set it to zero and hang the proxy                  |
| bypass the size ceiling | the bundle limit is a resource bound, not a policy a forge tunes |

The compressed form: **a forge answers questions, it does not do things.**
`clone_url` returns a URL; it does not clone. `verbs` names what is supported;
it does not dispatch. A verb returns a request description and translates a
response; it does not make the call. Holding to that is easy while there is one
forge and a reviewer sees the whole of it; the split is what keeps it true once
the code lives somewhere a reviewer of the broker will not look.

### Credentials belong to the forge

This is where the requirement bites hardest, and it is the easiest place to get
the seam wrong.

Four things about a credential differ per forge, and the natural place to put
each of them is a different one:

| Concern                   | Where it wants to go                                                      |
| ------------------------- | ------------------------------------------------------------------------- |
| whether it expires at all | nowhere — it is assumed, by whether an acquisition method exists at all   |
| how it is acquired        | the process that holds the privilege, named after the forge that needs it |
| how the API presents it   | inside whichever client makes the call                                    |
| how `git` presents it     | a side effect of acquisition, per [above](#gits-credential-has-no-seam)   |

Every one of those is defensible in isolation and the set is wrong, because they
are four views of one question — how is _this_ forge's token presented — and
scattering them is what allows the fourth to become invisible.

[`multi-forge-support.md`](multi-forge-support.md) §5 states the principle:
token acquisition is **"a strategy selected per provider, not a pipeline every
forge is fitted into."** GitHub App tokens are signature-derived and expire
hourly, so Minty exists; a GitLab group access token is a long-lived string with
no minting step, so for GitLab there is nothing to acquire. An interface built
around acquisition makes the second forge implement a method that does nothing
and receive an argument nobody uses, and still leaves it nowhere to put the parts
that are not empty.

**So one member, which the forge constructs and owns:**

```python
# providers/credentials.py — shared, forge-neutral
class Credential(Protocol):
    def ensure(self, repo: str) -> None: ...
    def headers(self, repo: str) -> dict[str, str]: ...
    def git_config(self, repo: str) -> tuple[tuple[str, str], ...]: ...
```

`ensure` is "make yourself current, if that means anything to you." Two
implementations cover both forges and, as far as anyone has proposed, the third:

| Strategy               | `ensure`                                         | `headers`                        | `git_config`                                              |
| ---------------------- | ------------------------------------------------ | -------------------------------- | --------------------------------------------------------- |
| `BrokeredCredential`   | asks the sidecar's refresh route                 | none — the CLI carries it        | none — the CLI installs a helper                          |
| `StaticFileCredential` | **nothing** — a long-lived token cannot go stale | reads the file, sends the header | the helper pin from [above](#gits-credential-has-no-seam) |

GitHub takes the first, GitLab the second. **GitLab's `ensure` is `pass`**, and
that is the point: a forge whose credential does not expire says so by choosing
a strategy that has nothing to do, rather than by inheriting a default from an
abstraction shaped around a forge that does.

Collecting the API header and the git config onto the same object as acquisition
is the load-bearing part. They are one concern seen three times — how this
forge's token is presented — and separating them is precisely what lets
`gh auth setup-git` become an undeclared side effect. One object owns all three,
and a reader of a forge package sees the whole credential story without leaving
the directory.

**Who does the privileged act.** A forge may not run a subprocess, and it does
not have to: `BrokeredCredential.ensure` POSTs to the sidecar's refresh route —
which `multi-forge-support.md` §5 generalises from `/v1/github/refresh` to
`/v1/forge/refresh` with the provider in the body — and the executor performs the
mint. Three roles, cleanly separated: **the forge chooses the strategy, the
strategy names the privileged operation, the executor performs it.** No shared
file names a forge, and nothing in a forge package can execute anything.

The broker does not mention credentials at all. The transport calls
`credential.ensure(repo)` before a request and the git runner asks for
`git_config` before an invocation. "Invoked as needed" is the caller's timing and
the strategy's decision, and neither is the broker's business.

### The one thing outside this boundary

Everything above keeps forge knowledge inside a forge package. Two files outside
this design do not, and cannot be fixed from inside it.

`CommandExecutor.ALLOWED_EXECUTABLES` is `("gcloud", "kubectl", "gh", "git")` and
`credential_proxy_client.py` carries the same set again as
`SUPPORTED_EXECUTABLES`. GitLab would add `glab` if it used one; Bitbucket has no
CLI at all. So the union of every supported forge's binaries is granted to every
install, twice over, and adding a CLI-backed forge means editing both.
[`multi-forge-support.md`](multi-forge-support.md) §5 makes that allowlist derive
from the configured providers, which is the right answer.

The forge-side half is small — a CLI-backed credential declares its binary, and
the allowlist is built from the constructed forges. The executor-side half
changes how `CommandExecutor` is constructed, which is that design's work and not
this one's. It is named here because it is the one place "adding a forge is a new
directory" is not true, and a modularity claim that quietly omits its own
exception is not worth checking.

### Checking it against Bitbucket

The layout is only worth its cost if the third forge is cheaper than the second.
Bitbucket Cloud is the honest test, because it differs from both GitHub and
GitLab in ways this design did not anticipate:

| Bitbucket is different in                                           | Absorbed by                                                                                                              | Shared file changed |
| ------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ | ------------------- |
| `workspace/repo` slugs, and a UUID form                             | `parse`, `clone_url` in its own package                                                                                  | none                |
| pull requests, not MRs; comments are on an `/comments` sub-resource | `translate.py` in its own package                                                                                        | none                |
| app passwords / API tokens, Basic auth not a bearer header          | `git_config`, and the token file it reads                                                                                | none                |
| no issue tracker on many workspaces                                 | `verbs` omitting the four issue verbs, `ForgeUnsupported` for free — **but see [below](#not-every-provider-is-a-forge)** | none                |
| `values`/`page`/`size` pagination, not `Link` headers               | its own translation of listings                                                                                          | none                |
| 401 where GitHub 404s on a private repo                             | `forge_error(status, detail)` with its own status extraction                                                             | none                |

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
a Bitbucket install usually _does_ have an issue tracker — it is Jira, on a
different host, behind a different credential.

**Jira is not designed here and is not on the delivery plan.** What belongs in
this document is the one assumption it breaks, because that assumption is cheap
to avoid now and expensive to unpick later.

Everything above assumes **one repository resolves to one provider that answers
every verb**. `resolve_forge(repository)` returns a single object; the routing
key is the repository's host; `parse` returns a repository. All three are false
for an issue tracker:

| Assumption                             | Why Jira breaks it                                       |
| -------------------------------------- | -------------------------------------------------------- |
| the routing key is the repository host | Jira's host has nothing to do with the code host         |
| one provider answers every verb        | proposals come from Bitbucket, issues from Jira, at once |
| `parse` yields a repository            | a Jira issue is `PROJ-123` and belongs to no repository  |

So the general shape is not "a forge" but **capabilities bound to a project
context**: code hosting and proposals from one provider, issue tracking from
another, which today happen to be the same object for GitHub and GitLab.

**What this design does about it now: two things, both nearly free.**

1. **Resolution returns a binding, not a forge.** `resolve(repository)` yields a
   small object with a provider per capability group, rather than one provider.
   For GitHub and GitLab every group points at the same instance and nothing
   observable changes. Adding Jira later is then a configuration entry and a new
   package, not a change to how every caller resolves.
2. **The directory is `providers/`, not `forges/`.** "Forge" is the right word
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

## The interface

After the four decisions and the modularity requirement, `Forge` is ten members:

| Member               | What it decides                                                                      |
| -------------------- | ------------------------------------------------------------------------------------ |
| `hosts`              | which hostnames are this forge's; also what the credential allowlist is built from   |
| `parse(url)`         | the repository a URL names, and the only validator of that repository's shape        |
| `clone_url(repo)`    | the URL to clone, composed from validated segments                                   |
| `capabilities(repo)` | what this install can do here, without spending a credential or touching the network |
| `verbs`              | which of the eight collaboration verbs this forge serves                             |
| `credential`         | the acquisition strategy, the API header and the git config — one object             |
| `transport`          | which transport the broker builds for it: `"cli"` or `"http"`                        |
| `for_config(config)` | how many instances of this forge this install has: 0, 1, or n                        |
| the eight verbs      | each describes a request and translates the response                                 |
| `error_overrides`    | the few statuses whose shared guidance this forge disagrees with                     |

Three of these are the modularity requirement rather than GitLab. `for_config` is
what lets `registry.py` stay ignorant of any particular forge. `credential` is
what keeps the next forge's token out of the credential proxy's own module.
`error_overrides` is what stops the first status whose meaning is forge-specific
from being absorbed as an `if` in a shared file.

`transport` is a declaration, not an implementation: the forge names what it
needs and the broker constructs it, which keeps the rule that a forge says what
to call and never how to execute it while allowing a transport that is not a
subprocess.

### Where the error contract splits

`errors.py` holds a status-to-guidance table — forge-neutral prose about what an
agent should do next — and `forge_error(status, detail)` builds a refusal from
it. Two things are deliberately _not_ in there, and both are places a
single-forge design would have put them:

- **Recovering the status.** `HttpTransport` has it as an integer. A CLI prints
  `(HTTP 404)` into stderr and something has to dig it out with a regex. That
  regex belongs to `CliTransport`, not to the error module, because it is a
  property of how the call was made rather than of what the forge answered.
- **Splitting one status by message text.** GitHub spends 403 on both a missing
  scope and a throttle, and telling them apart means matching throttle markers in
  prose. That heuristic lives on `GitHubForge`. GitLab returns 429 with
  `RateLimit-*` headers and needs none of it, and a shared module carrying
  GitHub's markers would be a shared module the next forge inherits through the
  front door.

---

## The GitLab package

Everything below lives in `providers/gitlab/` and is imported by exactly one line
outside it, in `registry.py`.

### Repository identity

GitLab namespaces nest: `group/subgroup/project`, arbitrarily deep. This is the
one place GitLab is structurally different from GitHub rather than differently
spelled, and the shared contract already allows for it: `StubForge.parse` accepts
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

| Neutral field       | GitLab source                     | Note                                                           |
| ------------------- | --------------------------------- | -------------------------------------------------------------- |
| `number`            | `iid`                             | never `id`                                                     |
| `state` (proposal)  | `state`                           | `opened`→`open`, `merged`→`merged`, `closed`/`locked`→`closed` |
| `draft`             | `draft`                           | `work_in_progress` on older instances; read `draft`, fall back |
| `author`            | `author.username`                 | no `[bot]` suffix to strip                                     |
| `source` / `target` | `source_branch` / `target_branch` | direct                                                         |
| `url`               | `web_url`                         |                                                                |
| `created`/`updated` | `created_at` / `updated_at`       | both ISO-8601, same as GitHub                                  |
| `body`              | `description`                     | GitLab's name for it                                           |
| `labels`            | `labels`                          | plain strings, not GitHub's `{name: …}` dicts                  |

Three asymmetries are worth their own note, because each one is a place the
neutral contract was shaped by GitHub and GitLab shows the shape.

**Issues are not proposals.** `GitHubForge.issue_list` filters out nodes
carrying a `pull_request` key, because on GitHub a PR _is_ an issue and the
issues endpoint returns both. Nowhere else models it that way. GitLab's
`/issues` returns issues, so `GitLabForge.issue_list` has no filter and
`issue_view` needs no "this is a merge request, read it with `proposal view`"
refusal. The neutral contract is right and GitHub is the odd one; the filter
stays where it belongs, inside `GitHubForge`.

**Comments are notes, and most notes are not comments.** GitLab's
`/merge_requests/{iid}/notes` returns the discussion _and_ system notes —
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
  `/merge_requests/{iid}/raw_diffs`. _(Live-verify: the exact path and whether
  it needs a size guard on a large MR.)_
- **Proposal creation** posts `source_branch`, `target_branch`, `title`,
  `description` to `/merge_requests`. GitLab's draft flag on creation has
  historically been a `Draft:` title prefix rather than a field; current
  instances accept neither reliably across versions. _(Live-verify: whether
  `draft` is settable at creation on the target version, and if not, whether
  `proposal_create` sets it in a second call or reports it unsupported.)_

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
  [`multi-forge-support.md`](multi-forge-support.md)
  §5 also decides and for the same reason. Bitbucket will revisit this — its
  tokens do come from a refresh flow — and the point of the strategy is that
  doing so is a third `Credential` implementation in `providers/bitbucket/`, not a
  change to GitLab's or GitHub's.

## Delivery

**Three pull requests, one per forge, and the first one has no forge in its
title.** This follows the merged design's constraint —
[`multi-forge-support.md`](multi-forge-support.md)
§9, _"no-behaviour-change before behaviour change"_ and _"everything provable on
GitHub, before anything that needs GitLab"_ — and it is also what makes the
modularity claim falsifiable rather than aspirational.

### One implementation, and what that costs the sequence

Two provider abstractions are being built at once. This one is broker-side. The
other is `forge.py`, agent-side, which `multi-forge-support.md` §4 widens and
migrates four consumers onto, and which has a draft stack behind it (#1159,
#1161, #1234, #1241). Both have a provider protocol, a GitHub implementation, a
host resolver, a repository parser, an error taxonomy and a `(HTTP nnn)` regex.
**There is one implementation, not two.**

That sounds like a dependency on the draft stack and it is deliberately not one.
A plan that waits on a draft has no trigger. What the plan rests on instead is a
fact about `main`:

> **`vcs_broker.py` is not on `main`.** The duplication does not exist yet. It
> comes into being only if a version of the abstraction merges that has a second
> `GitHubForge` in it.

So the ordering rule is about what reaches `main`, not about whose branch lands
first:

- **PR 1 is built against `multi-forge-support.md`**, which is merged and is the
  contract. Nothing in it needs a draft to land.
- **The abstraction reaches `main` in the `providers/` layout**, not in a flatter
  one restructured afterwards. This is the one hard rule. A flat version merging
  first turns PR 1 from a build into an argument about deleting merged code.
- **If the draft stack lands first**, PR 1 folds `forge.py` in and the four
  consumer migrations come along with it — they are needed under any layout and
  are worth having whoever writes them.
- **If it never lands**, PR 1 is the only implementation and the consumer
  migration happens against `providers/` whenever someone picks it up.

Either of those is fine, which is the property being bought. The state to avoid
is the third one, where both arrive independently.

**What each side contributes.** If the stack lands, these are the pieces worth
carrying across rather than reconciling by seniority:

| From `forge.py`                                       | From the broker-side design                                |
| ----------------------------------------------------- | ---------------------------------------------------------- |
| `RepoRef` as a value type, not an `owner/name` string | `ForgeUnsupported` → 501, and `verbs`                      |
| typed `PullRequest`/`Comment`/`Commit`/`Issue`        | `capabilities(repo)` answered with no token and no network |
| deriving the executable allowlist from configuration  | the status-to-guidance table                               |
| the wider verb surface the skills actually need       | the seven validators                                       |
| agent policy kept above the provider                  | `clone_url` composed from validated segments only          |
|                                                       | the `Credential` strategy, and this package layout         |

Neither is the superset, which is the argument for converging rather than
picking a winner. A union protocol is roughly thirty methods, unimplementable
without `verbs` and `ForgeUnsupported` to make partial support a first-class
answer — and the eight broker verbs are too narrow to serve the skills.

### PR 1 — the abstraction, with GitHub behind it

No GitLab. The order below is what keeps it reviewable: the shared contract
first, then the one forge that fills it in, then the tests that hold the
boundary, then registration.

| Step | Delivers                                                                                                                            | Held to it by                                                       |
| ---- | ----------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------- |
| 1    | `providers/` shared contract: `Forge` ABC with `verbs`, the validators, identity, the guidance table, `forge_error(status, detail)` | unit tests per module                                               |
| 2    | `Transport` protocol with the neutral `api` request; `CliTransport`, including status extraction                                    | transport unit tests                                                |
| 3    | `Credential` protocol; `BrokeredCredential`; `git_config` reaching the broker's git invocations                                     | a test that the config lands on the invocation and nowhere else     |
| 4    | `providers/github/` — the eight verbs, translation, its throttle heuristics, its `error_overrides`                                  | the verb suite                                                      |
| 5    | the credential-proxy wiring: a generic refresh route, repository validation via `forge.parse`                                       | refresh tests, including a nested-namespace repository              |
| 6    | the import-boundary test and the forge-name guard                                                                                   | they are the test                                                   |
| 7    | contract harness parameterised over `AVAILABLE`; GitHub fixtures recorded                                                           | the GitHub verb suite runs through it                               |
| 8    | `AVAILABLE`, `for_config`, `build_forges`; `StubForge` for registered-but-unconfigured hosts                                        | registry tests                                                      |
| 9    | `resolve` returning a per-capability binding                                                                                        | see [Not every provider is a forge](#not-every-provider-is-a-forge) |

**Exit criteria — falsifiable, and worth putting in the PR description:**

- `grep -ci github agents/platform/scripts/vcs_broker.py` returns 0, and the
  same for every module directly under `providers/`.
- `credential_proxy.py` names no forge on the VCS path.
- The import-boundary test passes, and fails if you add
  `from providers.github import …` to the broker.
- There is exactly one GitHub provider implementation in the tree.

The third matters most. Anyone can produce the directory layout; the test is
what says it will still be the layout in six months. The fourth is the one this
whole sequence exists to protect.

### PR 2 — GitLab

| Step | Delivers                                                                    | Held to it by                          |
| ---- | --------------------------------------------------------------------------- | -------------------------------------- |
| 10   | `HttpTransport` — timeout, size cap, redaction, status mapping              | unit tests against a local stub server |
| 11   | `providers/gitlab/` — identity, `StaticFileCredential`, translation, errors | the PR-1 harness, with GitLab fixtures |
| 12   | GitLab's 401 guidance override (the token-expiry case)                      | error tests                            |
| 13   | one line in `registry.py`                                                   | registry tests                         |
| 14   | operator: render the GitLab config, project the Secret, widen egress        | operator tests                         |
| 15   | live validation                                                             | see below                              |

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
_live-verify_ above cannot be closed without one, and neither can step 15. **PR
1 needs none of it**, which is most of the reason the sequence is shaped this
way: the abstraction is not held hostage to an environment question.

Three options, and the middle one is the recommendation:

| Option                                               | Footprint               | What it does not cover                     |
| ---------------------------------------------------- | ----------------------- | ------------------------------------------ |
| a gitlab.com project under a throwaway group         | none                    | customer hostname, private CA, egress rule |
| **omnibus GitLab CE container** (`gitlab/gitlab-ce`) | one pod, ~8 GB, one PVC | nothing this design needs                  |
| the GitLab Helm chart                                | ≥8 vCPU / 30 GB cluster | nothing — and it costs the most            |

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
steps 11–14. If standing infrastructure is the blocker, the same image runs
ephemerally in a CI job for the API-shape and credential tests, and the
long-lived instance is deferred to whenever the hostname, CA and egress work
lands. gitlab.com is not on the path at all — it costs nothing but it also
proves the least. This is the same question
[`multi-forge-support.md`](multi-forge-support.md)
§10 asks, and it should be answered once for both.

## Open questions

1. **How `providers/` and `forge.py` converge in practice.** That they converge
   is settled — see
   [One implementation, and what that costs the sequence](#one-implementation-and-what-that-costs-the-sequence).
   What is open is the coordination: who rebases what, and in which order, given
   that the agent-side half has a draft stack against it. Tracked on issue #1154
   rather than settled here.
2. **Where the forge configuration is declared.** This design states what the
   broker must receive; `multi-forge-support.md` §6 owns the CR surface —
   `spec.integration.git` with a provider, a host and a repository. The two have
   to agree before step 14, and the Go half of that surface is that design's
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
- [`multi-forge-support.md`](multi-forge-support.md) — the agent-side provider
  contract and the declarative surface. Unreconciled with the above over
  `forge.py`.
- [`pr-comment-conversation.md`](pr-comment-conversation.md) §3 — the seven
  provider operations `forge.py` implements today, and the three normalisations
  that already exist because of a non-GitHub forge.
- Issue #1154 — the GitLab/Bitbucket tracking issue.
