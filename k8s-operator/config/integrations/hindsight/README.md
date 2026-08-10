# Hindsight deployment

The long-term memory store behind the Chat Agent's `kube_agents_memory` provider:
a [Hindsight](https://github.com/vectorize-io/hindsight) API server and the
Postgres/pgvector database it keeps its memories in.

Why the design is what it is — one bank, scope tags, the settings the provider
pins — lives in [tag isolation](../../../../docs/designs/memory.md). This
README covers only the manifests in this directory.

## Install

Nothing here is a manual step. Hindsight is
[provisioning step 12](../../../scripts/README.md), so a full install
brings it up along with everything else:

```sh
make -C k8s-operator gcp-provision-13-hindsight   # or the whole provision.sh run
```

The step deploys the manifests and waits for `hindsight-api` to become ready,
which takes longer than the rest of the install because the API loads its
embedding and reranking models at startup.

To apply the manifests directly — the same thing the step does, without the
readiness gate:

```sh
kubectl apply -k k8s-operator/config/integrations/hindsight/ -n kubeagents-system
```

There is nothing to configure and no prerequisite Secret. Both images are pinned
by digest; `ankane/pgvector` publishes only a floating `latest`, so the digest is
the only thing standing between a pod reschedule and a different database engine.
Re-pin deliberately rather than dropping it.

## No credentials

Neither workload reads a Secret, and that is a decision rather than an oversight.

**Postgres takes no password.** `POSTGRES_HOST_AUTH_METHOD=trust` and a DSN with
no password in it. What keeps the database private is a ClusterIP service with no
route in from outside the cluster, and — on clusters that enforce it —
[`networkpolicy.yaml`](networkpolicy.yaml), which lets only the Hindsight API pod
reach port 5432. A password would add an install step (generate it, keep two keys
in agreement, rotate it) to protect an in-cluster database from pods that are
already trusted with the memories it holds.

Note the qualifier on the NetworkPolicy. GKE enforces one only where the cluster
was created with Dataplane V2 or `--enable-network-policy`; everywhere else the
object applies cleanly and does nothing, and any pod in the namespace can open a
connection. That is the same footing as the `litellm` and `github-token-minter`
policies this repo already ships, so it is a property of the cluster to check
rather than of this directory to fix — but check it before treating the policy as
the boundary.

One consequence to know: `POSTGRES_HOST_AUTH_METHOD` is read by `initdb` and
baked into `pg_hba.conf` when the volume is first created. Setting it on a volume
that already exists does nothing — an existing database keeps whatever auth
method it was initialised with, and the passwordless DSN will be rejected. Moving
an existing install onto this manifest means deleting
`data-hindsight-postgresql-0`, which discards every stored memory.

**LiteLLM takes no key either.** The gateway runs without a master key — the
agents authenticate to it with the literal string `none`
([`deploy/shared/defaults/config.yaml`](../../../../deploy/shared/defaults/config.yaml)) and
Hindsight does the same. The value is a placeholder the OpenAI-compatible client
insists on, not a credential. The real provider keys (Gemini, Anthropic, OpenAI)
live in LiteLLM's own secret and never reach this pod.

## What the agent connects to

```
http://hindsight-api.kubeagents-system.svc.cluster.local:8888
```

That URL is baked into `agents/chat/defaults/hindsight/config.json`, which the
container entrypoint force-syncs onto the agent's PVC on every start. The
manifests themselves are namespace-agnostic — every in-cluster address in them is
a short service name, and the provisioning step applies them with `-n $NAMESPACE`
— but the agent image is not. Installing into a different namespace means editing
that file and rebuilding the agent image; a hand-edit on the PVC is overwritten
on the next roll.

There are no banks to provision. A Hindsight bank does not exist until something
writes to it, and the provider creates `kube-agents-memory` with its mission and
retain strategies on the first session that stores anything.

## Teardown

`teardown_13_deploy_hindsight.sh` removes the workloads and **keeps the volume**.
A StatefulSet's `volumeClaimTemplate` PVC is not owned by these manifests, so
re-running the provisioning step reattaches it with every memory intact. Discard
them explicitly:

```sh
kubectl delete pvc data-hindsight-postgresql-0 -n kubeagents-system
```

## Notes

- **No Hugging Face egress.** The API image bakes its embedding and reranking
  models in, and `HF_HUB_OFFLINE` / `TRANSFORMERS_OFFLINE` keep the libraries from
  reaching for the network on a cold start — which would hang, not fail fast,
  where there is no route out of the cluster.
- **LLM calls go through LiteLLM**, the same gateway the agents use, so model
  routing and cost attribution stay in one place. That makes step 9 a
  prerequisite for step 12.
- **The database is the memory.** Deleting the `data-hindsight-postgresql-0` PVC
  discards every stored memory. There is no other copy.
