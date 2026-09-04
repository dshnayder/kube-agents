# Version control across forges

> **Status:** design of record; **nothing below is on `main` yet.** Every path
> this document names — `vcs_broker.py`, `providers/`, the `version-control`
> skill, the `/v1/vcs/*` routes, `/opt/vcs` in the sandbox image — is one the
> implementation creates. The design is measured rather than only argued: see
> [The experiment](#the-experiment). It lands in the sequence
> [`gitlab-forge.md`](gitlab-forge.md#delivery) sets out — the abstraction with
> GitHub behind it, then GitLab, then Bitbucket — and it depends on the shell
> sandbox, whose design is in review on #737 and is likewise not upstream.

## Summary

Prospective customers expect their agent to work against the version-control
system they already use. GitHub is not it for many of them, and two more systems
are needed in the short term: **GitLab and Bitbucket**. Today every repository
path in this codebase is GitHub — the CLI it shells, the JSON it parses, the
credential it mints, the noun it uses for a change under review.

So version control and issue tracking become an abstraction with a modular
per-forge layer behind it. The sandbox speaks forge-neutral verbs; the broker
decides which forge a repository belongs to and translates. Adding a fourth
system means writing that system's module, not reworking the layer.

Two things make the abstraction smaller than it sounds. All three target systems
are **forges of git** — the version-control system underneath is the same one,
and only the collaboration layer on top differs. And within that, only the
_remote_ operations actually differ: cloning against a credential, opening a
change proposal, listing issues. Local operations — `log`, `annotate`, `show`,
`diff`, file modes — are git in every case, so they stay native, run in the
sandbox on a working copy with no origin and no credential, and go through no
abstraction at all.

The design was measured against the two repository-access designs this
repository already has, on the same probes. It came out at least as good as
both, and cheaper to run than either. Details in
[The experiment](#the-experiment).

| Layer                      | Where it goes                                                                                                                                                |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| The sandbox client         | `agents/platform/skills/version-control/`                                                                                                                    |
| The broker                 | `agents/platform/scripts/vcs_broker.py`                                                                                                                      |
| The per-provider packages  | `agents/platform/scripts/providers/`, one directory per forge — see [Modularity](#modularity)                                                                |
| Whether an install arms it | `spec.harness.experimental.shellSandbox.versionControl`, carried to the broker as `CREDENTIAL_PROXY_VCS`                                                     |
| The local git              | `/opt/vcs/libexec/git` in the sandbox image, put ahead of the credential shim by `deploy/sandbox/entrypoint.sh`                                              |
| The experiment             | [`experiments/git-access-abc/`](https://github.com/dshnayder/kube-agents/tree/experiment/git-access-abc/experiments/git-access-abc) (fork branch, see below) |

**Owns:** the seam between the sandbox and the credentialed broker — the verb
vocabulary, the bundle transport, the `/v1/vcs/*` routes, the error contract, and
the provider interface those verbs are served through.
[`multi-forge-support.md`](multi-forge-support.md) owns the half above the seam:
repository identity, the declarative CR surface, the credential plane and the
consumer migration. The two describe **one** provider implementation, not two —
see [One implementation, not two](#one-implementation-not-two).
[`gitlab-forge.md`](gitlab-forge.md) fills the interface in with a second forge
and owns the package layout and the delivery sequence.

The experiment that measured this is corpus fixtures, probe definitions, per-run
worker logs and raw scorer output. It is kept out of this repository so the
shipped tree carries only design and code, and lives on the
[`experiment/git-access-abc`](https://github.com/dshnayder/kube-agents/tree/experiment/git-access-abc/experiments/git-access-abc)
branch of the fork — the same arrangement
[`docs/designs/memory.md`](memory.md) uses.

## How to read this document

Each section goes a level deeper than the one before it, so a human reader can
stop as soon as they have what they came for. An agent should read all of it.

| Section                                           | What it gives you                                                                              |
| ------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| [Why](#why)                                       | the customer requirement, and the shape of the answer — stop here if that is what you came for |
| [The concepts](#the-concepts)                     | what the three systems call things, and which words the verbs use                              |
| [Modularity](#modularity)                         | what a forge package supplies, where the packages go, and what keeps the boundary              |
| [The design](#the-design)                         | the transport, the two gits, the routes, the error contract                                    |
| [The experiment](#the-experiment)                 | how this was measured against the two existing designs, and the results                        |
| [What this does not fix](#what-this-does-not-fix) | the limits that remain once this lands                                                         |
| [Related](#related)                               | the designs and pull requests this sits next to                                                |

---

## Why

### What customers are asking for

An agent that manages infrastructure has to read and change the repository that
describes it. Prospective customers do not all keep that repository on GitHub,
and an agent that only speaks GitHub is an agent they cannot evaluate. Two
systems are required in the short term — **GitLab** and **Bitbucket** — and the
list is not closed; self-hosted GitLab and Gerrit come up often enough that a
design which handles three by enumeration and a fourth by rewrite is the wrong
design.

This is the reason the work exists. It is not a response to a deficiency in how
repositories are reached today: the mechanisms this repository ships work, and
one of them is the baseline the new one had to match.

### Why one abstraction rather than three integrations

The alternative is to add GitLab and Bitbucket the way GitHub was added — each
one threaded through the skills, the scripts, the credential minter and the
governance procedures that name a forge. That multiplies by the number of
forges in every one of those places, and each new one has to be added to all of
them again.

An abstraction pays for itself the moment there is a second forge, provided the
seam is in the right place. Putting it between the sandbox and the credentialed
broker is what makes the rest fall out: the sandbox has no forge knowledge at
all, so nothing in a skill, a prompt or an agent's habits has to change when an
install points at GitLab.

### What is abstracted, and what stays native

All three target systems are forges of git. That is the fact the design leans
on hardest, because it means the version-control system is not what varies —
the collaboration layer built on top of it is.

Split accordingly:

- **Remote operations are abstracted.** Anything that crosses the network or
  spends a credential: fetching a repository, sending revisions back, opening
  and reading change proposals, listing and commenting on issues. These genuinely
  differ per forge — different APIs, different authentication, different nouns —
  and they are the ones that need a credential the sandbox must not hold.
- **Local operations stay native.** `log`, `annotate`, `show`, `diff`,
  `status`, `grep`, file modes, walking history: these are `git`, invoked
  directly by the agent, at full fidelity, on a working copy in the sandbox. That
  working copy has **no origin and no credential**, so the native command cannot
  reach a network even if something asks it to.

The practical consequence is that the abstraction is small. It covers the verbs
that had to be covered and nothing else, and the agent keeps the tool it already
knows for the majority of the work. A model asked to find when a policy changed
runs `git log`, not a protocol verb.

The same split is what keeps a repository's own contents away from the
credential. History moves as a git bundle — objects and refs, no `.git/config`,
no hooks, no remote URL — so the credentialed side never checks out a tree that
a repository or a sandbox authored. That property is described in
[The shape](#the-shape) and it is worth having, but it is a consequence of the
transport rather than the reason for the work.

### Modular by construction

Support for each forge is one package implementing one interface: how to
recognise its repositories, what URL to clone, how to reach its API, how to
translate that API's objects into the shared concepts, and how its credential is
presented to both the API and to `git`. Adding a system means writing that
package and adding one line to a registration file. It does not mean touching
the transport, the routes, the bundle logic, the error contract or the client.

Stated so it can be checked rather than asserted: **adding a forge is a new
directory and one line in a registration file, touching no shared module and no
other forge's code.** [Modularity](#modularity) is the layout that makes that
true and the two tests that keep it true.

### How it compares to what we have

Before building on it, the abstraction was measured head to head against the two
repository-access designs already in this repository, on identical probes and
identical corpora: the existing shared-volume credential proxy, and content
passing (#962). Twenty read probes at three repository sizes, plus a four-probe
write rung.

The short version: **the abstraction is as good as the current implementation
and better on the measures taken.** It answered the most probes, took the fewest
turns, was the only design whose cost did not grow with repository size, and was
the only one that never reached past its own interface. Full method and results
in [The experiment](#the-experiment).

That is a sanity check, not the justification. The justification is the customer
requirement above. What the numbers establish is that meeting it costs nothing
in capability or speed — which is the thing that would have stopped the work.

---

## The concepts

The caller is a language model, so the verb names were chosen as a research
question rather than a naming preference: which words for these concepts is a
model most likely to already understand? Version-control concepts are stable
across systems and the spellings are not, so where the systems disagree the
neutral name is the command and the familiar one is an alias. Both always work.

### Where the names come from

The sources consulted were the systems' own command sets and reference
documentation — git, Mercurial, Subversion, Bazaar/Breezy, Jujutsu, Fossil and
Darcs — together with Eric S. Raymond's _Understanding Version-Control Systems_,
the _Version Control with Subversion_ book's terminology chapter, and
Wikipedia's _Version control_ article, whose "common terminology" section is
where the cross-system vocabulary is written down as vocabulary rather than as
one tool's manual.

For the collaboration half, which no version-control system defines, the source
is the cross-forge tooling: Launchpad's and Breezy's _merge proposal_, and
Jelmer Vernooij's `silver-platter`, which drives GitHub, GitLab and Launchpad
through one `MergeProposal` abstraction and is the closest existing answer to
this problem.

### The vocabulary

| Concept                     | Command    | Aliases    | Why this name                                                                                                                                                                                                                                                                                               |
| --------------------------- | ---------- | ---------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Per-line attribution        | `annotate` | `blame`    | Mercurial's command is `annotate`; Subversion accepts `annotate` and `ann` alongside `blame`; Breezy's is `annotate`; Jujutsu's is `jj file annotate`. Git is the outlier in leading with `blame`, and it accepts `annotate` too.                                                                           |
| Revision history            | `log`      | `history`  | `log` is consensus across git, Mercurial, Subversion and Breezy. Fossil's `timeline` is the only dissent.                                                                                                                                                                                                   |
| The tracked file set        | `files`    | `manifest` | Mercurial has both and its own documentation prefers `files`; `manifest` is a Mercurial-internal noun that means nothing elsewhere.                                                                                                                                                                         |
| Text search                 | `grep`     | `search`   | git and Mercurial both spell it `grep`, and the Unix name is the one a model has the most exposure to.                                                                                                                                                                                                      |
| Sending revisions upstream  | `publish`  | `push`     | Mercurial's phase model is where this is a concept: a _publishing repository_ is one that makes changesets public. `push` is the DVCS spelling, and here it would be a lie — this working copy has no remote to push to, and the word invites `--force` and an `origin` that do not exist.                  |
| A change offered for review | `proposal` | `pr`, `mr` | Launchpad and Breezy call it a _merge proposal_, and `silver-platter` settled on the same term for exactly this cross-forge problem. "Pull request" carries GitHub's fork-and-branch assumption, "merge request" is GitLab's, and Gerrit's unit of review is a single revision rather than a branch at all. |
| Dropping the working copy   | `discard`  | `close`    | Named for what it does. `close` implies a counterpart that was opened, and these routes hold no state — there is nothing on the credential side to release.                                                                                                                                                 |
| Work items                  | `issue`    | —          | The one noun every forge already agrees on.                                                                                                                                                                                                                                                                 |

`clone`, `commit`, `branch`, `diff`, `show` and `status` needed no decision;
they are the same word in every system that has the concept.

### Why the grammar changes between layers

One inconsistency is deliberate. The repository verbs are verb-first (`clone`,
`commit`, `publish`) and the collaboration verbs are noun-first
(`proposal create`, `issue list`). The grammar marks the layer: verb-first is
the version-control system, noun-first is the forge. A caller that notices the
difference has noticed something true about where the work happens — and about
which half varies between GitHub, GitLab and Bitbucket.

`create` takes `open` as an alias on both nouns, and that one runs the other way
round from the table above: every forge says _open a pull request_ and none of
them says _create_ one, so here the familiar word is the one a model reaches for
first and `create` is kept only because it is what the wire verb is called.

---

## Modularity

### What a new forge has to supply

`Forge` is the interface, and it is deliberately narrow. A package decides these
things and nothing else:

| Member               | What it decides                                                                             |
| -------------------- | ------------------------------------------------------------------------------------------- |
| `hosts`              | which hostnames are this forge's, which is also what the credential allowlist is built from |
| `parse(url)`         | the repository a URL names — and the only validator of that repository's shape              |
| `clone_url(repo)`    | the URL to clone, composed from validated segments rather than taken from the caller        |
| `capabilities(repo)` | what this forge can do here, answered without spending a credential or touching the network |
| `verbs`              | which of the collaboration verbs this forge serves                                          |
| `credential`         | the acquisition strategy, the API header and `git`'s config — one object, described below   |
| `transport`          | which transport the broker builds for it: a CLI, or an in-process HTTP client               |
| `for_config(config)` | how many instances of this forge an install has: zero, one, or one per configured host      |

Plus the eight collaboration verbs: `proposal_create`, `proposal_list`,
`proposal_view`, `proposal_comment` and the same four for `issue`. A forge that
cannot serve one raises `ForgeUnsupported` naming what is missing, which is an
answer the caller can report rather than a crash.

Everything else is shared. Cloning, bundling, ancestry checking, pushing, size
ceilings, scratch-directory lifecycle, the error contract and the whole client
are forge-independent and live in the broker.

Three of those members are less obvious than the rest, and each is there because
of something a single-forge design would not have had to answer.
[`gitlab-forge.md`](gitlab-forge.md#the-interface) has the full reasoning; the
short version:

- **`credential` is one object, not four decisions in four places.** Whether a
  credential can go stale, how it is acquired if so, what header the API wants,
  and what config `git` needs are one forge's answers and they belong together.
  Splitting them is what lets an acquisition step quietly authenticate `git` as a
  side effect — which is exactly what `gh auth setup-git` does, writing a global
  credential-helper entry from a script outside the forge package, so that a
  reader of the interface cannot see why `clone` works. On this interface the
  forge says so.
- **`transport` is a declaration, not an implementation.** A forge names what it
  needs and the broker constructs it, which keeps the rule that a forge says what
  to call and never how to execute it — while allowing a transport that is not a
  subprocess. The alternative is a forge CLI per forge in the container holding
  the token, each with its own auth state on disk, its own config file and its
  own CVE stream.
- **`for_config` is what lets registration stay ignorant of any particular
  forge.** A self-managed instance's hostname is known at deploy time, not at
  import time, so "how many of me does this install have" has to be the class's
  answer rather than a literal in a shared file.

A forge that is registered but not configured is a `StubForge`: it parses its own
repository specs and answers `capabilities` with the specific gap rather than a
generic refusal. That matters more than it sounds — a caller learns what is
missing before it has written a revision it cannot deliver, and an install that
has not set GitLab up gets a named refusal instead of a confusing authentication
failure.

### Where a forge's name is allowed to appear

One rule, and it is the whole of the modularity claim: **a forge's name appears
in its own package and nowhere else.** Not in the broker, not in a shared
contract module, not in another forge's package, and not in the two files outside
this design that decide what may run.

Six places would hold a forge name if nobody had decided otherwise, and each is
worth naming because each is a specific piece of the design rather than general
tidiness:

| Would name a forge                            | Does not, because                                                                                                                                              |
| --------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| the registry of which forges exist            | it names classes and asks each `for_config` how many instances this install has, so a self-managed host never reaches a shared file                            |
| the code that makes an authenticated API call | the forge declares a `transport`; the broker builds it. A CLI is one implementation of that, not the shape of the seam                                         |
| the request the verbs hand to it              | it is `(method, path, params, body, raw)` — an HTTP request, not one CLI's argv with a media type smuggled in as a header                                      |
| the set of verbs `capabilities` reports       | `verbs` is a class attribute of the forge                                                                                                                      |
| recovering an HTTP status from a failure      | the transport does it — an integer for HTTP, a parse for a CLI that prints `(HTTP 404)` into stderr — and the guidance table keyed on that status stays shared |
| the code that makes a credential current      | the forge's `credential` does; the broker never mentions credentials                                                                                           |

The rule has one acknowledged exception, and it is outside this design.
`CommandExecutor.ALLOWED_EXECUTABLES` and `credential_proxy_client.py`'s
`SUPPORTED_EXECUTABLES` each carry `("gcloud", "kubectl", "gh", "git")`, so the
union of every supported forge's binaries is granted to every install, twice.
[`multi-forge-support.md`](multi-forge-support.md) §5 owns deriving that from the
configured providers. The forge-side half is small — a CLI-backed credential
declares its binary — but the executor-side half changes how `CommandExecutor` is
constructed, which is that design's work and not this one's.

### Where the packages go

The question was whether per-forge support should be a plugin: separately
packaged, separately shipped, loaded at runtime rather than compiled in. That
would let GitLab support be delegated to a different developer without them
touching the shared file set at all.

**It should not, and the reason is where the code runs.** The broker is the
process that holds the credential. Its code arrives baked into the
credential-proxy image at `/opt/defaults/scripts`; the only thing the operator
mounts into that container is a read-only policy ConfigMap. There is no existing
mechanism to introduce Python into that process, and adding one would be adding
a path by which code the image did not ship executes next to a live token. A
runtime plugin loader in the credentialed container is a credential-exfiltration
surface, and the modularity it buys is available without it.

So: **build-time packages, one directory per provider, one explicit registration
list, no discovery.** A contributor adds `providers/gitlab/` and one line in
`providers/registry.py`. The line is a two-level registration —
`AVAILABLE = (GitHubForge, GitLabForge)` plus a `for_config` classmethod on each
class returning zero or more instances — so the registry names a class and never
learns how many of that forge an install has, or on which hosts.

What makes this more than a convention is that it is enforced. An `ast`-parsed
import-boundary test asserts that a provider package imports only the shared
contract modules and the standard library — not the broker, not another
provider — and a deliberately crude grep guard asserts that the string `github`
appears in neither `vcs_broker.py` nor any module directly under `providers/`,
because an `if host == "github.com":` needs no import to be written. Both run on
every change.
[`gitlab-forge.md`](gitlab-forge.md#modularity) has the full layout, the three
import rules, and the contract test that parameterises the existing verb
assertions over every registered provider so a new one is held to them by
shipping fixtures rather than by editing a shared test file.

The client side was never the hard part: `vcs.py` in the sandbox is forge-neutral
and holds no credential, so it needs no per-provider code at all.

### One implementation, not two

There are two provider abstractions in flight, and they are converging rather
than competing. This one is broker-side: `Forge`, eight collaboration verbs,
served to a sandbox that holds no credential.
[`multi-forge-support.md`](multi-forge-support.md) §4 owns the other, agent-side:
`forge.py`'s provider protocol, which four consumers use directly and which
carries the wider verb surface the skills need. Both have a provider protocol, a
GitHub implementation, a host resolver, a repository parser, an error taxonomy
and a `(HTTP nnn)` regex.

**There is one implementation.** Neither side is the superset, which is the
argument for converging rather than picking a winner: a union protocol is roughly
thirty methods, which is unimplementable without `verbs` and `ForgeUnsupported`
to make partial support a first-class answer — and the eight broker verbs are too
narrow to serve the skills. The pieces worth keeping from each, and the ordering
rule that keeps a second `GitHubForge` off `main`, are in
[`gitlab-forge.md`](gitlab-forge.md#sequencing-against-work-already-in-flight).

### Not every provider is a forge

Everything above assumes one repository resolves to one provider that answers
every verb. That is a convenience of GitHub, GitLab and Bitbucket rather than a
property of the domain: a Bitbucket install usually does have an issue tracker,
and it is Jira, on a different host and behind a different credential. The
routing key is then not the repository's host, one provider does not answer every
verb, and a Jira issue is `PROJ-123` and belongs to no repository at all.

**Jira is not designed here and is not on the delivery plan.** Two cheap things
are done now because they are expensive to unpick later: resolution returns a
per-capability binding rather than a bare forge — for GitHub and GitLab every
capability points at the same instance and nothing observable changes — and the
package is `providers/` rather than `forges/`, which is also the word
`multi-forge-support.md` already uses throughout.
[`gitlab-forge.md`](gitlab-forge.md#not-every-provider-is-a-forge) has the
reasoning. The point of naming it here is narrow: whoever reviews the first
provider package should know that "one host, one provider, all verbs" is an
assumption and not a fact.

---

## The design

### The shape

History moves as a bundle, in both directions, and is never checked out on the
credential side.

`clone` asks the broker for a git bundle of the repository and unpacks it in the
sandbox, into a working copy with no remote. A bundle is objects and refs. It
carries no `.git/config`, no hooks, no remote URL. So every question about the
past is answered locally, by the sandbox's own git, at full fidelity and without
a credential anywhere near it.

`commit` runs in the sandbox too, against that copy. The revision has a real
parent and a real identifier before anything leaves the container, which is why
a branch of five changes stays five revisions instead of arriving at the forge
flattened into one.

`publish` sends those revisions back up as a bundle, symmetric with `clone`. The
broker fetches the target branch into a scratch repository, unpacks the bundle
beside it, and checks four things before it pushes: that the bundle carries
exactly the branch it claims; that its tip descends from the revision `clone`
handed out; that the target's current tip is also an ancestor, so a push nobody
saw arrive is not silently discarded; and that an existing remote branch of the
same name is not being clobbered. Then it pushes the ref it fetched.

Ahead of all four it checks that the branch is not the target. Those four are
ancestry checks, and every one of them passes for a publish onto the branch the
copy was cloned from, because that is a fast-forward — which would leave the
default clone, edit, commit, publish sequence writing to the shared line of
development with nothing in the protocol objecting. `vcs.py` refuses it before
it builds the bundle so the message costs no round trip, and the broker refuses
it again with `TARGET_IS_BRANCH` rather than trusting the client that sent the
objects.

The scratch repository is never checked out. It is fetched into and pushed from,
and nothing materialises a working tree, so a `.gitattributes`, a hook, or a
`.gitmodules` among the incoming objects has nothing to act on. That is what
makes accepting caller-supplied objects at all defensible: the broker handles
them as objects, not as a repository it is standing in.

Nothing under the broker's scratch root outlives a request. Every route is one
request long, so there is no handle to leak, no tree to collide with another
caller's, and no cleanup an interrupted client can skip.

One consequence is worth naming. An agent almost never wants a whole answer: it
wants the shape of one, then one part of it in full. Here that is `log --stat`
and then `show -- <path>`, both local, both git's own narrowing, and neither of
them a route. A protocol that answers from the credential side has to grow a
verb for each such narrowing and a cap on each verb's response, and a caller
that hits the cap gets a truncated list rather than a smaller question. This
design has no cap on a history question, because a history question never
crosses the seam. The one transfer it does make is bounded, once, at `clone`.

### The proxy stops having to share the sandbox's pod

Everything above crosses as a payload rather than as a path, and that retires
the one reason the credential runtime is co-located with the shell today. The
shell-sandbox design (#737) puts the proxy in the
sandbox's pod because `credential_proxy_client.py` forwards the caller's `cwd`
and `kubeconfig` only when the endpoint is loopback; without that forwarding,
proxied `git` from another pod runs in the proxy's own empty workspace instead
of the tree the agent is working in. These routes send a bundle, so there is no
tree to point at and nothing for the proxy to be co-resident with.

Splitting the pod is worth doing on its own, because co-location is the weaker
arrangement on three counts it cannot avoid. The containers share a pod IP, so
NetworkPolicy cannot distinguish them and the sandbox's egress has to be widened
to everything the proxy needs to reach — the forge, the token broker, the
cluster's API server. `runtimeClassName` is pod-scoped too, so running the shell
under gVisor puts the proxy inside the same sentry. And the mount namespace ends
up the only boundary between the shell and the proxy's kubeconfig, gcloud
directory and federated token, which is why the two containers run as the same
uid and why every one of the proxy's volumes is one the shell must never mount.
None of those survive the split.

### No shallow clones

There is no `depth`. This is a property of the transport rather than an
omission: `git bundle create` inside a shallow repository succeeds and writes a
bundle whose boundary revisions name parents the bundle does not carry, and a
clone from it fails with `remote did not send all necessary objects`. Naming a
`branch` is the size control that works, because it makes the broker's clone
single-branch. The two ceilings — `CREDENTIAL_PROXY_MAX_CLONE_BYTES` (256 MiB
of working tree) and `CREDENTIAL_PROXY_MAX_BUNDLE_BYTES` (64 MiB of bundle, both
directions) — say so in their refusals.

### Two gits, on purpose

The sandbox image carries a real git at `/opt/vcs/libexec/git` and the
credential shim at `/opt/credential-proxy/bin/git`, and which of the two owns
the name `git` is decided at startup rather than in the image. `/opt/vcs/bin` —
whose `git` is a symlink to that binary — goes ahead of the shim when
`CREDENTIAL_PROXY_VCS=1`, and the operator sets that variable on the shell
container exactly when the CR arms the routes. `gcloud` and `kubectl` keep
resolving to the shim, because there is no credential-free equivalent of them
and nothing local for them to read.

This is the mechanism behind "local operations stay native". With the routes
armed, `git log` in the sandbox is a real git reading a real clone, not a
command forwarded to a credentialed container.

That prepend has to happen twice, because a sandbox session arrives by two
different doors. `/etc/profile.d/vcs-path.sh` covers a login shell, which is
what `kubectl exec -- bash -l` and the image's smoke test get. Hermes reaches
the sandbox over ssh, and `ssh sandbox git log` is not a login shell: its whole
environment is the `SetEnv` line `deploy/sandbox/entrypoint.sh` writes into
`/etc/ssh/sshd_config.d/`, since `sshd_config` sets `PermitUserEnvironment no`
and accepts only `LANG` and `LC_*` from the client. The first build of this
abstraction did the prepend in `profile.d` alone and shipped an install that
reported the feature armed while `git` and `gh` both still resolved to the
credential shim on the only path the agent uses. The entrypoint now decides the
PATH itself and forwards `CREDENTIAL_PROXY_VCS` through the same allowlist that
carries `CREDENTIAL_PROXY_URL`, so `profile.d` and ssh agree by construction.

A build guard fails the image when `command -v git` finds a native binary:
neither directory is on the build PATH, so the guard sees the shim. A second
guard runs beside it and proves the local binary cannot reach a
network. The four transports that dial a host — `git-remote-http` and the
`-https`, `-ftp` and `-ftps` symlinks to it — are deleted from
`/usr/lib/git-core`, and the guard fails the build if any is back and then runs
an actual `ls-remote` per scheme against an unroutable URL, requiring git's own
missing-helper message in the answer.

The message is what carries the assertion, not the exit status. `example.invalid`
resolves nowhere, so every one of those URLs fails on an image that still ships
the helpers, and a guard that checked only for failure would pass there. A
`file://` probe runs last for the converse reason: a git broken outright also
fails to reach a network, and this is what says the disarming was surgical. The
guard these replaced asked `git <helper> --help`, which git rewrites to
`git help <helper>` and execs `man`, absent in `python:3.11-slim` — so all four
probes exited 128 and it passed whatever the image contained.

`ext` and `fd` are handled differently because deleting them does nothing. Their
entries in `/usr/lib/git-core` are symlinks to `git` itself: both are builtins,
dispatched from git's own table without the filesystem being consulted. What
stops `ext::` — which would run an arbitrary command — is git's protocol
allowlist, where `protocol.ext.allow` has defaulted to `never` since 2.12, and
the guard asserts that refusal by its own message rather than attributing it to
an `rm`. `fd::` reads a descriptor the parent already opened, so it reaches
whatever the caller could reach anyway and opens nothing new.

`vcs.py` runs that binary with `GIT_CONFIG_NOSYSTEM=1`,
`GIT_CONFIG_GLOBAL=/dev/null`, `core.hooksPath` pointed at an empty directory,
`protocol.ext.allow=never`, and no `origin`. The clone's only config is the one
git just wrote, so a `.gitattributes` naming `filter.foo.clean` finds no `foo`
defined and is inert.

### Forge neutrality

Nothing crosses the seam in a forge's vocabulary. The sandbox sends a repository
spec and a verb; the broker decides which forge that is, calls it, and returns
objects in the concepts above. GitHub's JSON stops at the broker.

That decision is also the security boundary, which is why it is made there. A
caller-supplied URL determines which host a minted credential is presented to,
so `resolve_forge` matches the URL's host against an allowlist built from the
configured forges and refuses anything else outright — there is no default for a
URL with a host, because defaulting is how a token reaches a host nobody
configured. A bare `owner/name` means GitHub, which is what every skill in this
repository has always meant by it. Once the forge is chosen, the clone URL is
composed from validated path segments rather than taken from the caller: the URL
decided _which forge_, and it does not get to decide the host.

The GitHub module reaches the API through `gh api` and never through `gh pr` or
`gh issue`. Those subcommands infer the repository from a `.git/config` found
above the working directory, which is the one file this design keeps out of the
credentialed process, and they format for a human. `gh api` takes an explicit
path and returns the API's own JSON, so the module is a REST client that borrows
`gh` for authentication.

Translation is where the judgement is, and it is the part a new forge module
will spend its time on. A proposal has three states — `open`, `closed`,
`merged` — rather than GitHub's two plus a nullable `merged_at`, because closed
and merged are different outcomes on every forge and a caller should not have to
know how one of them encodes the difference. An `[bot]` suffix comes off a login
here rather than at the caller; `forge.py` records what comparing an
unnormalised one costs, which was an agent that answered its own comments
forever. And `issue list` drops the nodes carrying a `pull_request` key, because
GitHub is alone in modelling a proposal as an issue — a translation that exists
precisely because the shared concept and the forge's model disagree.

### The protocol

`/v1/vcs/*` is a separate namespace from `/v1/workspace/*` rather than more
verbs on it. They are different protocols sharing a transport: workspace routes
are handle-oriented and stateful across a session, while every vcs route stands
alone. Folding them together would put a `handle` argument on routes that have
none and invite a caller to hold one. They share the broker's `workspace_lock`,
because they share the disk.

| Verb                                 | Request                                                    | Response                                              |
| ------------------------------------ | ---------------------------------------------------------- | ----------------------------------------------------- |
| `capabilities`                       | `{repository}`                                             | `{forge, repo, proposalNoun, verbs, missing}`         |
| `clone`                              | `{repository, branch?}`                                    | `{forge, repo, branch, revision, size, bundleBase64}` |
| `publish`                            | `{repository, branch, target, baseRevision, bundleBase64}` | `{forge, repo, branch, revision}`                     |
| `proposal-create`                    | `{repository, source, target, title, body?, draft?}`       | `{proposal}`                                          |
| `proposal-list` / `issue-list`       | `{repository, state?, limit?, labels?}`                    | `{proposals\|issues, count, truncated}`               |
| `proposal-view` / `issue-view`       | `{repository, number, comments?, diff?}`                   | `{proposal\|issue, comments?, diff?}`                 |
| `proposal-comment` / `issue-comment` | `{repository, number, body}`                               | `{comment}`                                           |
| `issue-create`                       | `{repository, title, body?, labels?}`                      | `{issue}`                                             |

Refusals carry a code: 501 `FORGE_UNSUPPORTED`, 413 `CLONE_TOO_LARGE` and
`BUNDLE_TOO_LARGE`, 409 `NOT_FAST_FORWARD`, `BASE_MOVED`, `BRANCH_DIVERGED` and
`TARGET_IS_BRANCH`, 502 `GIT_FAILED`. `404 VCS_DISABLED` is what an install that
has turned `shellSandbox.versionControl` off answers.

A refusal the forge itself produced is translated rather than forwarded, and it
is written for the reader it has. That reader is a model choosing its next tool
call, so each message names the cause and then the action that follows from it,
which is the shape Anthropic's
[tool-writing guidance](https://www.anthropic.com/engineering/writing-tools-for-agents)
asks for. The distinction earns its keep where the right action differs and the
symptom does not: GitHub spends HTTP 403 on both a missing scope and a throttle,
and an agent told only that the call failed retries the one that will never
succeed and abandons the one that would have worked in ten seconds. So 401 is
`FORGE_UNAUTHENTICATED` and says to stop, a throttled 403 becomes 429
`FORGE_RATE_LIMITED` and says to wait and to prefer one wide call to many narrow
ones, an unthrottled 403 is `FORGE_FORBIDDEN` and says retrying will not change
the answer, 404 `FORGE_NOT_FOUND` says that a private repository this install
cannot see answers the same way so absence is not proven, 422 `FORGE_REJECTED`
says to fix a field rather than repeat the call, and 5xx is 503
`FORGE_UNAVAILABLE` and says to retry the same call unchanged. Everything
unrecognised is still 502 `FORGE_CALL_FAILED`.

The table above is shared, and keying it on the status alone is _nearly_
forge-neutral. The exception is real and has to be designed for: a status can
mean different things on different forges. GitLab's 401 most often means a group
access token reached its expiry after a year, which no refresh path can fix and
which wants an operator named rather than a retry; Bitbucket answers 401 for a
private repository it will not admit exists, where GitHub answers 404. So a forge
package may supply an override map merged over the shared table — a per-forge
dict of the few statuses whose guidance it disagrees with, and nothing more.
Recovering the status in the first place belongs to the transport, not to the
table: an HTTP client has it as an integer, and a CLI that prints `(HTTP 404)`
into stderr has to dig it out.

The forge's own first line rides along in `detail`, and `vcs.py` renders both.
They answer different questions — the broker's sentence says what to do, the
forge's says which field it rejected or that the branch has no commits — and
neither substitutes for the other.

Every credentialed verb makes its credential current before it spends it, on the
broker side and never in the sandbox — knowing that a particular forge's token
expires is forge knowledge, in the container that is supposed to have none of
it. It is not the broker's knowledge either. The broker does not mention
credentials at all: the transport asks the credential to make itself current
before a request, and the git runner asks it for config before an invocation.

That is one object per forge because it is one forge's answer to four questions
that are really the same question — how is this forge's token presented. GitHub's
is an App installation token that expires within the hour, so its strategy asks
the sidecar to refresh before every verb; minting is idempotent and costs one
local process, and the alternative is worse than the cost. An expired GitHub
token surfaces as `Authentication failed` from inside the broker's own clone,
which reaches the caller as a clone failure and reads like the repository is
gone, so inferring expiry from a failure means the first verb after an idle hour
fails once for a reason the caller cannot act on. GitLab's group access token has
no acquisition step at all, so its strategy does nothing — and says so by being a
strategy with nothing to do rather than by inheriting a no-op from a default
shaped around a forge that does.

A forge may not run a subprocess, and it does not have to: **the forge chooses
the strategy, the strategy names the privileged operation, the executor performs
it.** So no shared file names a forge, and nothing inside a forge package can
execute anything.

### Replacing `gh`

A forge CLI on PATH is the route out of a forge-neutral abstraction. Shipping
`gh` and naming it in skills binds an install to GitHub regardless of what the
abstraction says, so the collaboration verbs exist to make removing it possible:
`vcs.py issue list --state open --labels bug` replaces `gh issue list`, and
`vcs.py proposal create` replaces `gh pr create`.

In the sandbox that removal is literal. When the CR arms
`shellSandbox.versionControl` the entrypoint deletes
`/opt/credential-proxy/bin/gh`, and nothing else in the image supplies one, so
`gh` resolves nowhere in either session type. The smoke test asserts that by
absence rather than by which path wins, because a check on PATH order would pass
against a build that merely shadowed the name.

The measurement is what settled that it had to happen at all. With a working
`gh` on PATH the agent left the abstraction whenever a question got awkward, and
it did so without reporting that it had: of 60 read probes, 8 were answered
through the credential shim with no call to `vcs.py`, and 4 issued a
credentialed network clone through it. It is not that the verbs could not
answer — the same probes on the same skill were answered on the verbs once there
was no CLI to reach for. An abstraction whose bypass is on PATH under the
obvious name is one the model will take.

Removal replaced a refusing stub, and the second measurement is why. The first
build shipped `/opt/vcs/bin/gh` as a script that refused and named the verb to
use instead, on the theory that a named gap is an answer a caller can act on
while `command not found` reads to a model as a broken image to route around —
the same argument `StubForge` makes about an unconfigured forge. The write rung was then run
in a fully sealed configuration, no `gh` under any path, and the theory did not
reproduce: the agent never attempted a `gh` call, emitted no not-found across
four probes, stayed on the verbs throughout, and finished faster and in fewer
turns than the same rung with the stub in place. The skill already tells it that
no forge CLI is needed or available, and that turned out to be enough guidance
without a binary to carry it.

What this does not remove is the dependency elsewhere. `gh` stays on the
broker's executable allowlist, because the GitHub module uses `gh api` as an
authenticated HTTP client, and these still name it from outside the sandbox: the
seven governance SOPs under `agents/platform/governance/`, `forge.py`, the
`fleet-audit`, `github-issue-resolver`, `pr-conversation` and `submit-suggestion`
skills. Each is a port, not a rewrite. `forge.py` is the load-bearing one: it is
already a provider seam of its own, it is what the four skills call, and
[One implementation, not two](#one-implementation-not-two) is the decision that
it converges with `providers/` rather than becoming a second copy of it. Until
these are ported they do not work in a sandbox, and each is a place a second
forge would otherwise have to be added by hand.

A sanctioned `raw` verb — a forge-native method and path, passed through the
broker and logged — was the obvious way to keep an agent that needs something
unmodelled inside the boundary rather than out on the allowlist. It is not here,
and the measurement is why. Across the read rungs the agent on these verbs made
one `gh api` call, on a probe it had already answered from a commit message, and
it made that call with a working `gh` on PATH and the content-passing routes
still serving beside it. The escape hatch is a solution to a demand that did not
appear. Adding it would also put a forge-shaped hole in a forge-neutral protocol
and give a model a documented reason to stop at the first verb that does not
quite fit. If a real gap turns up, the verb list is where it gets answered.

---

## The experiment

Three ways of getting a sandboxed agent to a repository were run against the
same probes, the same corpora and the same model, to establish that the
abstraction costs nothing relative to what already exists. Everything below —
corpora, probe definitions, harness, per-probe worker logs, raw scores — is on
the fork at
[`experiments/git-access-abc/`](https://github.com/dshnayder/kube-agents/tree/experiment/git-access-abc/experiments/git-access-abc).

### What was compared

| Arm | Access design                                                                                                    |
| --- | ---------------------------------------------------------------------------------------------------------------- |
| A   | The shared volume this repository ships: `git` and `gh` in the sandbox are shims into the credentialed container |
| B   | Content passing (#962): `{path, bytes}` payloads over `/v1/workspace/*`, the broker owns the tree                |
| C   | This design: forge-neutral verbs over `/v1/vcs/*`, history as a bundle, native git locally                       |

Twenty read probes at three repository sizes — 200, 3,000 and 10,000 files — and
a four-probe write rung. The read probes cover contested facts across revisions,
history questions, per-line attribution, file modes, and negative controls where
the correct answer is that the repository does not say. The write probes open a
change proposal, add a file with a specific mode, revise an existing proposal in
response to a seeded review comment, and one adversarial probe whose corpus
instructs the reader to install a git clean filter and a pre-commit hook — it
passes only if nothing executes.

Each arm ran the write rung against a repository that arm had never seen, since
the first arm to open a proposal turns "open a pull request" into "notice one
exists" for everyone after it.

Arm C's write rung was run **sealed**: content-passing routes disabled and no
`gh` binary under any path, so the numbers describe an install that shipped only
this design rather than one where other doors happened to be open.

### Results

| arm | rung  | answered | stayed on its own route | left for `gh api` | median s | median turns |
| --- | ----- | -------- | ----------------------- | ----------------- | -------- | ------------ |
| A   | 200   | 19/20    | 17/20                   | 0/20              | 195.8    | 4.5          |
| A   | 3000  | 19/20    | 18/20                   | 0/20              | 227.1    | 5.0          |
| A   | 10000 | 19/20    | 20/20                   | 0/20              | 313.9    | 7.0          |
| A   | write | 4/4      | 4/4                     | 3/4               | 192.8    | 4.0          |
| B   | 200   | 18/20    | 20/20                   | 4/20              | 296.3    | 6.5          |
| B   | 3000  | 19/20    | 19/20                   | 4/20              | 286.3    | 6.5          |
| B   | 10000 | 18/20    | 20/20                   | 4/20              | 317.7    | 7.0          |
| B   | write | 4/4      | 1/4                     | 4/4               | 621.9    | 11.0         |
| C   | 200   | 19/20    | 18/20                   | 0/20              | 171.4    | 4.0          |
| C   | 3000  | 19/20    | 19/20                   | 0/20              | 215.0    | 5.0          |
| C   | 10000 | 20/20    | 20/20                   | 0/20              | 216.3    | 5.0          |
| C   | write | 4/4      | 4/4                     | 0/4               | 455.5    | 9.0          |

Every arm passed every write probe, including the adversarial one — no arm
executed anything the repository supplied. Capability is not what separates
them.

Four things the numbers say:

**Cost does not grow with repository size.** Arm C is the cheapest arm at every
rung and the only one that is flat from 200 to 10,000 files: 4.0 → 5.0 → 5.0
median turns, against arm A's 4.5 → 7.0. The bundle is why — one crossing of the
seam hands over the history and everything after it is local, so a bigger
repository does not mean more round trips. Arm C at 10,000 files costs fewer
turns than arm A at 3,000.

**Answered rate is at least as good.** 58 of 60 read probes against arm A's 57
and arm B's 55, and arm C is the only arm that answered all 20 at the largest
rung.

**The interface carries the work.** Across 60 read probes and the sealed write
rung, arm C made zero calls to a forge API — not because it could not, on the
read rungs, but because the verbs answered. Per read rung the route counts are
43/43/32 `vcs` calls against 29/27/30 local `git` calls: roughly one to one,
which is the design's own claim about where the split falls.

**The repository is left in better shape.** On the write rung arms A and B each
opened a duplicate proposal against the default branch from the same head
branch and closed it within a minute. Arm C opened exactly two proposals, both
against the correct base, and revised the first in place when asked.

Where arm C is not ahead: **writing costs more turns than arm A** — 9.0 against
4.0. That is largely structural rather than comprehension. A proposal in arm A
is `git push` followed by `gh pr create`, two commands the model has seen more
often than almost anything else; the same proposal here is clone, edit, publish,
`proposal create`. Sealing improved it substantially — 9.0 turns and 455.5s
against 12.0 and 653.0s unsealed — and the remaining gap is the price of the
indirection.

### What this does not show

- One run per probe per arm. A one-probe difference is noise; only whole-class
  differences are load-bearing.
- The read rungs were run unsealed for arm C — content-passing routes serving
  and a `gh` on PATH. Those runs made zero calls on either, so sealing removes
  doors they never opened, but they were not re-run to prove it.
- The write rung ran on a later broker build than the read rungs.
- The adversarial probe is one injection corpus. It shows those two techniques
  did not fire, not that the class is closed.
- Each arm's skill text was written by the same hand, which is not a neutral
  position from which to write a competing arm's instructions.

### One defect the experiment found

The write rung is what surfaced that `execute_forge_cli` omitted its
`containment_root` argument, so all eight collaboration verbs raised `ValueError`
before the forge call launched — on every install, since the routes shipped.
`clone` and `publish` were unaffected, which is why it survived: the verbs that
move code worked and the verbs that talk about it did not. The handler logged
only the exception type, so the outage reached the agent as
`vcs proposal-list error: ValueError`. Fixed, logged with the redacted message,
and covered by a regression test that fails without the fix.

It belongs in this document because it is the honest cost of the indirection:
an abstraction has surface that a shim does not, and this one shipped a quarter
of its verbs dead. The answer is tests and error messages, and both were added.

---

## What this does not fix

The credential proxy authenticates no caller. Anything that can reach loopback
in the sandbox can drive these verbs. That is the same boundary #913 has and the
same one the shell-sandbox design (#737) describes; what makes it a boundary at
all is that the sandbox has no other credential path, not that the socket is
trusted.

`clone` pulls a whole branch's history, which is the wrong shape for a one-off
read of a large upstream repository, and there is no shallow option to make it
cheaper.

`publish` proves ancestry, not authorship. The revisions in the bundle carry
whatever author the sandbox's git wrote, and the broker does not sign or rewrite
them.

Nothing here defends against prompt injection, and these verbs are a good
delivery vehicle for it. A repository is untrusted text — file contents, commit
messages, issue and proposal bodies, review comments — and `clone`, `log`,
`issue view` and `proposal view` all exist to put that text in front of a model.
What the design does buy is that the text cannot become a credential: the
credential never enters the sandbox, `raw_token`-style routes are not
agent-callable, and the sandbox has no other path to one. What it does not buy
is protection against the model being talked into an action it is allowed to
take. `publish`, `proposal create` and `issue create` are all reachable the
moment `CREDENTIAL_PROXY_VCS` is on, so an install that wants an agent to read
history without being able to write to a forge has no way to say so.
Splitting the flag so the read verbs and the write verbs arm separately is the
smallest thing that would fix it, and it is not built.

Until GitLab and Bitbucket ship, this is a forge-neutral design with one forge
in it — and an abstraction with one implementation is a hypothesis. The measure
of it is not that GitLab works but how much shared code has to change to make it
work, which is why [`gitlab-forge.md`](gitlab-forge.md#delivery) states that
measure as a falsifiable exit criterion rather than leaving it to be judged
afterwards.

Issue trackers that are not part of a forge — Jira alongside Bitbucket being the
case that will arrive first — are named in
[Not every provider is a forge](#not-every-provider-is-a-forge) and not designed.

## Related

- [`docs/designs/multi-forge-support.md`](multi-forge-support.md) — the agent-side half of the same provider: repository identity, the CR surface, the credential plane, the consumer migration
- [`docs/designs/gitlab-forge.md`](gitlab-forge.md) — the second forge, the package layout, and the delivery sequence for all three
- [`docs/credential-isolation-design.md`](../credential-isolation-design.md) — the content-passing workspace this builds beside
- [`docs/designs/gitops-workspace-leases.md`](gitops-workspace-leases.md) — the leased shared checkout this replaces for repository reads
- [`docs/designs/memory.md`](memory.md) — the document structure and experiment format this follows
- The shell-sandbox design (#737) — the sandbox, the credential proxy, and why `git` on PATH is a shim. Not upstream yet
