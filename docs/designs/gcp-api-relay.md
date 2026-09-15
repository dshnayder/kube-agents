# Read-Only Cloud API Relay in the Credential Broker

> **STATUS — design only.** Nothing here is implemented. This document was written to hand
> to the author of the `fleet-audit-collectors` branch, whose cost collector reads Cloud
> Monitoring in-process and cannot do so from the shell sandbox that #913 introduced. It is
> published on a fork branch for that purpose and is not on a path to merge.

## Summary

The Platform Agent's shell, file tools and code execution run in the shell sandbox pod, whose
ServiceAccount carries no cloud identity. Every credentialed call the model's code makes goes
through the credential broker, and the broker speaks one shape: an argv for `kubectl`,
`gcloud`, `gh` or `git`, checked against an allowlist and executed on the broker's side. No
`gcloud` command reads Monitoring time series, so a collector that needs a week of per-pod
usage has no sanctioned path to it today.

This design adds a second shape to the broker: an HTTP relay for **read-only Google Cloud
REST calls**. The sandbox sends an unauthenticated `GET` for a Google API URL to the broker;
the broker checks the host, method and path against a table of permitted reads, attaches its
own credential, forwards the request, and returns the response body. The model's code never
holds a Google token. What it gains is exactly the reads the table names, on the identity the
broker already has, with the same caller authentication, refusal shape and audit line as
`/v1/exec`.

The first table entries are the Monitoring `timeSeries` and `metricDescriptors` reads and the
Managed Prometheus query endpoints. The collector's one-function change is to obtain its
`requests`-shaped session from the broker client instead of from `google.auth`.

## What was verified on a live install

All of the following were observed read-only on `kage-management` on 2026-09-15, against the
three-pod layout #913 reconciles. They are the facts the design rests on.

| Fact                                                                                                                                                                                                 | Where it matters                                  |
| ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------- |
| The sandbox pod's metadata server answers with the unbound identity `<project>.svc.id.goog`. It issues a token; IAM grants that token nothing.                                                       | Why the collector cannot self-serve               |
| The sandbox has Python 3.14.7 with `requests` and `yaml` importable and no `google` package.                                                                                                         | Client side needs no new packages                 |
| `CREDENTIAL_PROXY_URL` in the sandbox points at the broker's ClusterIP Service on port 8765; the broker answers `/healthz` from there; the caller token is mounted at `CREDENTIAL_PROXY_TOKEN_FILE`. | Transport already exists                          |
| The broker runs with `CREDENTIAL_PROXY_AUTH_MODE=serviceaccount` and allows exactly the gateway's and the shell's ServiceAccounts.                                                                   | Caller authentication is reused                   |
| A `timeSeries` list for `kubernetes.io/container/cpu/core_usage_time` filtered to one cluster, made from the broker pod with its own metadata token, returned 200 with per-container series.         | `roles/monitoring.viewer` suffices; no IAM change |
| The GKE metadata server **ignores a `scopes` parameter**: a token requested with only `monitoring.read` came back carrying `cloud-platform` and reached the Compute API.                             | Scope narrowing is not an available control       |
| No Managed Prometheus frontend service exists in the cluster.                                                                                                                                        | Rules out a PromQL path through proxied `kubectl` |

The last two shape the security section: the policy table is the enforcement, not the token.

## The decision

One new route on the broker, `GET /v1/gcp/<host>/<path>?<query>`, admitted for the shell
caller role, evaluated by a new `api_policy` module in the style of `command_policy`, and
served by the same handler class that serves `/v1/exec`. Envoy needs no change: its single
route forwards every prefix to the Python runtime with no timeout.

Three choices inside that, each with the alternative named:

- **Host in the path, with a hard host allowlist**, rather than one fixed route per service.
  The next entry is Logging, and a per-service route means a handler per service. The host
  allowlist is what stops the relay being a forward proxy: a host not in the table is refused
  before the path is read.
- **A code table, not the JSON policy ConfigMap.** The broker's `policy.json` is a regex
  denylist over argv text; `GCLOUD_READ_COMMANDS` is a code allowlist, reviewed as a diff. The
  relay is an allowlist, so it follows the second. Widening it is a pull request, which is the
  property the `gcloud` table's comment asks for.
- **`GET` only in the first version.** Monitoring's `timeSeries.list` and the Managed
  Prometheus `query` and `query_range` are `GET`. The reads that are `POST` — Logging
  `entries:list`, Monitoring `timeSeries:query` for MQL — carry a body the policy would have to
  inspect, and a body cap and a JSON schema per entry are a second design. Nothing here
  forecloses it.

## The design

### Request contract

The sandbox sends:

```
GET {CREDENTIAL_PROXY_URL}/v1/gcp/monitoring.googleapis.com/v3/projects/{project}/timeSeries?filter=...&interval.startTime=...
Authorization: Bearer <projected caller token>
```

The broker:

1. Authenticates the caller through `_authenticated()`, exactly as every other route does,
   and refuses a caller whose role is not `shell` through `ROUTE_ROLES`, which gains the entry
   `("/v1/gcp/", (CALLER_ROLE_SHELL,))`.
2. Splits the path into `host` (the first segment after `/v1/gcp/`) and `path` (the rest),
   after rejecting any path that is not already in normal form: a `..` segment, an empty
   segment, a percent-encoded slash, or a scheme in the host position is a 400, not something
   to normalise.
3. Evaluates `api_policy.evaluate(method, host, path, query)`. A refusal answers 403 with the
   same body shape `/v1/exec` uses, so a caller that already reads `rule` and `message` reads
   these:

   ```json
   {
     "status": "blocked",
     "code": "SECURITY_POLICY_BLOCKED",
     "rule": "gcp.api.host",
     "message": "logging.googleapis.com is not a host the credential proxy relays. Reads are added to api_policy.API_READ_ROUTES by pull request."
   }
   ```

4. Builds the upstream request `https://{host}/{path}?{query}` with only two headers: the
   broker's `Authorization: Bearer <token>` and an `Accept: application/json`. Every header the
   caller sent is dropped. Three query parameters are removed if present because each is a way
   to substitute a credential or change the response class: `key`, `access_token`,
   `oauth_token`. Everything else in the query is forwarded byte-for-byte; the filter grammar is
   Google's to validate.
5. Forwards with a connect timeout and a total deadline, reads at most the response cap, and
   returns the upstream status code, `Content-Type` and body unchanged. A response over the cap
   is a 502 with `"code": "UPSTREAM_RESPONSE_TOO_LARGE"`; the caller's remedy is a smaller
   `pageSize`, which every listed endpoint supports.
6. Writes one audit line per request, before the decision and after it, on the pattern of the
   exec route:

   ```
   api request_id=%s principal=%s host=%s path=%s
   api blocked request_id=%s rule=%s
   api forwarded request_id=%s host=%s status=%d bytes=%d duration_ms=%d
   ```

   `host` and `path` are caller text and go through `_sanitize_for_logging` as `argv[0]`
   does.

Named constants, declared at the top of the handler module per the engineering rules:

| Name                            | Value      | Why this value                                                                                  |
| ------------------------------- | ---------- | ----------------------------------------------------------------------------------------------- |
| `API_RELAY_PREFIX`              | `/v1/gcp/` | The route                                                                                       |
| `API_RELAY_CONNECT_TIMEOUT_S`   | `10`       | Matches `BROKER_CONNECT_TIMEOUT_SECONDS` on the client                                          |
| `API_RELAY_DEADLINE_S`          | `120`      | Matches the collector's `MONITORING_TIMEOUT_S`; a week of series for a large cluster takes time |
| `API_RELAY_MAX_RESPONSE_BYTES`  | `8 MiB`    | A full `timeSeries` page at the API's maximum `pageSize` is under 4 MiB                         |
| `API_RELAY_STRIPPED_QUERY_KEYS` | see above  | Credential substitution                                                                         |

### The policy module

`agents/platform/scripts/api_policy.py`, stdlib only, importable by the broker and by tests.
It reuses `command_policy.Decision` so the handler's refusal code is shared.

```python
@dataclass(frozen=True)
class ApiRoute:
    host: str            # exact match, lower-cased
    method: str          # "GET" in this version
    path: re.Pattern     # anchored at both ends
    rule_id: str         # what the audit line and the 403 name

# Hosts whose responses are credentials, or that turn a read into a write
# somewhere else. Checked before the table and never overridable by it.
REFUSED_HOSTS = frozenset({
    "iamcredentials.googleapis.com",
    "sts.googleapis.com",
    "oauth2.googleapis.com",
    "accounts.google.com",
    "iam.googleapis.com",
    "secretmanager.googleapis.com",
    "cloudkms.googleapis.com",
    "metadata.google.internal",
})

PROJECT = r"[a-z][a-z0-9-]{4,28}[a-z0-9]"

API_READ_ROUTES: tuple[ApiRoute, ...] = (
    # The cost stream's overrequest check: one call per metric per cluster,
    # paginated. governance/fleet_wide_cost_analysis_sop.md §3.1.
    ApiRoute("monitoring.googleapis.com", "GET",
             re.compile(rf"^v3/projects/{PROJECT}/timeSeries$"),
             "gcp.api.monitoring.timeseries-list"),
    # Discovery read the model needs to name a metric it has not seen before.
    ApiRoute("monitoring.googleapis.com", "GET",
             re.compile(rf"^v3/projects/{PROJECT}/metricDescriptors$"),
             "gcp.api.monitoring.metricdescriptors-list"),
    # Managed Prometheus, for the PromQL the AI-workload skills already print.
    ApiRoute("monitoring.googleapis.com", "GET",
             re.compile(rf"^v1/projects/{PROJECT}/location/global/prometheus/api/v1/(query|query_range|series|labels)$"),
             "gcp.api.monitoring.promql-read"),
)
```

`evaluate` answers in this order, and the order is the security argument:

1. Method not `GET` → `gcp.api.method`.
2. Host in `REFUSED_HOSTS` → `gcp.api.host-refused`. Listed separately from an unknown host so
   the log distinguishes "asked for a token endpoint" from "asked for a service nobody has
   added".
3. Host not in any route → `gcp.api.host`.
4. Host known, path matches no route → `gcp.api.path`, with the message naming the rule ids
   that host does have.
5. Otherwise allowed, carrying the matching `rule_id` for the audit line.

Every regex is anchored and matches the whole path, so `timeSeries` admits nothing under
`timeSeries/` and a project segment cannot carry a slash. The project id is constrained to
Google's grammar so a path cannot smuggle a second segment through it. The table does not
constrain **which** project: the `gcloud` allowlist takes the same position and its comment
says why — deciding scope from caller text puts a parser where the boundary belongs. IAM
bounds the project set; the table bounds the operation.

### Token

The broker already obtains its identity through `google.auth.default()` for the chat relay.
The relay does the same once, holds the credentials object, and lets `google-auth` refresh it;
`AuthorizedSession` is deliberately not used, because the upstream call is built by hand so
that no caller header can leak into it.

The token is the broker's ambient Workload Identity token, with the `cloud-platform` scope.
Narrowing it was the intended second layer and does not work: the GKE metadata server returns
the same token whatever `scopes` it is asked for (verified above). Two consequences are stated
rather than hidden:

- The policy table is the only thing between the model's code and everything the platform
  service account can read. That is the same position `/v1/exec` is in for `gcloud`.
- A dedicated read-only identity is the hardening path, and the mechanism exists:
  `scoped_sa_pool.py` already mints impersonated tokens through the IAM Credentials API, where
  `scopes` is honoured and the target account's own roles are the ceiling. A member holding
  only `roles/monitoring.viewer` would make a table mistake cost a metric read rather than a
  project. It is not in this version because the pool is off by default and its members hold
  no grants; when the pool is turned on, the relay should draw from it and the table should
  name the scope per route.

### The client side

`credential_proxy_client.py` is already in the sandbox image and already knows the broker URL
and the caller token. It gains one class, so no skill or collector reimplements the rewrite:

```python
class ApiSession:
    """A requests-shaped session whose GETs to a Google API go through the broker."""

    def __init__(self, endpoint: str | None = None) -> None:
        self._endpoint = (endpoint or os.environ["CREDENTIAL_PROXY_URL"]).rstrip("/")
        self._http = requests.Session()

    def get(self, url: str, *, params=None, timeout=None):
        parsed = urllib.parse.urlsplit(url)
        relayed = f"{self._endpoint}{API_RELAY_PREFIX}{parsed.netloc}{parsed.path}"
        return self._http.get(relayed, params=params,
                              headers=authorization_headers(), timeout=timeout)
```

Two properties of that shape are the point. The collector's URL literal,
`https://monitoring.googleapis.com/v3/projects/{project}/timeSeries`, stays as written, so the
manifest's evidence label still names the real endpoint. And the object satisfies the
collector's `SessionFn` contract — "anything with a `requests`-shaped `.get(url, params=,
timeout=)`" — so `read_usage` and its tests do not change.

`fleet_waste.default_monitoring_session` becomes:

```python
def default_monitoring_session() -> SessionFn:
    if os.environ.get("CREDENTIAL_PROXY_URL"):
        from credential_proxy_client import ApiSession
        return ApiSession()
    import google.auth                       # a developer laptop, outside any pod
    from google.auth.transport.requests import AuthorizedSession
    credentials, _ = google.auth.default(scopes=[MONITORING_SCOPE])
    return AuthorizedSession(credentials)
```

The `google.auth` branch is kept for a laptop with Application Default Credentials and is not
reachable in either pod. The startup failure text changes from "ADC credentials were
unavailable" to "credential broker unavailable", which is what a `limitations` note will now
mean.

With that, the interpreter references go: `governance/fleet_wide_cost_analysis_sop.md` §3 and
the `fleet-wide-cost-analysis` prompt in `agents/platform/cron/jobs.json` say `python3`. The
sandbox image's shebang guard already refuses `/opt/hermes/.venv/bin/python3` in a staged
script; these two prose references were outside its reach.

A relay refusal reaches the model as the collector's `limitations` string for that cluster,
naming the rule. The SOP's existing metrics-degradation paragraph already tells the model what
to do with it: keep the cluster, skip check 3.1, do not read an empty answer as zero usage.

### What does not change

- **The operator.** The route lives in the broker image; the sandbox already carries the URL
  and the token. No CRD field, no new env, no new NetworkPolicy rule: the broker's egress to
  `googleapis.com` on 443 is what `gcloud` already uses.
- **IAM.** `roles/monitoring.viewer` is already granted and was shown sufficient.
- **The sandbox image.** `requests` is already installed and `credential_proxy_client.py` is
  already on the scripts allowlist, so `test_sandbox_delivery.py` needs no new entry.
- **Envoy.** One route, prefix `/`, timeout `0s`.

## Security review

**What the model's code gains.** `GET` on three path shapes on one host, executed with the
platform service account, returning metric data. A prompt-injected turn can read any project's
metrics that the account can read. It could already read the same clusters' objects through
proxied `kubectl` and the same projects' resources through proxied `gcloud`; this adds
Monitoring to that set.

**What it cannot do through the relay, and which line stops it.**

| Attempt                                                    | Stopped by                                                               |
| ---------------------------------------------------------- | ------------------------------------------------------------------------ |
| Mint a token (`iamcredentials`, `sts`, `oauth2`)           | `REFUSED_HOSTS`, before the table                                        |
| Reach an arbitrary host (exfiltration, SSRF into the VPC)  | Host allowlist; exact match on the first path segment                    |
| Write (`POST`/`PATCH`/`DELETE` on any host)                | Method check, first in `evaluate`                                        |
| Reach a sibling resource (`timeSeries/…`, `alertPolicies`) | Anchored path regex                                                      |
| Smuggle a second segment through the project id            | `PROJECT` grammar                                                        |
| Supply its own credential or an API key                    | Caller headers dropped; `key`/`access_token`/`oauth_token` stripped      |
| Forge an audit line                                        | `_sanitize_for_logging` on every caller-supplied field, as on `/v1/exec` |
| Call it from the gateway role                              | `ROUTE_ROLES` admits `shell` only                                        |
| Exhaust the broker with a huge response                    | `API_RELAY_MAX_RESPONSE_BYTES`, read with a cap rather than `.read()`    |
| Redirect the broker somewhere else                         | Redirects are not followed; a 3xx is returned to the caller as a 502     |

**Residual, stated.** The broker forwards with a `cloud-platform` token because narrowing is
unavailable; a table entry added carelessly is therefore a project-wide read of whatever it
names. The review bar for a new `ApiRoute` is the bar for a new `GCLOUD_READ_COMMANDS` tuple:
one line, one reason in a comment, tests that hold the door one word away. The impersonated
read-only identity above is what lowers that stake and should be scheduled with the pool.

**What it does not touch.** The gateway pod's own ambient identity, which the `gke` remote MCP
server uses today, is a separate open item and not made better or worse by this route.

## Tests

`agents/platform/scripts/test_api_policy.py`:

- Each listed route is allowed with its `rule_id`; the same path with `POST` is refused
  with `gcp.api.method`.
- Every `REFUSED_HOSTS` entry is refused with `gcp.api.host-refused` for any path.
- `logging.googleapis.com` is refused with `gcp.api.host` until an entry exists.
- On `monitoring.googleapis.com`: `v3/projects/p/timeSeries/x`, `v3/projects/p/alertPolicies`,
  `v3/projects/P-UPPER/timeSeries`, `v3/projects/a/b/timeSeries`, and a `metricDescriptors/`
  child path are each refused with `gcp.api.path`.
- The table validator, run at import like `_validate_route_roles`: a host with a scheme, a
  regex not anchored at both ends, and a duplicate `rule_id` each raise.

`test_credential_proxy.py`, in the existing `ThreadingHTTPServer` pattern with a fake upstream
standing in for `monitoring.googleapis.com`:

- A permitted `GET` reaches the fake upstream with exactly two headers, the caller's
  `Authorization` replaced by the broker's, and the stripped query keys absent.
- The upstream's status, `Content-Type` and body come back unchanged, including a 403 and a
  429 from upstream.
- A caller with the `chat` role gets `CALLER_ROLE_FORBIDDEN`; an unauthenticated caller gets 401.
- A `..` segment, a percent-encoded slash and an `https://` host are each a 400 that names the
  reason.
- An upstream body one byte over the cap is a 502 with `UPSTREAM_RESPONSE_TOO_LARGE`.
- An upstream 302 is not followed.
- Each of those writes the audit line the section above specifies, checked with
  `assertLogs`.

`test_credential_proxy_client.py`:

- `ApiSession.get` rewrites the URL, keeps `params` and `timeout`, and attaches
  `authorization_headers()`.

`fleet_waste.py`'s existing tests already inject a session stub and stay as they are.

## Live validation

On `kage-management`, under the live-test lease, with the broker image rebuilt from the
branch:

1. From the sandbox pod, a `curl` to the relay for one `timeSeries` page returns 200 with
   `k8s_container` series for the management cluster, and the broker log shows the three `api`
   lines with `rule=gcp.api.monitoring.timeseries-list`.
2. The same `curl` for `v3/projects/<p>/alertPolicies` returns 403 with `rule=gcp.api.path`;
   for `iamcredentials.googleapis.com/v1/...:generateAccessToken` returns 403 with
   `rule=gcp.api.host-refused`; with `-X POST` returns 403 with `rule=gcp.api.method`.
3. From the gateway pod, the same `curl` with the gateway's token returns 403
   `CALLER_ROLE_FORBIDDEN`.
4. `hermes cron run fleet-wide-cost-analysis` produces a manifest in which at least one
   `collected` cluster carries `overrequest` in `commands` and no `limitations` string, which is
   the state the SOP describes as a complete 3.1.

Each step names what to observe rather than what to run, per `.agents/rules/pre_pr_review.md`.

## Rollout

Two pull requests, in order:

1. **The broker relay**, against `main`: `api_policy.py`, the handler route, `ApiSession`,
   the tests above, this document moved into its final form, and a line in the site's
   security-and-IAM reference describing the route in the same paragraph that describes
   `/v1/exec`. Independently useful and reviewable without the audit rewrite.
2. **The `fleet-audit-collectors` branch**, consuming it: `default_monitoring_session`,
   the two interpreter references, and the failure text. Its Context section cites the first.

## Rejected alternatives

- **Sample with proxied `kubectl top`.** Needs nothing new and is already allowed. The SOP
  argues in §3.1 that a week of history is what justifies proposing a request value; three
  samples are a different, weaker check. Kept as the fallback if the relay cannot land first.
- **Ship `google-auth` in the sandbox.** The import would succeed and the call would fail with
  the unbound identity. The sandbox Dockerfile refuses the package for that reason.
- **Run the Monitoring read on the gateway as a `no_agent` job.** Works only because the
  gateway still carries an identity the sandboxing design is removing, splits the collector
  across two pods since the gateway has no `kubectl`, and writes the manifest to the volume the
  model cannot read. The design already refused this shape for the GitHub token refresh job.
- **The Monitoring remote MCP server.** Real, and it has `list_timeseries` and `query_range`,
  but a tool a model calls is not available to a script, and wiring it the way the `gke` server
  is wired adds a second dependency on the gateway's ambient identity.
- **A general authenticating forward proxy.** Signing anything the sandbox sends is the
  credential handed over in every respect except exportability.
- **A transparent proxy** (universe-domain and metadata-host variables on the sandbox, an
  internal CA, DNS to Envoy, external authorization calling the broker). The strategic form of
  this design: unchanged client code, gRPC included. It reuses this table and is a larger
  change; this route is the piece of it that is needed now.

## Open questions

- Whether `roles/mcp.toolUser`-style per-tool IAM deny policies, which Google applies to its
  managed MCP servers, have a REST-API equivalent that would let the project constrain the
  relay's reads independently of this table. Not found at the time of writing.
- When the scoped pool turns on, whether the relay should refuse rather than fall back to the
  ambient identity when no read-only member exists. The pool's own rule is refuse; the relay
  should follow it.
