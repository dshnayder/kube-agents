# Memory tag isolation

**Status:** implemented on the Chat Agent profile.

How `kube_agents_memory` keeps every user's long-term memory apart while holding
all of it in a single Hindsight bank, and why the wrapper exists at all when
Hindsight already ships tagging, tag filters and tag-scoped consolidation.

Source of record: [`agents/chat/plugins/memory/kube_agents_memory/`](../../agents/chat/plugins/memory/kube_agents_memory/).
Operational settings live in the `memory` block of
[`agents/chat/config.yaml`](../../agents/chat/config.yaml).

## The shape

One Hindsight bank. Every memory carries a **scope tag**:

| Scope    | Tag            | Written by                             | Read by        |
| -------- | -------------- | -------------------------------------- | -------------- |
| Personal | `user:<id>`    | automatic capture, and `memory_retain` | that user only |
| Shared   | `scope:shared` | `memory_retain(scope="shared")`        | everyone       |

`<id>` is the gateway identity (`agent._user_id`), sanitised the same way
Hindsight sanitises a bank name segment. Recall asks for `[user:<id>,
scope:shared]` and nothing else can come back.

The Chat Agent is the only profile that gets this, because it is the only
profile that knows who is speaking. Kanban-spawned specialists carry no human
identity; the provider fails closed there rather than pooling their writes into
one anonymous bucket.

## Why a wrapper

Hindsight has every mechanism this needs. What it has no way to do is learn the
**current user's id**: `{user}` substitution is applied by
`_resolve_bank_id_template`, which the plugin calls for `bank_id` and for nothing
else. `retain_tags` and `recall_tags` are read as literal config strings, so a
configured `retain_tags: "user:{user_id}"` tags every user with the eleven
characters `user:{user_id}` — no isolation, no error, and a config file that
reads as though it were working.

Resolving that identity into the right tags is most of what the wrapper does.
Everything else is still the stock provider, loaded through
`load_memory_provider("hindsight")`; not forking its implementation means a
Hermes base-image bump brings Hindsight fixes along with it.

## The four pinned settings

Each of these is set by the wrapper rather than left to configuration, because
each is a silent leak or a silent loss if it is wrong.

### 1. `recall_tags_match = "any_strict"`

Hindsight's tag matcher treats `any` and `all` as _"matching tags **or** no tags
at all"_; only the `_strict` variants exclude untagged rows. The plugin's default
is `any`. In a shared bank that default returns every untagged memory to every
user.

The corollary matters as much: under `any_strict`, an **untagged memory is
invisible to everyone**. Anything written into this bank must carry a scope tag
or it is silently lost, which is why the wrapper attaches one on every write
path rather than leaving it to the caller — and why the TTL curator aborts
outright on an observation it cannot scope.

### 2. `observation_scopes = [[scope_tag]]`

Recall returns _observations_ — the LLM-consolidated layer — not raw facts. So
isolation is only real if the observation layer is scoped too.

Hindsight's default, `combined`, scopes an observation by the full tag set of its
sources. The stock provider attaches a `session:<id>` lineage tag to every
auto-retained turn, which would make each session its own scope: nothing said
last week would ever consolidate with what is said today. Pinning the scope to
the single scope tag fixes that and the isolation question together, and is
exactly the use Hindsight's consolidator documents for explicit scopes
("deduplicate across volatile per-call provenance tags … without dropping those
tags from the source facts").

`per_tag` is the wrong tool here despite sounding right: it emits one observation
per individual tag, so a fact tagged `["user:alice", "cluster:foo"]` produces a
`cluster:foo` observation carrying no user tag at all.

### 3. Prefetch is forced to `recall` mode, and `memory_reflect` is reimplemented

`hindsight_reflect` and reflect-mode prefetch both call
`areflect(bank_id, query, budget)` with no tag arguments. In one bank that
synthesises across every user — the one path that would cross users even with
everything else correct.

The REST API and the generated client both accept `tags`, `tags_match`,
`tag_groups` and `fact_types` on reflect; only the plugin omits them. So the
wrapper implements `memory_reflect` against the client directly and pins
`_prefetch_method` to `recall`, whose filter path does apply the tags.

### 4. Shared writes bypass the stock retain path

`_build_retain_kwargs` merges the instance's `_retain_tags` into every write and
offers no per-call `observation_scopes` or `strategy`. A shared fact must not
inherit the caller's `user:` tag — it would consolidate into that person's scope
and be invisible to everyone else. `memory_retain` therefore builds its own item
and calls `aretain_batch`, which also avoids swapping instance attributes around
an asynchronous writer thread.

## What goes in which scope

Tag isolation works, and the first thing that proved was that it isolates too
much. "Alice is a tech lead in GKE", stated by Alice in her own DM, is captured
by the automatic path — which is always personal — and so lands under
`user:alice`, where no other user can ever reach it. Asked _"who can approve
this?"_ as a different user, the agent answered _"ask a tech lead."_ Re-stated as
`scope:shared`, the same question answered _"Alice is a tech lead in GKE who can
review and approve these changes."_ The org-chart knowledge the fleet most needs
is exactly what automatic capture files privately.

The discriminator is **would another user need this to know who to ask, or who
approves?** Roles, ownership and approval authority are shared; preferences,
defaults, possessions and working style are personal.

This is enforced in the **persona**, not in the wrapper:
[`agents/chat/SOUL.md`](../../agents/chat/SOUL.md) §1.6 carries the rule and the
four conditions on it (stated not inferred, roles only, never automatic, and the
agent says out loud that it wrote org-wide). The alternatives were a classifier
in the wrapper deciding scope per fact, or a second retain strategy that splits a
turn into a personal and a shared extraction. Both put a judgement call about
meaning into a layer that only manipulates tags — the wrapper would stop being
slim, and a misclassification would be a silent leak of a personal fact into
shared memory with no human in the loop. The model is already making that
judgement; the persona is where its judgement is directed.

The floor underneath is unchanged: automatic capture is still personal-only, so
nothing reaches shared memory without a deliberate `memory_retain(scope="shared")`.

## Where the connection settings come from

The stock provider reads `$HERMES_HOME/hindsight/config.json`. That file is
**image-owned**: it ships as
[`agents/chat/defaults/hindsight/config.json`](../../agents/chat/defaults/hindsight/config.json)
and `deploy/shared/docker-entrypoint.sh` force-copies it over the PVC copy on
every start, alongside `config.yaml` and the persona files. It carries the
connection settings and nothing else — mode, the in-cluster Hindsight URL, and
the two budget knobs.

It is image-owned because it was not, once. The file was hand-written onto the
PVC, where it survived every image roll carrying whichever design was current
when it was last touched — and kept naming a bank from the era before this one.
Tag isolation was unaffected, since the wrapper pins the tags itself, which is
precisely what made it invisible: no code review or manifest diff can see a file
that exists only on a volume.

So the bank name is not a setting. `_apply_scoping` pins the constant and logs a
warning if a config file disagrees, rather than obeying it, and the TTL curator
hardcodes the same constant. The URL the config file does carry is
fixed by the install recorded in
[`k8s-operator/config/integrations/hindsight/`](../../k8s-operator/config/integrations/hindsight/README.md),
which is the source of truth for that value — changing the service or its
namespace means editing the defaults file and rebuilding the agent image.

## Failing closed

Personal memory is switched off, and only `scope:shared` is reachable, whenever
the speaker cannot be attributed:

- **No `user_id`.** Nothing to scope by. Automatic capture is disabled outright,
  so an anonymous session cannot write untagged rows into the bank.
- **A shared thread.** `build_session_key()` omits the participant id inside a
  thread unless `thread_sessions_per_user` is set, so the second person to post
  reuses the first person's cached Agent — and `agent._user_id` was frozen at
  construction. A per-user tag would then recall A's memories into B's prompt and
  retain B's turns under A's name. Nothing in the provider protocol identifies
  the speaker per call (`system_prompt_block()` takes no arguments), so there is
  no way to detect this later; it is decided at session start.

Both cases put an explanation in the system prompt, so the agent tells the user
why rather than appearing to forget them. This is what makes personal memory
DM-only by design.

### Why a space stays on one session

`thread_sessions_per_user` would restore attribution — and with it personal
memory — by giving each participant in a thread their own session. It is
deliberately left off. A space's value is the shared thread: with per-user
sessions, Bob's follow-up lands in a session that never saw Alice ask the
question, and the agent has no idea what he is following up on.

The cost of that choice is that **failing closed protects the store, not the
transcript.** A space is one conversation, so Alice's "my cluster is clusterA" is
in the model's context when Bob later says "delete my cluster" — and nothing in
the memory layer was involved in binding the two. Switching personal memory off
does not prevent it.

The control for that is in the persona, not here: [`agents/chat/SOUL.md`](../../agents/chat/SOUL.md)
§1.6 requires a possessive in a space to resolve only from the current speaker's
own words, and requires a destructive delegation to be confirmed against a named
target first.

## Related

- [Distill-then-retire](memory-distill-then-retire.md) — how the bank is kept
  bounded without forgetting what it learned.
- `agents/chat/SOUL.md` §1.6 — the agent-facing rules for using the two scopes.
