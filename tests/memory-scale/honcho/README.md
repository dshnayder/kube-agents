# Honcho under the Meridian bank tests

A second memory backend for the scale harness, deployed alongside the existing
Hindsight one so the same 26 probes can be scored against both. This directory
is the whole deployment; nothing here is part of the product.

## What it is not

Honcho is a **peer-modeling** system, not a document store. It ingests messages
attributed to a peer and an asynchronous **deriver** turns them into
conclusions and a per-peer representation. That means a corpus is not queryable
the moment seeding returns — see [Settle time](#settle-time).

Per-user isolation is deliberately **not** implemented in this experiment, so
the two isolation probes (`Q-ISO-001`, `Q-ISO-002`) are expected to fail.
Honcho does have the primitive for it — the Hermes provider passes
`runtime_user_peer_name` from `user_id`
(`plugins/memory/honcho/__init__.py:477`) — it is just not wired up here.

## Deploy

```sh
kubectl apply -f 00-postgres.yaml -f 01-redis.yaml -f 02-config.yaml \
              -f 03-api.yaml -f 04-deriver.yaml
```

The `honcho-db` Secret is **not** in this directory and must exist first. It
carries three keys, created out of band so no credential lands in the repo:

| key              | used by                                   |
| ---------------- | ----------------------------------------- |
| `password`       | the Postgres StatefulSet                  |
| `connection-uri` | `DB_CONNECTION_URI` on the api + deriver  |
| `llm-api-key`    | `LLM_OPENAI_API_KEY` on the api + deriver |

Apply order matters once: `03-api.yaml`'s entrypoint runs the Alembic
migrations, and the embedding dimension is frozen into the schema at that
point (`src/models.py:35` builds `Vector(_VECTOR_DIM)` at import time). To
change `VECTOR_DIMENSIONS` later you must delete the Postgres PVC and let the
schema be recreated.

## Two things that cost a debugging cycle

**The LLM key env var needs the `LLM_` prefix.** A plain `OPENAI_API_KEY` is
silently ignored: the embedding client falls back to
`settings.LLM.OPENAI_API_KEY` (`src/config.py:529`) and `LLMSettings` declares
`env_prefix="LLM_"` (`src/config.py:749`).

**Every embedding failure is reported as a token-limit error.**
`src/utils/search.py:383-394` wraps the embed call in a bare `except
ValueError` and re-raises it as `"Query exceeds maximum token limit of 8192."`
— for a nine-word query. A missing API key, a dimension mismatch, and a genuine
overflow are indistinguishable from the response. Read the pod log, not the
HTTP body.

## Models

Everything points at the cluster's existing LiteLLM. **Nothing was added to the
LiteLLM config for this experiment** — `gemini-embedding-2` was already served
and honours the OpenAI `dimensions` parameter, which is what allows
`VECTOR_DIMENSIONS = 1536` against a model whose native width is 3072.

Two settings favour Honcho and are stated here so the report can declare them
rather than have a reviewer discover them:

- `deriver.FLUSH_ENABLED = true` bypasses the batch-token gate, so work is
  processed as it arrives instead of waiting up to
  `REPRESENTATION_BATCH_MAX_AGE_SECONDS` (1800) for a partial batch to age out.
  This improves settle time and costs nothing in quality.
- `thinking_effort = "minimal"` on the deriver model. Without it the model
  spends reasoning tokens to answer `ok`.

Dreams (Honcho's consolidation analogue) are left at the shipped defaults:
`MIN_HOURS_BETWEEN_DREAMS = 8` and `IDLE_TIMEOUT_MINUTES = 60` mean they are
unlikely to fire inside a ladder run. Worth stating in the report rather than
silently disabling.

## Settle time

Nothing is scoreable until the deriver drains its queue. A three-message smoke
batch produced `observation_count=3` in one work unit 5s after ingest, with a
2.5s LLM call. At corpus scale this is the dominant cost, which is why the
ladder needs an explicit drain check before scoring rather than a fixed sleep.

## Verified surfaces

All confirmed live against this deployment, with correct semantic ranking:

| surface                                   | returns                                    |
| ----------------------------------------- | ------------------------------------------ |
| `POST /v3/workspaces/{ws}/search`          | raw messages, hybrid semantic + keyword    |
| `POST .../peers/{peer}/search`             | the same, scoped to one peer               |
| `POST .../conclusions/list`                | derived conclusions, paginated             |
| `POST .../conclusions/query`               | derived conclusions, semantic              |
| `POST .../peers/{peer}/representation`     | the assembled representation, as markdown  |
| `POST .../peers/{peer}/chat`               | dialectic — an LLM answer over the above   |
| `GET  .../peers/{peer}/card`               | peer card; `null` until enough messages    |

`conclusions/query` **requires** `observer` and `observed` inside a `filters`
object; without them it 400s rather than searching unfiltered.

The message ingest field on the wire is `peer_id`, not `peer_name`
(`src/schemas/api.py:256` declares `peer_name: str = Field(alias="peer_id")`).

## Image

`ghcr.io/plastic-labs/honcho:latest`, pinned by digest to rev `a92fb1e`,
source version 3.0.12. The published semver tags stop at **v2.0.3** (July
2025), which predates the `/v3` API that the Hermes provider's SDK calls — so
the highest-numbered tag is the wrong choice here.
