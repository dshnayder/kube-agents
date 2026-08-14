# Scheduled-report relay: the specialist reasons, the Chat Agent speaks

**Status:** implemented, and every job on
[the Platform Agent's roster](../../agents/platform/cron/README.md) delivers this
way. Not yet validated against a live cluster.

## The problem

A watchdog on a named profile runs with the right brain and no voice.

`profile-cron-tick` fires the specialist's job under its own `HERMES_HOME`, so it
keeps that profile's persona, skills, model and turn budget — which is the whole
reason the watchdogs live on the Platform Agent's roster rather than as kanban
cards. What it does not get is a way to say anything useful to the person who
cares.

The report arrives as a monologue from a process that has now exited. A user who
replies "why does that matter?" is talking to the Chat Agent, which never saw the
finding, and there is nothing it can do but apologise.

Getting the words into a channel _at all_ was a second and separate gap, now
closed: `HOME_TARGET_ENV_KEYS` in `profile_cron_tick.py` named the Slack keys and
nothing else, so on a Google Chat install the child resolved `deliver: "all"` to
an empty target list and posted nowhere. It names both platforms now, and
`home_target_env`'s docstring is where that story lives. Closing it does not
close this one: a channel id makes the message appear and still leaves the
follow-up unanswerable.

## The shape of the fix

Separating who reasons (the specialist) from who speaks (the Chat Agent).

The specialist keeps the work: it runs on its own schedule, in its own profile,
with its own tools, and produces a finished report. It does not deliver that
report. It hands it to the Chat Agent, which presents it in the channel and
therefore owns the conversation that follows.

The trigger is the job's own delivery setting: `deliver: "chat"`.

```
chat roster: profile-cron-tick  (no_agent, * * * * *)
        │
        └── hermes cron tick, HERMES_HOME=<profile>
                 │
                 └── specialist cron job — does the work
                          │
                          │  scheduler: _deliver_result(job, final_response)
                          │  deliver == "chat" → relay instead of resolving targets
                          ▼
                 POST /v1/cron-reports        (Session KV server, loopback 8699)
                          │
                          ├── POST /api/sessions/{sid}/chat   → the Chat Agent's turn
                          │        message = the report, system_message = relay instructions
                          │
                          ├── hermes send                     → what the Chat Agent composed
                          │
                          └── INSERT INTO incidents (chat_id, thread_id, report)
                                       │
                          user replies in the thread
                                       ▼
                          incident_context prepends the report → the Chat Agent has it
```

This is the event watcher's delivery path with the reasoning step removed. There,
an out-of-band signal starts an agent turn that investigates and reports; here
the investigation already happened and the turn only presents. The three pieces
that make an alert answerable — a thread, a session bound to it, and the report
stored against that thread — are reused unchanged.

## Why the Chat Agent composes but does not send

The Chat Agent cannot post to a chat platform out of band. Its toolset is
`mcp-router`, `kanban` and `memory`; `terminal` is on its denylist, and
`agents/chat/config.yaml` calls that denylist "the authoritative guarantee that
the front door cannot touch the system."

So the relay reads the turn's response body — `POST /api/sessions/{id}/chat`
returns `{"message": {"role": "assistant", "content": …}}` — and sends that text
itself. The alternative, giving the Chat Agent a send tool, would widen exactly
the boundary that file exists to hold, and buy nothing: the voice and the context
are the Chat Agent's either way.

The relay instruction goes in `system_message`, which the gateway applies as an
`ephemeral_system_prompt`. It steers this turn without being replayed into every
later turn of the thread, so a follow-up reaches a Chat Agent that remembers the
report and not the order to repeat it.

## Why context works without an append-only endpoint

There is no way to put a message into a Hermes session's history without running
a turn: `/chat` always infers, `PATCH /api/sessions/{id}` accepts only `title`
and `end_reason`, and `/messages` is GET-only.

It turns out not to matter, because session history is not what makes the event
watcher's alerts answerable. `incident_context`
(`agents/platform/plugins/incident_context/__init__.py`) is a
`pre_gateway_dispatch` hook: on every inbound message it looks up
`(chat_id, thread_id)` in the `incidents` table and, on a hit, prepends the
stored report to the user's words before the agent sees them. Context comes from
the thread, not the session.

The relay writes that row itself rather than calling `POST /v1/incidents` over
loopback — it is that endpoint's own server, so an HTTP call to itself inside a
background task would only add a way to fail.

## Session lifetime: one per job, per UTC day

One session per _report_ — what the watcher's `per-incident` mode does — gives a
daily watchdog a new thread every tick, so a follow-up lands in a session that
has seen one message. One session per _job_, kept forever, is the opposite
failure: every turn replays the whole history, so a job on a five-minute
schedule grows an unbounded prompt and relaying report N costs proportionally to
N.

`cron-<profile>-<job_id>-<YYYYMMDD>` sits between them. Consecutive reports from
one job share a thread, so the Chat Agent can see it is the third time today;
the history resets before it can grow without bound. Yesterday's thread does not
go dark at the rollover, because `incident_context` resolves a reply by thread
rather than by session id, and those rows live for `CLEANUP_TTL_DAYS`.

## Why not a flag on `/sessions/{id}/inject`

That route is an incident path. It classifies severity, spends `alert_quota`, and
hands the agent the triage template. A scheduled report is not an incident, and
it should not be silently dropped because a node storm spent the day's Warning
budget — the suppression there is deliberately invisible in chat, which is right
for alert volume and wrong for a watchdog that runs once a day.

`/v1/cron-reports` is therefore its own route with no severity and no quota. It
caps report length instead (`CRON_REPORT_MAX_CHARS`, default 12000), which bounds
the accident that route actually has: a job that cats a log into the model and
the channel.

## What a job has to do: nothing

A job relays because of one field. `deliver: "chat"` and no prompt boilerplate —
no instruction to call a tool, no instruction to return `[SILENT]` afterwards.

The reason a prompt contract was rejected is the job nobody writes by hand. A
user asks the Platform Agent to watch something every morning; the agent calls
`cronjob(action='create')` and invents the prompt on the spot. That prompt
carries whatever the agent remembered to put in it. Delivery that depends on a
remembered sentence is delivery that fails on exactly the jobs no reviewer ever
reads. `create_job`'s signature is fixed keywords, so `deliver` is also the only
field such a job _can_ set — which is what makes it the right place for the
switch rather than merely a convenient one.

Making it a mode is free because the scheduler has already done the work by the
time delivery is reached. `run_one_job` applies the `[SILENT]` check and, on a
failed run, substitutes `_summarize_cron_failure_for_delivery(job, error)` —
both _before_ calling `_deliver_result`. So a `deliver: "chat"` job inherits
silence-on-nothing-to-report and audible failures without asking the model for
either.

## How the switch is wired: `chat` is a platform

Nothing in Hermes is patched. Upstream already routes `deliver=<name>` through
the platform registry, and `cron/scheduler.py::_plugin_cron_env_var` says so in
its own words — a plugin that sets `cron_deliver_env_var` gets "cron delivery
support without editing this module". So the relay ships as a bundled platform
plugin, [`deploy/docker/plugins/chat/`](../../deploy/docker/plugins/chat/),
copied into `plugins/platforms/chat/` where Hermes auto-registers it.

It is a platform with no inbound side. The only hook it implements is
`standalone_sender_fn` — the one Hermes calls when cron runs in a separate
process from the gateway, which is exactly what `profile_cron_tick.py` spawns.
`adapter_factory` raises if anything ever tries to start it.

`CHAT_HOME_CHANNEL` is the whole switch, and it is set in one place:
`profile_cron_tick.py`, on the cron children it spawns. It gates enablement
(through `is_connected`) and it is the `cron_deliver_env_var` the scheduler reads
to resolve `deliver: "chat"` to a target. Unset — which is how the gateway
process runs — the enablement pass leaves the platform disabled, no adapter is
ever asked for, and `deliver: "chat"` resolves to nothing.

One thing a plugin manifest is not is documentation. `optional_env` is folded
into `OPTIONAL_ENV_VARS` under the default `category: "messaging"`, and the
subprocess scrub blocklist blocks that bucket whole — so a variable named there
is stripped from every child `build_subprocess_env` spawns, `profile-cron-tick`
included. Declaring `SESSION_KV_API_KEY` made the relay revoke its own
credential: the pod had the key, the gateway had it, and the cron child that
needed it did not. `chat/plugin.yaml` carries the rule and the two names left
out because of it.

The sender is handed the delivery text, not the job. It recovers the job's id and
name from the header `_deliver_result` wraps every cron delivery in, and posts
the report without it. That coupling is the one thing the plugin route
cannot state in code it owns, so it is asserted at image build time:
[`verify_chat_relay.py`](../../deploy/docker/plugins/verify_chat_relay.py) drives
the real `_deliver_result` against a loopback stand-in for the Session KV server
and asserts on what crossed the wire. Upstream changing the wrapper fails the
build.

### What this costs

Being a real delivery target rather than an interception has consequences, and
every one of them is visible to a job author:

- **The token is not exclusive.** `deliver: "chat,slack"` relays _and_ posts to
  Slack. A job that wants one voice names one target.
- **`deliver: "all"` includes the relay**, because `_expand_routing_tokens`
  expands to every platform with a configured home channel and the relay now has
  one. A job left on `all` therefore reports twice — once flat into the channel
  and once through the Chat Agent — on either platform, now that
  `home_target_env` restores the Google Chat channel too. So the whole Platform
  Agent roster names `"chat"` rather than relying on the expansion, which is
  also the only way to say "relay, and do not also post flat" at all: the token
  is additive, so there is no value that subtracts a target from `all`.
  No migration was needed to get there: `deliver` is
  an image-owned key on a named profile, so the entrypoint's existing cron merge
  rewrites it on every live volume at the next pod start (`agents/platform/cron/README.md`
  says which merge and why the default profile is the exception). What that
  cannot reach is a job the agent creates at runtime, which is why `AGENTS.md`
  tells it to pass `deliver='chat'`.
- **A failed relay does not fall back.** It returns an error, which the scheduler
  records in `last_delivery_error`; the report itself is still in the job's saved
  output. Routing a failure to `all` would need the interception this design
  gives up, and it would post the report to a channel the job did not choose.
- **`cronjob(action='create')` calls a relayed job local-only.** `cronjob` runs
  in the gateway, where the switch is off, and `_local_delivery_notice` decides
  by asking whether the job resolves to a target _here_ — so a job the agent
  creates at runtime with `deliver: "chat"` is created correctly and described
  wrongly, with advice to use `deliver: "all"` instead. Taking `all` degrades to
  the right behaviour rather than to silence (the child expands it to the
  relay), which is why this is a wrong sentence and not a lost report. Both
  branches are asserted in `verify_chat_relay.py` so the claim stays measured.

The alternative was to set the switch process-wide and disable the platform in
the root `config.yaml` (`_enabled_explicit` is honoured by the enablement pass).
That fixes the notice and costs more than it buys: the file is runtime state the
agent writes, not something the image owns, and with the switch on in the gateway
every Chat Agent job on `deliver: "all"` starts recording "platform 'chat' not
configured/enabled" against a delivery that in fact succeeded.

`report_to_chat` stays, scoped to what the mode cannot do: reporting mid-run, or
sending something other than the final response.

## Related

- [`agents/platform/cron/README.md`](../../agents/platform/cron/README.md) — the
  roster's own rules, including why no job sets `deliver: "local"` and why an id
  must not appear on two rosters.
- [`agents/platform/docs/session_management.md`](../../agents/platform/docs/session_management.md)
  — the Session KV server, its callers and its auth.
- [`concepts/autonomous-watchdogs`](../site/src/content/docs/concepts/autonomous-watchdogs.md)
  — what fires the schedule.
