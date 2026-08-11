# `kube_agents_memory` — the Hindsight-backed memory provider

A thin wrapper around the memory provider Hermes already ships (`plugins/memory/hindsight`).
Everyone's memory lives in one bank, `kube-agents-memory`, and a **scope tag** on every fact is
what separates one person's from another's: `user:<id>` for a private memory, `scope:shared` for
one the whole organisation can read. Recall asks for the current user's tag plus the shared tag,
and nothing else can come back.

Nearly all of the code is there because Hindsight has no way to learn the _current user's id_ —
its `{user}` substitution is wired to `bank_id` alone, so `retain_tags: "user:{user_id}"` tags
every user with the literal characters `user:{user_id}`. The wrapper resolves the identity, then
hands the stock provider the right tags and pins the four settings that would otherwise leak or
silently lose data. The module docstring in [`__init__.py`](__init__.py) names each of those four
and cites the upstream code that makes it necessary; read that before changing any of them.

The stock plugin is **not** forked. It is loaded through `load_memory_provider("hindsight")`, so a
Hermes base-image bump brings its fixes along with no merge to redo.

## Choosing it

This is the default provider, and it is what `install.sh --memory=hindsight` selects. It needs the
in-cluster Hindsight API and its Postgres database, which provisioning step 13 deploys. For a small
or personal install that will not run a database, [`multiuser_memory`](../multiuser_memory/README.md)
is the alternative.

```yaml
spec:
  harness:
    memory:
      provider: kube_agents_memory
```

`memoryEnabled` stays `false`. This provider replaces Hermes' built-in `MEMORY.md`/`USER.md` store
rather than sitting alongside it.

## Where the rest of it is documented

[`docs/designs/memory.md`](../../../../../docs/designs/memory.md) is canonical for the design: why a
single bank rather than one per user, what the specialists get and why it is read-only, and the
measurements behind the default. This file covers only how to work on the directory.
