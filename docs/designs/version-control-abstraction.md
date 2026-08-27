# Version control as verbs

> **Status:** design of record for the `version-control` skill and the broker's
> `/v1/vcs/*` routes. Both are implemented behind `CREDENTIAL_PROXY_VCS`, which
> is off by default. The skill is not yet the default door to a repository:
> `inspect-repository` still is, and an install chooses between them by which
> skill it ships. The A/B/C experiment that measured the three access designs is
> [on the fork](https://github.com/dshnayder/kube-agents/tree/experiment/git-access-abc/experiments/git-access-abc).

## The problem this is the third answer to

An agent in the shell sandbox has no credential. Getting it to a repository
means one of two things, and this repository has shipped both.

The first is a checkout on a volume the credential broker also mounts. `git`
and `gh` in the sandbox are symlinks to `credential-proxy-exec`
(`deploy/sandbox/Dockerfile`), so the command runs in the broker's container
where the token is, against the tree the sandbox just edited. Everything about
git works, because it is git: `log`, `blame`, `show HEAD~3:path`, file modes,
the lot.

What it gives up is that the broker's git reads a `.git/config` the sandbox
wrote. A clean filter, a `core.hooksPath`, an `ext::` remote — any of the
sixteen known routes — is a line in a file on a shared volume, and the process
that reads it is the one holding the credential. This is not hypothetical; it
was demonstrated against a live install by pointing `filter.<name>.clean` at a
command and running `git add`. PR #955 and PR #960 both name the class.

The second answer is content passing (#962): the sandbox never has a checkout
at all. It asks the broker to `open` a repository, then `read`, `list`, `grep`,
`commit`, `push` over `/v1/workspace/*`, and every payload is `{path, bytes}`.
Nothing the repository supplied is ever interpreted beside the token, because
the broker's tree has no name the sandbox can say.

What that gives up is history. `read` and `grep` answer from one commit.
There is no `log`, no `annotate`, no earlier revision of a file, and no file
mode — git records `100644` or `100755` on a tree entry and a protocol carrying
only bytes has nowhere to put it.

The measured consequence is the reason this document exists. Given real
questions about a real repository, an agent on the content protocol answered
19 of 20 — and reached the history and file-mode answers by leaving the
protocol for `gh api`, which is on the executable allowlist, runs in the broker,
and is inspected by nothing. An access design that omits a capability does not
remove the capability. It relocates it to a route nobody designed.

## The shape

History moves as a bundle, in both directions, and is never checked out on the
credential side.

`clone` asks the broker for a git bundle of the repository and unpacks it in the
sandbox, into a working copy with no remote. A bundle is objects and refs. It
carries no `.git/config`, no hooks, no remote URL — the three things the shared
volume leaked. So every question about the past is answered locally, by the
sandbox's own git, at full fidelity and without a credential anywhere near it.

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

The scratch repository is never checked out. It is fetched into and pushed from,
and nothing materialises a working tree, so a `.gitattributes`, a hook, or a
`.gitmodules` among the incoming objects has nothing to act on. That is what
makes accepting caller-supplied objects at all defensible: the broker handles
them as objects, not as a repository it is standing in.

Nothing under the broker's scratch root outlives a request. Every route is one
request long, so there is no handle to leak, no tree to collide with another
caller's, and no cleanup an interrupted client can skip.

### No shallow clones

There is no `depth`. This is a property of the transport rather than an
omission: `git bundle create` inside a shallow repository succeeds and writes a
bundle whose boundary revisions name parents the bundle does not carry, and a
clone from it fails with `remote did not send all necessary objects`. Naming a
`branch` is the size control that works, because it makes the broker's clone
single-branch. The two ceilings — `CREDENTIAL_PROXY_MAX_CLONE_BYTES` (256 MiB
of working tree) and `CREDENTIAL_PROXY_MAX_BUNDLE_BYTES` (64 MiB of bundle,
both directions) — say so in their refusals.

### Two gits, on purpose

The sandbox image carries a real git at `/opt/vcs/libexec/git`, off every PATH.
`git` on PATH is still the credential shim and still runs in the broker; the
build guard that fails when `command -v git` finds a native binary is unchanged,
and the relocated binary satisfies it rather than being excepted from it.
`git-receive-pack`, `git-upload-pack`, `git-upload-archive` and `git-shell` are
deleted from the image, so the local git can read and write a repository and
cannot speak the wire protocol to anything.

`vcs.py` runs that binary with `GIT_CONFIG_NOSYSTEM=1`,
`GIT_CONFIG_GLOBAL=/dev/null`, `core.hooksPath` pointed at an empty directory,
`protocol.ext.allow=never`, and no `origin`. The clone's only config is the one
git just wrote, so a `.gitattributes` naming `filter.foo.clean` finds no `foo`
defined and is inert — the same argument `content_workspace` makes about the
broker's own trees, made here about a tree that is now allowed to exist.

## The vocabulary, and where it comes from

The caller is a language model, so the verb names were chosen as a research
question rather than a naming preference: which words for these concepts is a
model most likely to already understand? Version-control concepts are stable
across systems and the spellings are not, so where the systems disagree the
neutral name is the command and the familiar one is an alias. Both always work.

The sources consulted were the systems' own command sets and reference
documentation — git, Mercurial, Subversion, Bazaar/Breezy, Jujutsu, Fossil and
Darcs — together with Eric S. Raymond's _Understanding Version-Control Systems_,
the _Version Control with Subversion_ book's terminology chapter, and
Wikipedia's _Version control_ article, whose "common terminology" section is
where the cross-system vocabulary is written down as vocabulary rather than as
one tool's manual. For the collaboration half, which no version-control system
defines, the source is the cross-forge tooling: Launchpad's and Breezy's
_merge proposal_, and Jelmer Vernooij's `silver-platter`, which drives GitHub,
GitLab and Launchpad through one `MergeProposal` abstraction and is the closest
existing answer to the problem this section is about.

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

One inconsistency is deliberate. The repository verbs are verb-first (`clone`,
`commit`, `publish`) and the collaboration verbs are noun-first
(`proposal create`, `issue list`). The grammar marks the layer: verb-first is
the version-control system, noun-first is the forge. A caller that notices the
difference has noticed something true about where the work happens.

## Forge neutrality

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
composed from validated path segments rather than taken from the caller: the
URL decided _which forge_, and it does not get to decide the host.

Only `github` is functional. `gitlab` and `bitbucket` parse their own specs and
then name what this install is missing — no credential minter, and no REST
client in the broker for merge requests and issues. That is the deliverable, not
a placeholder: `capabilities` answers without minting anything or touching the
network, so a caller learns the gap before it has written a revision it cannot
deliver. Bitbucket additionally records the one difference that is not plumbing:
Bitbucket Cloud ships no CLI, so its collaboration verbs need a REST client in
the broker rather than an allowlist entry — the same conclusion `forge.py`
reaches for pull requests.

The GitHub forge reaches the API through `gh api` and never through `gh pr` or
`gh issue`. Those subcommands infer the repository from a `.git/config` found
above the working directory, which is the one file this whole design exists to
keep out of the credentialed process, and they format for a human. `gh api`
takes an explicit path and returns the API's own JSON, so the forge class is a
REST client that borrows `gh` for authentication — and the day `gh` leaves the
broker image, one method changes.

Translation is where the judgement is. A proposal has three states — `open`,
`closed`, `merged` — rather than GitHub's two plus a nullable `merged_at`,
because closed and merged are different outcomes on every forge and a caller
should not have to know how one of them encodes the difference. An `[bot]`
suffix comes off a login here rather than at the caller; `forge.py` records what
comparing an unnormalised one costs, which was an agent that answered its own
comments forever. And `issue list` drops the nodes carrying a `pull_request`
key, because GitHub is alone in modelling a proposal as an issue.

## The protocol

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
`BUNDLE_TOO_LARGE`, 409 `NOT_FAST_FORWARD`, `BASE_MOVED` and `BRANCH_DIVERGED`,
502 `FORGE_CALL_FAILED` and `GIT_FAILED`. `404 VCS_DISABLED` is what an install
without the flag answers.

Every credentialed verb mints first, and the minting happens on the broker side.
The GitHub credential is an App installation token that expires within the hour,
and an expired one surfaces as `Authentication failed` from inside the broker's
own clone — which reaches the caller as a clone failure and reads like the
repository is gone. Inferring expiry from a failure means the first verb after
an idle hour fails once, for a reason the caller cannot act on. Minting is
idempotent and costs one local process, so a forge that has an expiring
credential refreshes before every verb that spends one. This is a forge's
decision, not the broker's, because the answer is forge-shaped: a forge
configured with a long-lived personal token has nothing to do here. It moved out
of the sandbox for the same reason — knowing that a GitHub token expires is
forge knowledge, in the one container that is supposed to have none of it.

## Replacing `gh`

Shipping `gh` and naming it in skills binds this install to GitHub regardless of
what the abstraction above says, so the collaboration verbs exist to make
removing it possible. `vcs.py issue list --state open --labels bug` is what
replaces `gh issue list`; `vcs.py proposal create` replaces `gh pr create`.

That removal is not complete. `gh` remains on the broker's executable allowlist
and in the sandbox image, and these still name it: the seven governance SOPs
under `agents/platform/governance/`, `forge.py`, the `fleet-audit`,
`github-issue-resolver`, `pr-conversation` and `submit-suggestion` skills, and
the shim loop in `deploy/sandbox/Dockerfile`. Each is a port, not a rewrite —
`forge.py` in particular is already a five-operation forge seam and becomes an
HTTP client of these routes. Until they are all ported, the escape hatch stays
open, and an agent that finds this skill insufficient can still go around it.
What changes now is that it has less reason to: the capabilities it was leaving
the protocol to get are verbs.

## What this does not fix

The credential proxy authenticates no caller. Anything that can reach loopback
in the sandbox can drive these verbs. That is the same boundary #913 has and the
same one `docs/designs/agent-shell-sandboxing.md` describes; what makes it a
boundary at all is that the sandbox has no other credential path, not that the
socket is trusted.

`clone` pulls a whole branch's history, which is the wrong shape for a one-off
read of a large upstream repository, and there is no shallow option to make it
cheaper. `inspect-repository`'s paging is the answer there, and the skill says
so.

`publish` proves ancestry, not authorship. The revisions in the bundle carry
whatever author the sandbox's git wrote, and the broker does not sign or rewrite
them.

## Related

- [`docs/designs/agent-shell-sandboxing.md`](agent-shell-sandboxing.md) — the sandbox, the credential proxy, and why `git` on PATH is a shim
- [`docs/credential-isolation-design.md`](../credential-isolation-design.md) — the content-passing workspace this builds beside
- [`docs/designs/gitops-workspace-leases.md`](gitops-workspace-leases.md) — the leased shared checkout this replaces for repository reads
- [`docs/designs/memory.md`](memory.md) — the experiment format the A/B/C run follows
