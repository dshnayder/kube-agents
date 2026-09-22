# Context-scoped capabilities: exposing the tools and skills a turn needs

> **STATUS — proposal for review; nothing here is implemented.** The measurement phase (§7,
> phase 0) is the first deliverable and the gate on the rest.

**Status:** Draft for review. **Scope:** every agent profile this repository ships (chat,
platform, cluster, a2a specialists) and the harness that runs them, whichever harness that is.

## Summary

An agent sees its whole capability catalogue on every turn: every tool schema in the request and
every skill in a system-prompt index. The Platform Agent's catalogue is 44 skills and roughly 90
tools, and it grows without a gate, because 29 of those skills are synced from an upstream
repository and every new MCP server adds its tools to the same list. Published measurements put
the point where tool selection accuracy starts to fall at 30 to 50 tools. This repository has
recorded two incidents from the other direction, where the harness hid capabilities to keep the
list short: the model failing to find a hidden capability
([#1703](https://github.com/gke-labs/kube-agents/issues/1703)), and the model acting on the wrong
one after a search returned it
([#1699](https://github.com/gke-labs/kube-agents/issues/1699)). The crowded-list failure has no
recorded incident here yet; §1.2 gives the published evidence and the persona prose that routes
around it, and phase 0 measures it.

This document proposes a **capability-scoping layer** that sits between the catalogue and the
model request and decides, per turn, which capabilities the model sees in full, which it sees by
name only, and which it must search for. The layer is harness-independent: it is specified as
four interfaces (catalogue, scope policy, ranker, working set) and one record (what was shown and
what was used). §8 reports the first run of the prototype; §9 maps the layer onto Hermes with the hooks that
exist today and the one upstream change it needs; §10 says what a replacement harness implements
natively.

The design borrows its shape from the memory design that already runs on the chat profile: stop
injecting the corpus and start searching it, pay for the corpus once at build time and per turn
only for what the turn needs, and decide by measurement rather than by argument. It differs in
one respect that matters: a memory the ranker misses is a fact the agent lacks, while a capability
the ranker misses is an action the agent substitutes for, so the working set is bounded below as
carefully as it is bounded above.

## How to read this document

§1 states the problem with the numbers behind it. §2 sets goals and non-goals. §3 is the
vocabulary. §4 is the design: the layer, its interfaces, and the working-set policy. §5 weighs
the alternatives. §6 is the failure modes and what bounds each. §7 is the evaluation plan and the
rollout, in that order, because the rollout is gated on the evaluation. §8 is what the prototype
measured when the plan was run once. §9 is the Hermes integration; §10 the custom-harness
contract. §11 lists what is unresolved.

## 1. The problem

### 1.1 What the model sees today

| Profile  | Skills in the index | Tools in the request                                                                                                    | Where the catalogue comes from                                                |
| -------- | ------------------- | ----------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| platform | 44                  | ~40 harness core tools, 3 search-bridge tools, 3 memory tools, 14 kanban tools; 43 MCP tools deferred behind the bridge | `agents/platform/skills/` (29 synced from `google/skills`), three MCP servers |
| cluster  | 7                   | ~40 harness core tools, 3 bridge tools; GKE and developer-knowledge MCP tools deferred                                  | `agents/cluster/`, scaffolded per cluster                                     |
| chat     | 0 (skills disabled) | router, kanban, memory                                                                                                  | `agents/chat/config.yaml`, a deliberate lockdown                              |

The tool counts are derived from the toolset definitions at the pinned harness version; the
measured figure per profile is phase 0's first output (§7.1). The skill bodies total 465 KB;
the index the model reads is one line per skill, and the harness truncates each description to 60
characters. The platform profile's descriptions average 77 characters, so most of the 44 lines the
model chooses from end in an ellipsis.

Three static levers exist and are used: a per-profile toolset allowlist, a per-platform toolset
denylist applied last, and per-task skill lists on cron jobs (16 of them) and kanban cards (the
`--skill` flag). All three decide before the question exists. Nothing decides per turn.

### 1.2 Why the size matters

The evidence that a large catalogue costs accuracy, not only tokens, is consistent across sources:

- Anthropic's tool-search documentation states that selection accuracy "degrades once you
  exceed 30–50 available tools", reports an 85% reduction in tool-definition tokens from
  deferral, and gives Opus 4 improving from 49% to 74% and Opus 4.5 from 79.5% to 88.1% on an
  MCP evaluation with search enabled.
- RAG-MCP (arXiv 2505.03275) reports selection accuracy rising from 13.62% to 43.13% when a
  retriever narrows the catalogue before the model sees it, with prompt tokens halved. The tools
  and model were unchanged; only the visible count changed.
- "How Many Tools Should an LLM Agent See?" (arXiv 2605.24660) shows the right shortlist size
  varies per query, and that a fixed shortlist is wrong in both directions.
- The harness's own tracker quantifies the token side: one report measures 54 tool schemas at
  27k tokens, 83% of every request.

The failure is not confined to the tool array. A skill index is a selection problem with the same
shape, and this repository's personas already carry the evidence that the choice is hard: the
platform persona ([`agents/platform/SOUL.md`](../../agents/platform/SOUL.md), "Authorized Commits
& Change Flow") names which of two skills owns the write path, and a bench case
([`bench/tasks/rca-remediation-pr/task.yaml`](../../bench/tasks/rca-remediation-pr/task.yaml))
spells out that remediation goes through `submit-suggestion` and not `github-issue-resolver`,
which "was retargeted out of this scenario". Prose in a persona is the routing lever available
today, and neither file is an incident record: no run in this repository is documented as having
picked the wrong skill from the index.

### 1.3 Hiding is the other failure

Deferral is not free. Two incidents in this repository show what happens when the harness hides a
capability to shorten the list:

- In [#1703](https://github.com/gke-labs/kube-agents/issues/1703) (delegation broken on a
  kustomize dev install) the front-door agent's `kanban_create` was absent in 25 of 32 runs; a
  search for "kanban" returned nothing, and the model reported that only the four bridge tools
  existed.
- In [#1699](https://github.com/gke-labs/kube-agents/issues/1699) (the front-door agent patched a
  Deployment through the hosted GKE MCP server on the ambient credential) a search for
  `kanban_create` returned `mcp__gke__create_cluster`, and the agent went on to use the GKE tools.

The harness's own documentation records the same pattern in benchmarking. Its tool-search page at
the pinned version
([`website/docs/user-guide/features/tool-search.md`, "Why the listing exists"](https://github.com/NousResearch/hermes-agent/blob/v2026.8.19/website/docs/user-guide/features/tool-search.md))
says that without the names listing "deferred capabilities are _invisible_ — live benchmarking
showed models substituting visible core tools (running `gh` in the terminal instead of searching
for the deferred GitHub tool) or declaring a capability nonexistent instead of calling
`tool_search`". An upstream issue,
[hermes-agent#71481](https://github.com/NousResearch/hermes-agent/issues/71481), measures that a
names-only skill index breaks discovery because the model does not take the listing fallback.

So the target is not the smallest working set. It is the working set that contains what the turn
needs, with a discovery path the model actually takes when it does not, and a measurement that
says how often that happens.

### 1.4 What is not known

Nothing in this repository measures how often an agent picks the wrong skill or tool, or how
often it fails to find one. Bench verifiers check outcomes and report phrases; none asserts which
capability was used. Token cost per turn is recorded per bench run but not broken down by tool
schemas versus skill index versus transcript. The design therefore begins by producing those
numbers in a mode that changes nothing the model sees (§7.1), and the decision to enforce scoping
is made on them.

## 2. Goals and non-goals

Goals:

1. Per turn, present the model with a working set of capabilities sized to the turn, with every
   capability it is required to use present in full.
2. Keep every capability discoverable from inside a turn, and measure how often discovery is
   needed.
3. Make the scoping decision observable: for every turn, what was shown, what was hidden, what
   was used, and whether the use was a miss.
4. Hold the model's request prefix stable enough that prompt caching survives scoping.
5. Specify the layer so it runs on the current harness through its extension points and moves to
   a replacement harness without redesign.

Non-goals:

- **Authorization.** A hidden capability is not a forbidden one. RBAC, the credential broker,
  and the sandbox remain the only permission boundaries. §6 says why this matters in the design,
  not only in the reading of it.
- **Rewriting skill content.** The layer reads catalogue metadata. Improving a skill's
  description is a separate, cheaper change that phase 0 will likely recommend.
- **Choosing the profile.** Which specialist a task goes to is the dispatcher's job (#1719). This
  layer scopes inside a profile once the task has arrived.
- **A router model.** No second model call decides what the first one sees.

## 3. Vocabulary

- **Capability**: a tool (a callable with a schema) or a skill (a procedure the model loads into
  context). The layer treats them alike for scoping and differently for injection.
- **Catalogue**: every capability a profile may ever use. Static per profile; built once.
- **Scope**: the subset of the catalogue eligible for a session or task. Deterministic.
- **Working set**: the subset of the scope the model sees in full on a given turn.
- **Shelf**: the part of the scope the model sees by name only.
- **Scoping signal**: the text the ranker scores against: the turn's user message, the task
  assignment, and the names of capabilities used in recent turns.
- **Miss**: a turn in which the model used, or searched for, a capability that was in scope and
  not in the working set.
- **Pinned**: a capability that is in every working set of its profile.

## 4. The design

### 4.1 Three stages

Scoping happens in three stages, each narrowing the last and each with a different cadence and
a different owner.

**Stage 1, profile scope, at build time.** The catalogue is compiled from the profile directory
and its MCP manifests into a single indexed artifact: name, full description, trigger phrases,
domain tags, risk class, token cost, and the `pinned` flag. This stage exists today as the
toolset allowlists and the skills directory; the design adds the compiled artifact and the
metadata. Owner: the profile author. The index is built in the image so no runtime pays for it,
which is what the memory design meant by paying for the corpus once.

**Stage 2, task scope, at session or assignment start.** The entry point declares what the task
is for: a kanban card's `--skill` list, a cron job's `skills:` field, a dispatcher's domain tag, a
chat delegation's intent. The task scope is the catalogue filtered by those declarations plus
the pinned set; with no declaration, it is the whole catalogue. Deterministic, logged, and the
basis for delegation narrowing: a child agent inherits the parent's task scope, not its
catalogue. Owner: the caller that created the task.

**Stage 3, turn scope, at every turn boundary.** The ranker scores the task scope against the
scoping signal and the working-set policy (§4.3) decides what enters, stays, and leaves. The
output is the working set, the shelf, and a scoping record. Owner: this layer.

### 4.2 Interfaces

The layer is four interfaces and a record. Names are illustrative; the contract is the shape.

```text
CapabilityCatalogue
  entries() -> [CapabilityEntry]            # compiled at build time
  CapabilityEntry:
    id, kind (tool|skill), name, description, triggers[], anti_triggers[],
    domains[], risk (read|write|external), pinned, cost_tokens, requires[]

ScopePolicy
  task_scope(catalogue, assignment) -> Scope   # deterministic filter
  budget(profile, turn) -> {max_skills_full, max_tools_full, max_tokens}

Ranker
  rank(scope, signal) -> [(entry, score)]     # no side effects, no model call

WorkingSet
  step(previous, ranked, budget) -> (working_set, shelf)   # stickiness, eviction

ScopeRecord (one per turn, emitted before the model call, completed after it)
  turn_id, signal_hash, shown_full[], shown_names[], hidden[],
  used[], misses[], searched[], budget, ranker_version
```

`rank` takes no model call and has no side effects. That is a design constraint, not an
optimisation: a ranker that calls a model doubles latency to first action on every turn, and a
ranker that reads the cluster can be steered by what it reads. The ranker scores catalogue
metadata against the signal and nothing else.

The catalogue metadata is the part profile authors own, and the part that decides quality. Two
consequences follow. The 29 synced skills cannot carry metadata in their `SKILL.md`, because the
sync overwrites them wholesale; their entries live in a sidecar file in the skills directory that
the sync does not touch, keyed by skill name, and the build fails when a skill has neither
frontmatter metadata nor a sidecar entry. And the domain tags reuse the slugs bench already
defines in `docs/designs/domains.yaml`, so the same vocabulary that says which domain a case
covers says which domain a skill serves, and a case can later assert that its domain's skill was
used.

### 4.3 Working-set policy

The policy has five rules. Each exists because of a failure mode in §6, and the section cites the
rule it bounds.

1. **Pinned capabilities are always in the working set, in full.** The harness's own core
   (read a file, run a command, load a skill, search the catalogue) and whatever the profile
   author pins. The platform persona's two write-path skills are the obvious candidates; pinning
   is the author's call and shows up in the record.
2. **The shelf is never empty.** Every in-scope capability the working set omits appears by
   name, grouped by domain, with the instruction to search. A capability that is nowhere is one
   the model substitutes for; the incidents in §1.3 are both this.
3. **Entry is by rank within budget; exit is by disuse.** A capability enters when it ranks
   inside the budget for the current signal. It leaves after N turns without use, N configurable,
   default 5, or when the budget is exceeded and it is the least recently used. It does not leave
   because the next turn's signal ranked it lower. This is hysteresis: a troubleshooting session
   that oscillates between storage and networking keeps both.
4. **Changes land at turn boundaries only.** The working set for turn T is fixed before the
   first model call of turn T and does not change during the tool loop. A mid-turn discovery (the
   model searched and found something) enters at T+1 and is recorded as a miss for T.
5. **The budget is a token budget with count ceilings**, not a count alone. Default: skills in
   full up to 8 and 2,000 tokens; tools in full up to 25 and 8,000 tokens, both including the
   pinned set. The numbers are starting points for the A/B in §7.2, chosen so that the default
   working set sits under the 30-tool knee with room for pinned tools.

### 4.4 Ranker

Version 1 is lexical: BM25 over each entry's name, description, triggers, argument names and
descriptions, with a fixed bonus for a domain-tag match against the task scope and a penalty for
an anti-trigger match. It is deterministic, runs in microseconds, needs no model or network, and
can be unit-tested against a golden set of (signal, expected top-k) pairs drawn from bench cases
and the persona's own routing rules. That golden set is a test with no model in the loop, and it
runs on every pull request.

Version 2 adds an embedding similarity term if phase 0 shows a miss rate the lexical ranker
cannot close. Embeddings are computed at build time for the catalogue and per turn for the
signal; the per-turn call is one small embedding request, and the design keeps it optional
because it is the only network dependency the ranker would have. Whether v2 is needed is a
number, not a preference.

### 4.5 Injection

Tools and skills leave the layer differently.

Tools in the working set go into the request's tool array; the shelf goes into a names-by-domain
manifest that the search tool's description carries. Both are outside the cached prefix (§6,
caching), which for most providers means the tool array has to be treated as volatile and the
system prompt as stable, so the design keeps the system prompt free of per-turn content.

Skills in the working set are rendered as an index block with full descriptions; the shelf is
the names-only block. Where the block goes is a harness decision, and §8 discusses the two
places Hermes allows. The skill body still loads on demand; the layer changes which descriptions
the model reads, not when bodies enter.

### 4.6 The scoping record

The record is the design's product as much as the working set is. Emitted per turn, it lets the
miss rate, the substitution rate, and the token split be computed from logs without a bench run,
and it is what phase 0 emits with enforcement off. It carries no user text, only a hash of the
signal, so it can be retained at the same tier as the tool-call audit.

## 5. Alternatives

| Option                                                              | What it fixes                           | What it costs                                                                                                 | Verdict                                         |
| ------------------------------------------------------------------- | --------------------------------------- | ------------------------------------------------------------------------------------------------------------- | ----------------------------------------------- |
| A. Status quo: prose routing, everything visible                    | Nothing; baseline                       | Grows with every synced skill; no measurement of the cost                                                     | Keep as the control arm                         |
| B. More, narrower static profiles (#1719)                           | The dispatcher picks a small catalogue  | A profile per domain multiplies personas to maintain; a task spanning two domains still needs both catalogues | Necessary, not sufficient; stage 1 builds on it |
| C. Model-invoked search only (Anthropic tool search, Hermes bridge) | Token cost; nothing visible until asked | The two incidents in §1.3; a search the model does not think to make is a capability that does not exist      | Keep as the escape hatch, not the mechanism     |
| D. Automatic per-turn retrieval (bigtool, RAG-MCP)                  | Selection accuracy and tokens           | Retrieval misses; flapping; cache churn if done naively                                                       | Adopted, with §4.3's bounds                     |
| E. Two-stage router model                                           | Accuracy, at any catalogue size         | A model call before every turn; the router's own errors are invisible to the executor                         | Rejected for latency and opacity                |
| F. Provider-side deferral (`defer_loading`)                         | Everything in D, with caching handled   | Anthropic-only; this repository talks to every provider through an OpenAI-compatible proxy                    | Unavailable on this path                        |
| G. Fix descriptions and the 60-character truncation only            | Chooser quality for the current index   | Does not bound growth; the platform index is still 44 lines                                                   | Do first, inside phase 0                        |

The adopted shape is B for the catalogue, D for the working set, C for the shelf, and G as the
first thing shipped. B and D are not competitors: a smaller catalogue is a better input to a
ranker, and a ranker is what makes a catalogue that spans domains usable.

## 6. Failure modes and their bounds

**Retrieval miss.** The ranker leaves out the capability the turn needs. Bounded by rule 2 (the
shelf names it), rule 1 (the required ones are pinned), and the search tool (the model can ask).
Measured as the miss rate in the record. This is the failure that decides whether v2 of the
ranker is built.

**Substitution.** The model, lacking a capability, uses another one to the same end, as in
#1699. This is a miss the record cannot see directly, because nothing was searched for. Bounded
by rule 2 and by the risk class: a write-class capability never appears on the shelf as a
search result for a read-class query without its name being explicit in the query. Detected in
bench by verifiers that assert which capability produced the outcome, which is a bench-format
change (§11).

**Flapping.** The working set churns turn to turn, and with it the request. Bounded by rule 3's
hysteresis and rule 4's boundary; measured as working-set churn per session in the record.

**Cache invalidation.** Most providers cache a prefix that includes the tool array, so any
per-turn change to it costs the cache. Hermes' own documentation states that toolset edits
mid-session invalidate the cache; #776 records that the proxy currently sends no cache
breakpoints at all, so the baseline cache behaviour has to be measured, not assumed. The design
keeps per-turn content out of the system prompt, changes the tool array only at turn boundaries
and only when the working set changes, and lets the sticky policy keep it unchanged on most
turns. The A/B records cache-read tokens per turn (§7.2) as a first-class metric.

**Scoping mistaken for authorization.** A reviewer, or a future change, treats "not in the
working set" as "cannot be called". It cannot be, because the shelf and the search tool exist,
and because a hidden tool the model names by a guess may still resolve. The layer never removes a
capability from the scope for safety reasons, and the design places this sentence in the
persona-facing docs so it is not learned from an incident.

**Injection through the signal.** The scoping signal contains user text. The ranker reads it as
a query, executes nothing, and writes only a hash of it to the record. A user who names a
capability gets it ranked higher, which is the intended behaviour and also the extent of the
influence.

**Growth resumes elsewhere.** Once the working set is bounded, nothing stops the catalogue from
growing until the shelf itself is long enough to be a selection problem. The record's shelf
size per profile is the tripwire; a shelf past a threshold is the signal to split the profile
(option B), which is why B and D are one design.

## 7. Evaluation and rollout

The rollout is gated on the evaluation. Each phase has an exit criterion that is a number.

### 7.1 Phase 0: measure, change nothing the model sees

Build the catalogue artifact and the v1 ranker; emit the scoping record on every turn with
enforcement off. The working set is computed and logged, and the model still sees everything.
From the logs, per profile:

- tokens per turn split into tool schemas, skill index, and the rest;
- what the record would have called a miss, had enforcement been on: the fraction of turns where
  a capability outside the computed working set was used;
- the capability-use distribution: which skills and tools are used at all, which never, which
  together.

In the same phase, ship option G: full descriptions in the index, and the triggers and domains in
the catalogue metadata for every skill, sidecar entries for the synced ones.

Exit: two weeks of nightly bench and the shared install's chat traffic in the record. Decision:
if the would-be miss rate under the default budget is under 5% and the tool-schema token share
is under 20% of the request, the remaining phases are not worth their complexity and the
document is closed with that finding. Either number above its threshold opens phase 1.

### 7.2 Phase 1: enforce on the platform profile behind a switch

Enforce the working set for skills first, then tools, on the platform profile, with a per-profile
switch that restores everything-visible. Run the A/B the memory design ran: the active bench
matrix at three repetitions per arm, arms being everything-visible and scoped, on the same build.
Metrics per arm:

| Metric                     | Source                     | Must                               |
| -------------------------- | -------------------------- | ---------------------------------- |
| Bench pass rate            | `results.json`             | not regress on the blocking roster |
| Miss rate                  | scoping record             | under 5%                           |
| Search-then-use rate       | scoping record             | reported; no target                |
| Input tokens per turn      | harness usage              | fall                               |
| Cache-read tokens per turn | harness usage              | not fall                           |
| Time to first tool call    | trace                      | not rise by more than the budget   |
| Wrong-capability incidents | bench verifier (new field) | zero on the cases that assert one  |

Exit: every "must" holds across three repetitions. A regression on any blocking case is a stop,
per the presubmit gate's rules, not a tuning opportunity.

### 7.3 Phase 2: the cluster profile and delegation

The cluster profile's catalogue is small enough that the value is in delegation: a child created
from a kanban card inherits the card's task scope rather than the parent's catalogue. This is
where #1719's dispatcher and this layer meet, and the phase is sequenced after it.

### 7.4 Phase 3: the escape-hatch quality loop

With the record in place, the search-then-use rate names the capabilities the ranker keeps
missing. Each is a description or trigger fix in the catalogue, and for synced skills an upstream
description change proposed to `google/skills`, which improves the chooser for every consumer of
those skills. This is the loop that keeps running after the design is done.

## 8. What the prototype run showed

The plan in §7 was run once, on 2026-09-21, against a dedicated install with the Platform Agent
as the front door. The method, code and probes are in
[`experiments/capability-scope-ab/`](../../experiments/capability-scope-ab/README.md), and the
scored tables, one row per run, are committed under its `results/` directory; the raw transcripts
and per-turn records stayed on the machine that ran them. Every number below is in those files.

### 8.1 What was run

Three arms and two catalogue sizes, 22 probes, three repetitions each, one prompt per session,
the same image and model throughout. `stock` is the shipped index: every skill, description cut
at 60 characters. `fulldesc` is the same index with full descriptions, the cheapest fix in §5.
`scoped` is the design's stage 3: names only in the system prompt, the top six skills by a BM25
ranker injected with full descriptions per turn, and the tool array filtered to a pinned set plus
six. The `shipped` rung is the 44 skills in the repository; the `grown` rung adds 60 real skills
from the upstream repository the sync reads, five of which the next sync will pull in unasked.
Twenty probes name a gold skill; two are controls where loading any skill is wrong. Each run was
capped at ten model iterations and prefixed with one sentence keeping it on the local cluster.
The model was Gemini 3.1 Pro through the install's LiteLLM; §8.6 covers the repeat on Gemini 3.5
Flash.

The offline ranker alone, before any model saw it, put the gold skill in its top six for 16 of
20 probes on the shipped catalogue and 15 of 20 on the grown one, and first for 13 and 10. Those
misses are carried into the run, not tuned away.

### 8.2 Selection

Sixty probe runs per cell; the two controls are reported separately.

| First skill loaded               | stock  | fulldesc | scoped | stock  | fulldesc | scoped |
| -------------------------------- | ------ | -------- | ------ | ------ | -------- | ------ |
| catalogue                        | 44     | 44       | 44     | 104    | 104      | 104    |
| gold                             | 55.0%  | 60.0%    | 68.3%  | 53.3%  | 58.3%    | 78.3%  |
| acceptable                       | 1.7%   | 0.0%     | 1.7%   | 0.0%   | 8.3%     | 5.0%   |
| wrong                            | 1.7%   | 0.0%     | 3.3%   | 1.7%   | 3.3%     | 0.0%   |
| none loaded                      | 41.7%  | 40.0%    | 26.7%  | 45.0%  | 30.0%    | 16.7%  |
| gold loaded at any point         | 56.7%  | 60.0%    | 71.7%  | 53.3%  | 61.7%    | 78.3%  |
| spurious loads on control probes | 0 of 6 | 0 of 6   | 0 of 6 | 0 of 6 | 0 of 6   | 0 of 6 |

On the grown catalogue the gain in gold-first under `scoped` is significant against both other
arms (two-proportion test: p = 0.004 against `stock`, p = 0.019 against `fulldesc`; 95% interval
66 to 87 against 41 to 65 and 46 to 70), and so is the drop in runs that load no skill at all
(p = 0.001 against `stock`). On the shipped catalogue every movement has the same direction and
about half the size, and none reaches significance at sixty probes (p = 0.13 for gold-first
against `stock`). `fulldesc` alone moves gold-first by five points on both rungs and does not
move the no-skill rate on the shipped one; the descriptions are necessary for the gain and not
sufficient for it. Wrong picks are rare in every arm. The crowded-list failure in this run is
not the wrong skill; it is no skill, and scoping reduces that most where the list is longest.

Three details from the per-probe table qualify the averages:

- The regression §6 predicted appears once: the repository-inspection probe went from three gold
  picks under `stock` to none under `scoped` on the shipped rung, because the ranker leaves that
  skill out of its top six and the names-only shelf did not bring the model to it.
- Under `scoped`, a third of all skill loads were of skills outside the injected six (25 of 78
  on the shipped rung, 20 of 82 on the grown), so the shelf carried them; without it those would
  have been misses. The shelf is load-bearing.
- Under `stock` on the grown catalogue one run loaded a skill that does not exist, a plausible
  name assembled from its neighbours in the list; no scoped run did. Four of the six wrong picks
  across all cells were the write-path skill chosen for a configuration change, which the
  persona's own routing rule half-endorses; the scoring kept them wrong.

### 8.3 Cost

| Per run, medians                | stock | fulldesc | scoped | stock | fulldesc | scoped |
| ------------------------------- | ----- | -------- | ------ | ----- | -------- | ------ |
| catalogue                       | 44    | 44       | 44     | 104   | 104      | 104    |
| prompt tokens, first model call | 28.4k | 31.3k    | 28.3k  | 30.4k | 38.6k    | 29.7k  |
| input tokens, whole run         | 309k  | 328k     | 306k   | 318k  | 399k     | 321k   |
| cache-read share of input       | 92%   | 94%      | 88%    | 93%   | 93%      | 88%    |
| wall seconds                    | 70    | 120      | 74     | 77    | 129      | 92     |
| seconds to first tool call      | 8.7   | 15.5     | 7.3    | 8.4   | 17.9     | 7.5    |

The prompt is about 28k tokens on every arm because it is the persona and the harness's standing
instructions; the skill index at 60 characters a line is under a thousand tokens of that, the
tool array under the API server is 21 to 35 tools of which the filter hid one to a dozen, and the
MCP tools were already behind the harness's own search bridge. What the table does show is the
cost of the obvious alternative: full descriptions for every skill add 3k tokens to every call on
the shipped catalogue and 10k on the grown one, and double the time to the first tool call.
`scoped` delivers the descriptions that matter at the token cost of `stock`, and at 104 skills it
is already 800 tokens a call cheaper than the truncated index. The token claim in §1 therefore
holds as a function of catalogue size and of where the tool schemas sit, and on this profile as
shipped it is small; the accuracy claim is what the run supports.

### 8.4 What the shadow record said before enforcement

Phase 0's exit test asked whether the would-be miss rate under the default budget is under 5%.
In the `stock` arms, where the plugin only recorded, 16 of 50 skill loads on the shipped rung and
28 of 58 on the grown rung were outside the working set the ranker would have shown. That is a
third to a half, far past the threshold, and it is the number that says the v1 ranker needs the
metadata it does not yet have (triggers, domain tags) before it could be the only route to a
skill. It is also why the shelf is not optional.

### 8.5 What changed in the design because of the run

- The token argument moves from §1's headline to a conditional on catalogue size and tool-schema
  placement; on this profile today the case for scoping is accuracy, and the case against the
  full-description fix on its own is cost.
- The shelf is load-bearing, measured at a third of loads, and a names-only index with no
  per-turn descriptions would have been a regression, which matches the upstream finding cited in
  §1.3.
- The chooser problem on this profile is under-use of skills, not wrong use. Injected full
  descriptions for a handful of candidates moved it; full descriptions for all of them did not.
- The ranker's offline misses reproduce in the run as the probes that regress. The metadata
  sidecar in §4.2 is the fix and is now the first item of the next phase, ahead of tools.
- The bench-side verifier gap in §11 has a concrete shape: every number above came from a driver
  that reads the tool-call trace, and the harness records the same trace today.

### 8.6 The repeat on Gemini 3.5 Flash

Pending: the same six cells were re-run on `gemini/gemini-3.5-flash`, the model production
answers with today, and this subsection is replaced by their numbers when the run completes.

## 9. Integration with Hermes

Hermes at the pinned version has most of the pieces and one gap. The facts, then the mapping.

**What exists.** The system prompt is built once per session and replayed verbatim; the skills
index sits in its volatile tier with every visible skill, no aggregate budget, and the 60-character
description limit. The tool array is fixed per session; it is rebuilt only at a turn boundary,
when MCP registration changes, by a per-turn prologue step. Deferral exists for MCP and plugin
tools only, as three bridge tools with a BM25 catalogue and a names manifest capped at 4,000
tokens; core tools cannot be deferred. Per turn, a plugin may append content to the API copy of
the user message through the pre-LLM-call hook, and a context engine may replace the request's
message list, system message included, without touching the tools. Plugins may register tools at
load and deregister them, and may add a system-prompt section of up to 8,000 characters, frozen
at session start. Delegation passes toolsets to a child programmatically but exposes no such
parameter to the model and has no notion of a skill subset. Kanban cards carry `--skill` and cron
jobs carry `skills:`, which is the task scope already declared.

**Stage 1.** A build step in the image compiles the catalogue from the profile's `skills/`
frontmatter, the sidecar metadata file, and the MCP schema cache the profile already ships.

**Stage 2.** The task scope is read from the card or job that spawned the worker, through the
same environment the kanban worker patches already read. Delegation narrowing uses the
programmatic toolsets parameter for tools; for skills it needs the child's index to be built from
the task scope, which is the same mechanism as stage 3.

**Stage 3, skills, no upstream change.** Set the static index to names only, grouped, which is
the shelf, and inject the working set's full descriptions per turn through the pre-LLM-call hook
into the user message's API copy, the same channel the memory prefetch uses, so the persisted
transcript is unchanged and the prefix stays stable. The upstream project's own measurement says
a names-only index alone breaks discovery; this design does not rely on it alone, because the
full descriptions arrive with every turn. The alternative channel is the context engine, which
can rewrite the index inside the system message per request; it is the cleaner placement and the
more invasive one, since only one context engine may be registered and this repository may want
that slot for other work.

**Stage 3, tools, one upstream change.** No hook mutates the tool array per turn. The per-turn
prologue already rebuilds it when MCP registration changes, so the change is a `select_tools`
hook at that point, receiving the full list and the turn's signal and returning the subset, with
the harness guaranteeing the same cache-safety it already claims for the prologue. This is an
upstream contribution rather than a patch, and it is one the upstream tracker is already asking
for in several forms: per-session tool filtering, deferral of built-in toolsets, an on-demand
tool loader for gateway profiles. Until it lands, tools are scoped statically at the toolset
level, which is what the profiles do today, and the record still measures what the per-turn
policy would have done.

**The record.** The pre-tool-call observer hook and the skill lifecycle hook give the "used"
side; the layer emits the "shown" side. Phase 0 needs nothing else.

**The description limit.** A one-line constant upstream; the tracker has an open issue asking to
relax it. Phase 0 proposes the change upstream and, if it is slow, carries a build-time patch as
the image already does for other constants.

**What the prototype learned about this install.** The run in §8 used exactly the shape above:
a plugin on the pre-LLM-call, pre-tool-call and post-API-request hooks, plus a build-time patch
with four environment-gated seams (names-only index, description limit, extra skill directories,
a tool filter in the conversation loop). Four facts about the deployment, not the harness, cost
most of the integration time and belong here so the next attempt does not rediscover them:

- The operator passes `spec.deployment.env` to the sandbox allowlist and the credential proxy,
  never to the gateway container, so a runtime switch has to reach the process another way; the
  prototype reads a file on the data volume at plugin load. A supported switch needs an operator
  field or an AgentPlugin.
- With the Platform Agent as the front door, its `config.yaml` on the data volume is not
  re-synced from the image at start, so an image-side change to `plugins.enabled` does not reach
  an existing install; the operator's overlay appends its own plugins to whatever list is there.
- The per-turn iteration cap comes from `agent.max_turns` in the profile config, which the API
  server re-derives into the environment on every request; setting the variable directly does
  nothing. `spec.harness.tuning.platform.maxTurns` is the supported knob.
- The authenticated door in front of the API server drops any turn over 300 seconds with an HTML
  502 and no session id. The bench harness uses that door, so a case whose single turn runs
  longer reads as a failure there; the experiment drove the API server on the pod's loopback
  instead.

## 10. What a replacement harness implements

The Hermes section is a shim over channels that were built for other purposes. A harness built
for this design provides the four interfaces of §4.2 as first-class request-assembly steps:

- the catalogue is an input to the agent's construction, not a directory walk at prompt time;
- the tool array and the skill index for a request are produced by one call to the working-set
  policy, given the previous working set and the signal, and the request assembler places them
  in the volatile region of the request by construction;
- delegation takes a scope, and the child's catalogue is that scope;
- the scoping record is emitted by the assembler, not reconstructed from observer hooks.

The same contract maps onto the Agent Development Kit's toolset predicate and skill toolset, with
the caveat its tracker records that per-step toolset evaluation is not yet supported there. That
is the point of specifying the layer as interfaces: the shim is disposable and the policy, the
ranker, the golden set, and the record survive the harness change.

## 11. Open questions

1. **Bench verifier for capability use.** The wrong-capability incidents are only detectable by
   a verifier that asserts which skill or tool produced the outcome. The case format has no such
   field, and skill scripts run through the code-execution tool leave no distinct tool name. The
   scoping record gives a place to look; the format change is a separate design.
2. **Budget per profile or per turn.** §4.3 fixes a budget per profile. The shortlist-size paper
   argues it should vary per query, and the record will show whether a fixed budget is where
   the misses come from.
3. **Who owns the sidecar for synced skills.** The sync script's substitution mechanism is the
   precedent; whether the metadata belongs there or beside the skills is a question for the
   people who run the sync.
4. **The context-engine slot.** If another design claims it, the skills injection stays on the
   user-message channel; if not, moving the index into the system message per request is the
   better placement and should be tried in phase 1.
5. **Plugin-registered skills.** They are absent from the index today by upstream design and
   reachable only by an explicit prefixed name. The catalogue should list them; whether they may
   enter a working set uninvited is a question for the plugin authors.

## Related

- [`memory.md`](memory.md): the per-turn retrieval precedent, its injection channel, and the A/B
  method this design reuses.
- [`spec-subagent-profiles.md`](spec-subagent-profiles.md) and #1719: the static profile layer
  this design assumes underneath it.
- [`agent-communication.md`](agent-communication.md): delegation, which stage 2 narrows.
- [`domains.yaml`](domains.yaml): the domain vocabulary the catalogue metadata reuses.
- [`testing-strategy.md`](testing-strategy.md) and
  [`../../.agents/rules/eval_driven_development.md`](../../.agents/rules/eval_driven_development.md):
  the loop phase 1 runs under.
- Anthropic, tool search tool documentation and "Advanced tool use" (2025); RAG-MCP,
  arXiv 2505.03275; "How Many Tools Should an LLM Agent See?", arXiv 2605.24660;
  `langgraph-bigtool` for the retrieve-then-bind pattern.
