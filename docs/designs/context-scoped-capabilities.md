# Context-scoped capabilities: exposing the tools and skills a turn needs

> **STATUS — parked.** An experiment found that scoping tools and skills per turn does not pay
> for its complexity on the catalogue that ships today. Nothing here is enabled on any install.
> §3 says what to do instead and which measurements reopen the design.

**Status:** Parked after one experiment. **Scope:** every agent profile this repository ships
(chat, platform, cluster, a2a specialists) and the custom harness that will run them.

## Summary

An agent sees its whole capability catalogue on every turn: every tool schema in the request and
every skill in a system-prompt index. The Platform Agent's catalogue is 44 skills and about 60
tools, and it grows without a gate: 29 of the skills are synced from an upstream repository, and
every new MCP server adds its tools to the same list. Published measurements put the point where
tool selection starts to degrade at 30 to 50 tools. This document proposed a scoping layer that
decides, per turn, which capabilities the model sees in full, which by name only, and which it
must search for.

The design is parked because the experiment in §2 does not justify it for today's catalogue:

- **With the 44 skills that ship,** scoping cut the runs where the agent read no skill from 35% to
  21%. Its gain in reading the right skill first, from 62% to 72%, is not statistically
  significant.
- **With 104 skills,** scoping raised right-skill-first from 59% to 76%, significant against both
  alternatives. That size is not where the catalogue is today.
- **The failure on this profile is under-use,** an agent skipping skills. It is not an agent
  picking the wrong skill. Wrong picks were rare in every arm.
- **When the ranker misses, scoping does worse than no scoping.** The ranker's recall decides the
  outcome, and the lexical ranker missed a quarter of the probes.
- **Scoping saves no tokens on this profile,** because the skill index is under a thousand
  tokens of a 28k-token prompt. It adds a ranker, per-session state, and a new failure mode.
- **Picks were scored, not answers.** The run does not show that a better pick produced a better
  outcome.

Replacing the lexical ranker with hybrid semantic search could raise right-skill-first under
scoping to about 80 to 90% (§2.7). That estimate is unmeasured, and cheaper fixes aimed at
under-use have not been tried. §3.2 lists them; §3.3 lists the measurements that reopen the
design. §4 keeps the design as proposed, for the day it is revisited.

## How to read this document

§1 states the problem as it was posed. §2 is what the experiment measured, and the estimate for a
semantic ranker. §3 is the decision to park, what to do instead, and when to revisit. §4 is the
design as proposed, kept for reference. §5 is what the custom harness would provide if the design
is revived. §6 lists what is unresolved.

## 1. The problem as posed

### 1.1 What the model sees today

| Profile  | Skills in the index | Tools in the request                                                                                            | Where the catalogue comes from                                                |
| -------- | ------------------- | --------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| platform | 44                  | ~40 core tools, 3 search-bridge tools, 3 memory tools, 14 kanban tools; 43 MCP tools deferred behind the bridge | `agents/platform/skills/` (29 synced from `google/skills`), three MCP servers |
| cluster  | 7                   | ~40 core tools, 3 bridge tools; GKE and developer-knowledge MCP tools deferred                                  | `agents/cluster/`, scaffolded per cluster                                     |
| chat     | 0 (skills disabled) | router, kanban, memory                                                                                          | `agents/chat/config.yaml`, a deliberate lockdown                              |

Under the Platform Agent's API endpoint, where §2 measured, the platform profile's request carried
21 tools. The skill bodies total 465 KB. The index the model reads is one line per skill, and
each description is cut to 60 characters. Every one of the platform profile's 44 descriptions is
longer than that: the shortest is 111 characters and the median about 350. So every line the
model chooses from ends in an ellipsis.

Three static levers exist and are used: a per-profile toolset allowlist, a per-platform toolset
denylist, and per-task skill lists on cron jobs (9 of the 16 carry one) and kanban cards. All
three decide before the question exists. Nothing decides per turn.

### 1.2 Why the size was expected to matter

The published evidence that a large catalogue costs accuracy, not only tokens:

- Anthropic's tool-search documentation states that selection accuracy "degrades once you
  exceed 30–50 available tools", and reports gains on an MCP evaluation with search enabled:
  Opus 4 from 49% to 74% and Opus 4.5 from 79.5% to 88.1%.
- RAG-MCP (arXiv 2505.03275) reports selection accuracy rising from 13.62% to 43.13% when a
  retriever narrows the catalogue before the model sees it, with prompt tokens halved.
- "How Many Tools Should an LLM Agent See?" (arXiv 2605.24660) shows the right shortlist size
  varies per query, and that a fixed shortlist is wrong in both directions.

A skill index is a selection problem with the same shape. The platform persona
([`agents/platform/SOUL.md`](../../agents/platform/SOUL.md), "Authorized Commits & Change Flow")
names which of two skills owns the write path, and a bench case
([`bench/tasks/rca-remediation-pr/task.yaml`](../../bench/tasks/rca-remediation-pr/task.yaml))
spells out that remediation goes through `submit-suggestion` and not `github-issue-resolver`.
Neither file is an incident record: no run in this repository is documented as having picked the
wrong skill from the index. Those published numbers come from catalogues of hundreds to
thousands of tools; §2 measured at 44 and 104 skills.

### 1.3 Hiding is the other failure

Deferral is not free. Two incidents in this repository show what happens when a capability is
hidden to shorten the list:

- In [#1703](https://github.com/gke-labs/kube-agents/issues/1703) (delegation broken on a
  kustomize dev install) the front-door agent's `kanban_create` was absent in 25 of 32 runs. A
  search for "kanban" returned nothing, and the model reported that only the four bridge tools
  existed.
- In [#1699](https://github.com/gke-labs/kube-agents/issues/1699) (the front-door agent patched a
  Deployment through the hosted GKE MCP server on the ambient credential) a search for
  `kanban_create` returned `mcp__gke__create_cluster`, and the agent went on to use the GKE tools.

So the target was never the smallest working set. It was the working set that contains what the
turn needs, plus a discovery path the model actually takes when it does not. The experiment
confirmed that path matters: under scoping, a quarter to a third of skill loads were of skills
the ranker had left out.

## 2. What the experiment showed

The experiment ran once, on 2026-09-21, against a dedicated install with the Platform Agent as the
front door. The current agent runtime served only as the test driver: a prototype plugin ranked
the catalogue and injected the result, and nothing in it is a proposal for that runtime. The
method, code and probes are in
[`bench/experiments/capability-scope-ab/`](../../bench/experiments/capability-scope-ab/README.md),
and the scored tables, one row per run, are under its `results/` directory. The raw transcripts
stayed on the machine that ran them. Every number below is in those files.

### 2.1 What was run

Three arms and two catalogue sizes, 22 probes, three repetitions each, one prompt per session,
the same image and model throughout:

| Arm        | Skill index in the system prompt              | Per turn                                           |
| ---------- | --------------------------------------------- | -------------------------------------------------- |
| `stock`    | every skill, description cut at 60 characters | nothing                                            |
| `fulldesc` | every skill, full description                 | nothing                                            |
| `scoped`   | every skill by name only                      | top six skills by a BM25 ranker, full descriptions |

`fulldesc` is the cheapest fix in §4.9. `scoped` is the design's turn scope; it also filtered the
tool array to a pinned set plus six. The experiment files call it `scoped-all`. The 44-skill
catalogue is the one in the repository. The 104-skill catalogue adds 60 real skills from the
upstream repository the sync reads, five of which the next sync will pull in.

Twenty probes each ask for something one skill is written for, the probe's gold skill. Two are
controls, where reading any skill is wrong. Each run was capped at ten model iterations and
prefixed with one sentence keeping it on the local cluster. The model was Gemini 3.1 Pro; §2.5
covers the repeat on Gemini 3.5 Flash.

The ranker is BM25, the lexical scoring classic search engines use, over each skill's name and
description, with a crude suffix stemmer and no model call. Offline, before any model saw it, it
put the gold skill in its top six for 16 of 20 probes on the 44-skill catalogue and 15 of 20 on
the 104-skill one. It put the gold skill first for 13 and 10. The driver's framing sentence was
part of the text the ranker scored. With it, the gold skill stayed in the top six for 15 of 20
but came first for only 5, because the framing words outweigh a short ask in a lexical score.

### 2.2 Selection

The agent reads a skill by calling a tool, so each run is scored by the first skill it read: the
gold skill, an acceptable alternative listed in the probe, a wrong skill, or none at all. Each
column is 60 runs: 20 probes, three times each. The control probes are scored in the last row.

| Share of runs where the agent...          | stock  | fulldesc | scoped | stock  | fulldesc | scoped |
| ----------------------------------------- | ------ | -------- | ------ | ------ | -------- | ------ |
| skills in the catalogue                   | 44     | 44       | 44     | 104    | 104      | 104    |
| read the gold skill first                 | 55.0%  | 60.0%    | 68.3%  | 53.3%  | 58.3%    | 78.3%  |
| read an acceptable alternative first      | 1.7%   | 0.0%     | 1.7%   | 0.0%   | 8.3%     | 5.0%   |
| read a wrong skill first                  | 1.7%   | 0.0%     | 3.3%   | 1.7%   | 3.3%     | 0.0%   |
| read no skill                             | 41.7%  | 40.0%    | 26.7%  | 45.0%  | 30.0%    | 16.7%  |
| read the gold skill at any point          | 56.7%  | 60.0%    | 71.7%  | 53.3%  | 61.7%    | 78.3%  |
| read any skill on a control probe (count) | 0 of 6 | 0 of 6   | 0 of 6 | 0 of 6 | 0 of 6   | 0 of 6 |

With 104 skills, the rise in gold-skill-first under `scoped` is significant against both other
arms (two-proportion test: p = 0.004 against `stock`, p = 0.019 against `fulldesc`). So is the
drop in runs that read no skill (p = 0.001 against `stock`). With 44 skills every movement has
the same direction and about half the size, and none reaches significance at 60 runs (p = 0.13
for gold skill first against `stock`). Full descriptions alone move gold skill first by five
points on both catalogues. Wrong picks are rare in every arm: the failure is no skill, not the
wrong skill.

Three details from the per-probe table qualify the averages:

- The repository-inspection probe went from three gold picks under `stock` to none under
  `scoped` with 44 skills. The ranker left that skill out of its top six, and the names-only list
  did not bring the model to it.
- Under `scoped`, a quarter to a third of all skill loads were of skills outside the injected six
  (25 of 78 with 44 skills, 20 of 82 with 104). The names-only list carried them.
- Under `stock` with 104 skills, one run loaded a skill that does not exist, a plausible name
  assembled from its neighbours in the list. No scoped run did.

### 2.3 Cost

| Per run, medians                | stock | fulldesc | scoped | stock | fulldesc | scoped |
| ------------------------------- | ----- | -------- | ------ | ----- | -------- | ------ |
| skills in the catalogue         | 44    | 44       | 44     | 104   | 104      | 104    |
| prompt tokens, first model call | 28.4k | 31.3k    | 28.3k  | 30.4k | 38.6k    | 29.7k  |
| input tokens, whole run         | 309k  | 328k     | 306k   | 318k  | 399k     | 321k   |
| cache-read share of input       | 92%   | 94%      | 88%    | 93%   | 93%      | 88%    |
| wall seconds                    | 70    | 120      | 74     | 77    | 129      | 92     |
| seconds to first tool call      | 8.7   | 15.5     | 7.3    | 8.4   | 17.9     | 7.5    |

The prompt is about 28k tokens on every arm because it is mostly the persona and standing
instructions. The skill index at 60 characters a line is under a thousand tokens of that. The
filter hid exactly one of the 21 tools on every recorded turn, and the MCP tools were already
behind a search tool. So scoping costs what `stock` costs and saves nothing on this profile. Full
descriptions for every skill add 3k tokens a call with 44 skills and 8k with 104, and double the
time to the first tool call.

### 2.4 The ranker decides the outcome

The run's per-probe rows split cleanly by whether the ranker, scoring the prefixed text, put the
gold skill in its top six.

| `scoped`, share of runs reading the gold skill first | ranker found it (15 probes) | ranker missed it (5 probes) |
| ---------------------------------------------------- | --------------------------- | --------------------------- |
| Gemini 3.1 Pro, 44 skills                            | 87%                         | 13%                         |
| Gemini 3.1 Pro, 104 skills                           | 96%                         | 27%                         |
| Gemini 3.5 Flash, 44 skills                          | 98%                         | 13%                         |
| Gemini 3.5 Flash, 104 skills                         | 98%                         | 0%                          |

The 104-skill rows reuse the 44-skill catalogue's list of misses, so they are approximate. When
the ranker surfaces the right skill, the model almost always takes it. When the ranker misses,
`scoped` does worse than `stock` did on the same probes, which was 27 to 47%. Scoping is only as
good as its ranker's recall.

### 2.5 The repeat on Gemini 3.5 Flash

The same six cells were re-run on Gemini 3.5 Flash, the model production answers with today,
three hours after the first matrix. Scored the same way as §2.2.

| Flash: share of runs where the agent...   | stock  | fulldesc | scoped | stock  | fulldesc | scoped |
| ----------------------------------------- | ------ | -------- | ------ | ------ | -------- | ------ |
| skills in the catalogue                   | 44     | 44       | 44     | 104    | 104      | 104    |
| read the gold skill first                 | 70.0%  | 76.7%    | 76.7%  | 65.0%  | 63.3%    | 73.3%  |
| read an acceptable alternative first      | 1.7%   | 3.3%     | 8.3%   | 10.0%  | 13.3%    | 8.3%   |
| read a wrong skill first                  | 0.0%   | 1.7%     | 0.0%   | 1.7%   | 1.7%     | 0.0%   |
| read no skill                             | 28.3%  | 18.3%    | 15.0%  | 23.3%  | 21.7%    | 18.3%  |
| read any skill on a control probe (count) | 0 of 6 | 0 of 6   | 1 of 6 | 0 of 6 | 0 of 6   | 0 of 6 |
| prompt tokens, first model call (median)  | 29.9k  | 32.9k    | 29.8k  | 34.9k  | 40.1k    | 31.2k  |
| seconds to first tool call (median)       | 6.1    | 12.1     | 4.7    | 7.2    | 12.2     | 6.5    |

Flash reads skills more readily than Pro without help, so there is less headroom and every
movement is smaller. None of the Flash differences is significant on its own. The direction is
the same in all four model-and-catalogue comparisons, and pooling the two models gives 120 runs
per arm:

| Both models pooled                | stock | fulldesc | scoped | scoped vs stock | scoped vs fulldesc |
| --------------------------------- | ----- | -------- | ------ | --------------- | ------------------ |
| 44 skills, read gold skill first  | 62.5% | 68.3%    | 72.5%  | p = 0.10        | p = 0.48           |
| 44 skills, read no skill          | 35.0% | 29.2%    | 20.8%  | p = 0.014       | p = 0.14           |
| 104 skills, read gold skill first | 59.2% | 60.8%    | 75.8%  | p = 0.006       | p = 0.013          |
| 104 skills, read no skill         | 34.2% | 25.8%    | 17.5%  | p = 0.003       | p = 0.12           |

With the catalogue as shipped, scoping's one reliable effect is that the agent skips skills less
often. With a catalogue more than twice the size, scoping improves the pick by about a sixth
against either alternative, and full descriptions alone do nothing for it.

### 2.6 What the run does not show

- **Whether the pick produced a better outcome.** The probes were scored on the pick, not on the
  answer.
- **Multi-turn behaviour.** Every probe was one turn. The prototype as run cleared its state at
  the end of every message, so the sticky working set of §4.5 is untested.
- **Tool scoping.** The filter hid one tool of 21, so the run says nothing about the 30-to-50
  tool threshold the literature reports.
- **A ranker with better metadata or a semantic model.** §2.7 estimates the second.
- **Cheaper fixes for under-use.** No arm tried better descriptions or a persona instruction to
  check the skill list first.

### 2.7 Estimate: semantic search instead of BM25

This section is an estimate from the run's data and published benchmarks. Nothing in it was run.

**What BM25 missed.** All five probes the ranker missed are vocabulary mismatches:

- "The PersistentVolumeClaim ... has been Pending" did not match the storage skill, whose
  description says "PVCs".
- "Our GKE bill grew thirty percent" did not match "optimizes GKE costs".
- "Pods get 403 permission denied when they read from a Cloud Storage bucket" is a workload
  identity problem. Its skill describes security controls and never uses the user's words.
- "What is in the deploy directory of github.com/..." did not match "read and analyze the source
  of any GitHub repository", because the URL stayed one token.
- The node-pool scale-up probe fell out of the top six only when the framing sentence was added.

An embedding model matches meaning, not words, and plausibly fixes four of the five. The
workload-identity probe is the likeliest to stay a miss. That would put the gold skill in the top
six for 19 of 20 probes.

**Projected effect.** If the newly found probes behaved like the ones the ranker already found
(§2.4), gold-skill-first under `scoped` would reach 83 to 94%, against 68 to 78% measured. That
is an upper bound: the probes BM25 missed are also harder for the model. Discounting for that,
the realistic range is 8 to 15 points above the measured `scoped` arm, or about 80 to 90%. The
runs that read no skill would fall too, since most of them came from probes the ranker missed.

**What published work says.**

- Off-the-shelf embeddings are not automatically better. On ToolRet, a benchmark of 43,000
  tools, BM25 matched or beat common embedding models such as ColBERT, Contriever and e5 (nDCG@10
  of about 36 for BM25). Newer and tool-tuned embedders win clearly: about 43 for
  Qwen3-Embedding-0.6B and about 52 for a tool-tuned 4B model.
- Hybrid search, which combines BM25 with embeddings, wins at scale. On about 2,800 tools one
  vendor reports 94% correct selection for hybrid search against 34% for BM25 alone; the vendor
  sells the product it tested. An independent test found BM25 tool search finding the right tool
  for 16 of 25 tasks on about 4,000 tools.
- Rewriting tool descriptions helps every ranker: an LLM-expanded version of ToolRet lifted BM25
  and dense retrievers alike by 2 to 4 points.
- Small catalogues narrow the gap. At 30 to 100 tools, BM25 already reaches high top-five
  accuracy, which matches the 16 of 20 measured here.

**What this means for the decision.** If the estimate holds, scoping with a hybrid ranker would
be a real gain over `stock`: about 20 points of gold skill first on the catalogue as shipped. It
does not reverse the decision in §3, for two reasons. The estimate is unmeasured, and the probes
were written by someone who knew the skills, which flatters any ranker. And the fair baseline is
not `stock` but the cheap fixes in §3.2, which target the same under-use and have not been tried.
A future experiment should run a hybrid-ranker arm against those fixes, not against `stock`.

## 3. Decision: park the design

### 3.1 Why scoping does not pay now

- **The gain is modest where the catalogue is.** At 44 skills the only significant effect is
  fewer skipped skills. The pick improves by ten points, not significantly.
- **The problem it would solve is not the problem measured.** Scoping targets a crowded list
  that confuses the model. The measured failure is the agent not reading a skill at all, which
  cheaper changes can target directly.
- **It adds a failure mode.** A ranker miss makes the agent worse than no scoping, and the
  lexical ranker missed a quarter of the probes.
- **It saves no tokens here.** The index is a small part of the prompt, and the tool array is
  already short under the API endpoint.
- **It adds moving parts.** A catalogue build, a ranker with its own golden tests, per-session
  working-set state, and a record to maintain, for an effect that is significant only on a
  catalogue twice today's size.

### 3.2 What to do instead

1. **Rewrite skill descriptions for selection.** Lead each description with the words a user
   would use ("my pods keep restarting", "the bill went up") and what the skill is not for. This
   helps every chooser, the model's and any future ranker's. For the synced skills, propose the
   change upstream.
2. **Tell the agent to check its skills.** One persona line asking the agent to scan the skill
   list before investigating targets the skipped-skill failure directly. It is untested.
3. **Measure capability use in the custom harness.** Record which skill and tool each turn used,
   so under-use and wrong use are visible in production traffic, not only in an experiment. This
   is the scoping record of §4.8 without the scoping, and it is what reopens this design.

### 3.3 When to revisit

Reopen the design when any of these holds:

- the platform catalogue passes about 80 skills, the size between the two measured points;
- the tools in a request pass 30, the threshold the literature reports, or tool schemas pass
  about 20% of the prompt;
- the capability-use record shows under-use or wrong use that the fixes in §3.2 did not close.

A revived design starts from a hybrid ranker (§2.7), scores only the user's own text and not
text the harness prepends, and keeps the names-only list for everything outside the working set.

## 4. The design as proposed

This section is the design as it stood before the experiment, kept so a revival does not start
from nothing. It borrows its shape from the memory design that runs on the chat profile: stop
injecting the corpus and start searching it, pay for the corpus once at build time, and decide by
measurement. It differs in one respect: a memory the ranker misses is a fact the agent lacks,
while a capability the ranker misses is an action the agent substitutes for, so the working set
is bounded below as carefully as above.

### 4.1 Goals and non-goals

Goals:

1. Per turn, present the model with a working set of capabilities sized to the turn, with every
   capability it is required to use present in full.
2. Keep every capability discoverable from inside a turn, and measure how often discovery is
   needed.
3. Make the scoping decision observable: for every turn, what was shown, hidden, and used, and
   whether the use was a miss.
4. Hold the model's request prefix stable enough that prompt caching survives scoping.

Non-goals:

- **Authorization.** A hidden capability is not a forbidden one. RBAC, the credential broker,
  and the sandbox remain the only permission boundaries.
- **Rewriting skill content.** The layer reads catalogue metadata. Improving a description is a
  separate, cheaper change, and §3.2 recommends it first.
- **Choosing the profile.** Which specialist a task goes to is the dispatcher's job. This layer
  scopes inside a profile once the task has arrived.
- **A router model.** No second model call decides what the first one sees.

### 4.2 Vocabulary

- **Capability**: a tool (a callable with a schema) or a skill (a procedure the model loads into
  context).
- **Catalogue**: every capability a profile may ever use. Static per profile; built once.
- **Scope**: the subset of the catalogue eligible for a session or task. Deterministic.
- **Working set**: the subset of the scope the model sees in full on a given turn.
- **Shelf**: the part of the scope the model sees by name only.
- **Scoping signal**: the text the ranker scores: the user's message, the task assignment, and
  the names of capabilities used in recent turns.
- **Miss**: a turn in which the model used, or searched for, a capability that was in scope and
  not in the working set.
- **Pinned**: a capability that is in every working set of its profile.

### 4.3 Three stages

**Stage 1, profile scope, at build time.** The catalogue is compiled from the profile directory
and its MCP manifests into one indexed artifact: name, full description, trigger phrases, domain
tags, risk class, token cost, and the `pinned` flag. Owner: the profile author.

**Stage 2, task scope, at session or assignment start.** The entry point declares what the task
is for: a kanban card's skill list, a cron job's `skills:` field, a dispatcher's domain tag. The
task scope is the catalogue filtered by those declarations plus the pinned set. A child agent
inherits the parent's task scope, not its catalogue. Owner: the caller that created the task.

**Stage 3, turn scope, at every turn boundary.** The ranker scores the task scope against the
scoping signal, and the working-set policy (§4.5) decides what enters, stays, and leaves. The
output is the working set, the shelf, and a scoping record. The experiment in §2 tested this
stage alone.

### 4.4 Interfaces

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

The ranker makes no generative model call and reads nothing but catalogue metadata and the
signal. A ranker that calls a model doubles latency to first action, and a ranker that reads the
cluster can be steered by what it reads. The synced skills cannot carry metadata in their
`SKILL.md`, because the sync overwrites them, so their entries live in a sidecar file the sync
does not touch. Domain tags reuse the slugs in `docs/designs/domains.yaml`.

### 4.5 Working-set policy

1. **Pinned capabilities are always in the working set, in full.** The core tools (read a file,
   run a command, load a skill, search the catalogue) and whatever the profile author pins.
2. **The shelf is never empty.** Every in-scope capability the working set omits appears by
   name, grouped by domain. A capability that is nowhere is one the model substitutes for; both
   incidents in §1.3 are this. §2.2 measured the shelf carrying a quarter to a third of loads.
3. **This turn's ranking always enters; earlier entries are carried, then leave by disuse.**
   Entries from earlier turns stay while younger than five turns, up to as many again as the
   fresh budget, so the working set is at most twice the budget.
4. **Changes land at turn boundaries only.** The working set for a turn is fixed before its first
   model call. A mid-turn discovery enters next turn and is recorded as a miss.
5. **The budget is a token budget with count ceilings.** Starting point: skills in full up to 8
   and 2,000 tokens; tools in full up to 25 and 8,000 tokens, both including the pinned set.

### 4.6 Ranker

The proposal was lexical first: BM25 over name, description, triggers, and argument names, with
a bonus for a domain-tag match and a penalty for an anti-trigger match. It is deterministic,
needs no network, and can be unit-tested against a golden set of (signal, expected top-k) pairs.
The experiment showed that lexical matching misses on vocabulary (§2.7). A revival should start
with a hybrid ranker: embeddings computed for the catalogue at build time, one embedding call per
turn for the signal, and scores combined with BM25 so exact names like "vLLM" or "L4" still
count. With a pinned embedding model the ranking stays reproducible in tests.

### 4.7 Injection

Tools in the working set go into the request's tool array; the shelf goes into a names-by-domain
list on the search tool's description. Skills in the working set are rendered with full
descriptions; the shelf is a names-only block. Per-turn content stays out of the system prompt so
the cached prefix is stable. Skill bodies still load on demand.

### 4.8 The scoping record

Emitted per turn, the record lets the miss rate, the substitution rate, and the token split be
computed from logs without a bench run. It carries only a hash of the user's text, so it can be
retained at the same tier as the tool-call audit. §3.2 recommends building its "used" half now.

### 4.9 Alternatives

| Option                                             | What it fixes                           | What it costs                                                                                            | Verdict after the experiment                                  |
| -------------------------------------------------- | --------------------------------------- | -------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------- |
| A. Status quo: prose routing, everything visible   | Nothing; baseline                       | Grows with every synced skill                                                                            | Keep, with the fixes in §3.2                                  |
| B. More, narrower static profiles                  | The dispatcher picks a small catalogue  | A profile per domain multiplies personas; a task spanning two domains still needs both catalogues        | Keep; it bounds growth without a ranker                       |
| C. Model-invoked search only                       | Token cost; nothing visible until asked | The two incidents in §1.3: a search the model does not think to make is a capability that does not exist | Keep for MCP tools, as today                                  |
| D. Automatic per-turn retrieval (bigtool, RAG-MCP) | Selection accuracy and tokens           | Retrieval misses; churn; a ranker to maintain                                                            | This design; parked                                           |
| E. Two-stage router model                          | Accuracy, at any catalogue size         | A model call before every turn; the router's errors are invisible to the executor                        | Rejected for latency and opacity                              |
| F. Provider-side deferral (`defer_loading`)        | Everything in D, with caching handled   | One provider only; this repository reaches every provider through an OpenAI-compatible proxy             | Unavailable on this path                                      |
| G. Full descriptions in the index                  | Chooser quality for the current index   | Measured: 3k to 8k tokens a call, twice the time to first tool call, five points of gold skill first     | Rejected on cost; rewrite descriptions instead of lengthening |

### 4.10 Failure modes and their bounds

**Retrieval miss.** The ranker leaves out the capability the turn needs. Bounded by the shelf,
the pinned set, and the search tool. §2.4 measured what an unbounded miss costs.

**Substitution.** The model, lacking a capability, uses another to the same end, as in #1699.
The record cannot see it directly, because nothing was searched for. Bounded by the shelf and by
risk class: a write-class capability never surfaces as a search result for a read-class query
unless the query names it.

**Churn.** The working set changes turn to turn, and with it the request. Bounded by the sticky
policy and the turn-boundary rule.

**Cache invalidation.** Most providers cache a prefix that includes the tool array, so a per-turn
change to it costs the cache. The design changes the tool array only at turn boundaries and only
when the working set changes. §2.3 measured the cache-read share falling from about 93% to 88%
under `scoped`.

**Scoping mistaken for authorization.** "Not in the working set" never means "cannot be
called": the shelf and the search tool exist, and a tool the model names by guess may still
resolve.

**Injection through the signal.** The ranker reads user text as a query, executes nothing, and
records only a hash. A user who names a capability gets it ranked higher, which is intended.

### 4.11 Rollout, if revived

Measure before enforcing: emit the record with enforcement off, and enforce only if the
would-be miss rate is under 5%. The experiment's `stock` arms made that measurement: 16 of 50
skill loads with 44 skills and 28 of 58 with 104 fell outside the ranker's top six, far past the
threshold. So a revival also starts with a better ranker. Then enforce on the platform profile
behind a switch and run the active bench matrix at three repetitions per arm, requiring no
regression on the blocking roster.

## 5. What the custom harness would provide

If the design is revived, the custom harness provides the interfaces of §4.4 as request-assembly
steps, not as add-ons:

- the catalogue is an input to the agent's construction, not a directory walk at prompt time;
- the tool array and the skill index for a request come from one call to the working-set policy,
  and the request assembler places them in the volatile part of the request;
- delegation takes a scope, and the child's catalogue is that scope;
- the scoping record is emitted by the assembler.

Only the first and last are worth building now: a compiled catalogue and a capability-use record
cost little and make the revisit triggers in §3.3 measurable.

## 6. Open questions

1. **Bench verifier for capability use.** Wrong-capability incidents are detectable only by a
   verifier that asserts which skill or tool produced the outcome. The case format has no such
   field.
2. **Outcome, not pick.** No measurement here connects a better pick to a better answer. A
   revival needs cases whose verifiers depend on the skill's procedure.
3. **Budget per profile or per turn.** The shortlist-size paper argues the budget should vary per
   query.
4. **Who owns the sidecar for synced skills.** The sync script's substitution mechanism is the
   precedent; whether the metadata belongs there or beside the skills is for the people who run
   the sync.

## Related

- [`memory.md`](memory.md): the per-turn retrieval precedent and the A/B method this experiment
  reused.
- [`spec-subagent-profiles.md`](spec-subagent-profiles.md): the static profile layer, option B.
- [`agent-communication.md`](agent-communication.md): delegation, which stage 2 narrows.
- [`domains.yaml`](domains.yaml): the domain vocabulary the catalogue metadata reuses.
- Anthropic, [tool search tool documentation](https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-search-tool);
  RAG-MCP, arXiv 2505.03275; "How Many Tools Should an LLM Agent See?", arXiv 2605.24660.
- [Retrieval Models Aren't Tool-Savvy (ToolRet)](https://arxiv.org/html/2503.01763);
  [Tools are under-documented: document expansion boosts tool retrieval](https://arxiv.org/html/2510.22670v1);
  [Stacklok MCP Optimizer vs Anthropic Tool Search Tool](https://stacklok.com/blog/stackloks-mcp-optimizer-vs-anthropics-tool-search-tool-a-head-to-head-comparison/);
  [Beyond BM25: the future of MCP tool discovery](https://mcpproxy.app/blog/2026-03-15-beyond-bm25-tool-discovery/).
