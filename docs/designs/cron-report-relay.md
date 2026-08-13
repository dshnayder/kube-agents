# Scheduled-report relay: the specialist reasons, the Chat Agent speaks

**Status:** implemented. Rolled out to one pilot watchdog
(`github-issue-resolver`); the remaining seven jobs on
[the Platform Agent's roster](../../agents/platform/cron/README.md) still deliver
through the scheduler, and converting each is a one-field edit.

## The problem

A watchdog on a named profile runs with the right brain and no voice.

`profile-cron-tick` fires the specialist's job under its own `HERMES_HOME`, so it
keeps that profile's persona, skills, model and turn budget — which is the whole
reason the watchdogs live on the Platform Agent's roster rather than as kanban
cards. What it does not get is a way to say anything useful to the person who
cares. Two separate gaps:

1. **Delivery.** `deliver: "all"` expands at fire time to every platform with a
   configured home channel, and `profile_cron_tick.py` only knows how to hand a
   child profile the Slack channel (`HOME_TARGET_ENV_KEYS` names
   `SLACK_HOME_CHANNEL` and nothing else). On a Google Chat install the child
   resolves `deliver=all` to an empty target list and records "no delivery
   target resolved" while posting nowhere.
2. **Context.** Even where delivery works, the report arrives as a monologue
   from a process that has now exited. A user who replies "why does that
   matter?" is talking to the Chat Agent, which never saw the finding.

The second gap is the interesting one, because it does not go away by fixing the
first. Handing the specialist a Google Chat channel id would make the message
appear and still leave the follow-up unanswerable.

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

The switch is one edit in `_deliver_result`, applied at image build time by
[`deploy/docker/patches/apply_cron_deliver_chat.py`](../../deploy/docker/patches/apply_cron_deliver_chat.py):
before targets are resolved, a job whose `deliver` carries the `chat` token is
relayed and the function returns. The token is exclusive — `"chat,slack"` relays
and does not also post to Slack — because the point is one voice, and Hermes'
own `_resolve_delivery_targets` would otherwise deliver the report twice.

`cronjob`'s `_local_delivery_notice` is patched in the same pass. Left alone it
tells the agent its job "will not be delivered anywhere" and to recreate it with
`deliver='all'` — advice addressed to a model that can act on it, and undo the
mode.

**Failure falls back rather than swallowing.** If the relay is unreachable or
answers non-2xx, the job is delivered with `deliver: "all"` instead: the report
is worth more than the routing. The fallback rewrites a copy — `mark_job_run`
persists the job dict, and a relay outage is not a roster edit. An unexpected
exception takes the same path, because an escape here would be recorded as the
_run_ having failed, which it did not.

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
