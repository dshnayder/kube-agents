---
title: GitLab as the GitOps forge
description: Installing with the GitOps repository on gitlab.com or a self-managed GitLab — the token, the flags, and where the token goes.
---

`install.sh` connects the agent's GitOps repository on GitHub by default. With
`--gitops-forge=gitlab` the repository is a GitLab project instead, on gitlab.com or on a
self-managed instance. A GitLab install has no GitHub App and no token minter: the agent's
credential is a GitLab access token, which you hand to the installer and which it stores in
a Kubernetes Secret in the agent's namespace. Nothing else ever holds it.

## Before you start

- **The release.** GitLab support starts in release 0.9.0. An earlier release has no
  `--gitops-forge` flag. The latest release is on
  [GitHub Releases](https://github.com/gke-labs/kube-agents/releases/latest). To check a
  release, run
  `curl -fsSL https://raw.githubusercontent.com/gke-labs/kube-agents/<RELEASE_VERSION>/install.sh | bash -s -- --help | grep -- --gitops-forge`,
  or `./install.sh --help | grep -- --gitops-forge` in an unpacked release.
- **The GitLab version.** Tested with GitLab 19.4, and with GitLab 16.11 except for the diff
  of a merge request.
- **The project.** Create the GitOps project first. The installer does not create it. Note its
  full path with its nested groups, for example `platform/infra/gitops`. The project needs a
  default branch with at least one commit: the agent opens its merge requests against it.
- **The host.** gitlab.com needs no host. For a self-managed instance, the host is a lowercase
  DNS name with at least two labels, such as `gitlab.example.com` or `gitlab.corp.internal`.
  It has no scheme, port or path. The broker connects to it with HTTPS on port 443. The
  installer refuses a GitHub hostname.
- **The token.** Use a group or project access token with the `api` and `write_repository`
  scopes and the Developer role. On a GitLab tier without group or project access tokens, use
  a personal access token of a dedicated account that is a Developer member of the project.
  Do not use a person's own token. The Developer role is enough: the agent pushes its own
  branches and opens merge requests, and it never merges or pushes to a protected branch. Two
  limits apply:
  - The broker uses the token only for the declared repositories. Another project in the
    same group gets no token; `inspect-repository` reads it without one, so only a public
    project is readable. A project outside those groups is refused. For
    `platform/infra/gitops`, the group is `platform/infra`.
  - The token limits what GitLab lets the broker do. Give it the smallest reach: a project
    access token on the GitOps project, or a group access token on the project's own group
    (`platform/infra`, not `platform`).
- **Token expiry.** GitLab access tokens expire, by default after at most one year. An
  administrator of a self-managed instance can change that limit. Record the expiry date.
  Replace the token before it expires, as [Rotating the token](#rotating-the-token) shows.
- **Network.** Only the credential broker pod (`<agent>-credential-proxy`) connects to GitLab,
  with HTTPS on port 443. Its shipped NetworkPolicy limits ingress only, so its egress is open,
  to private ranges too. The agent pod's egress policy excludes private ranges, but the agent
  never connects to GitLab. If you add a policy that limits egress in the agent's namespace,
  let the broker pod reach the GitLab host on 443. The broker resolves the hostname through the
  cluster DNS. For a private hostname on GKE, a Cloud DNS private zone that is attached to the
  cluster's VPC works. A name in an on-premises DNS needs a Cloud DNS
  [forwarding zone or peering](https://cloud.google.com/dns/docs/zones/zones-overview) to that
  DNS. The VPC firewall must let the cluster's node and Pod ranges reach the
  host on 443. For a public instance, the cluster also needs Cloud NAT or public IP addresses.
  A route to a private network outside the VPC (Cloud VPN or Cloud Interconnect) must carry the
  Pod range: advertise that range with
  [Cloud Router custom advertisements](https://cloud.google.com/network-connectivity/docs/router/how-to/advertising-overview),
  or masquerade it with
  [the IP masquerade agent](https://cloud.google.com/kubernetes-engine/docs/how-to/ip-masquerade-agent).
- **The installer host.** `install.sh` never connects to GitLab. The machine that runs it needs
  no route to the GitLab host and no trust for its certificate.
- **The certificate.** On a self-managed instance, the broker must trust the certificate. A
  certificate from a public CA, or from a proxy that terminates TLS with such a certificate,
  needs nothing. For a private CA, use a release whose `install.sh --help` lists
  `--gitops-ca-file`.
- **`gh`.** The installer checks for the GitHub CLI on every install, and installs it when it
  is missing (a dry run stops instead). A GitLab install does not use it to connect to GitLab.

### Check the token

Run these checks on a machine that has a route to the GitLab host. It does not have to be the
installer host. Each command sends the token in a header from a pipe, so the token is not in a
process's arguments.

The token's user, scopes and expiry date:

```bash
pass show gitlab/agent-token | head -n1 | sed 's/^/PRIVATE-TOKEN: /' |
  curl -sS -H @- https://gitlab.example.com/api/v4/personal_access_tokens/self
```

The answer has `scopes`, `user_id`, `active` and `expires_at`. This works for personal, project
and group access tokens (tested on GitLab 19.4). For the user name, use
`https://gitlab.example.com/api/v4/user` with the same pipe.

The token's access to the project. The path is URL-encoded:

```bash
pass show gitlab/agent-token | head -n1 | sed 's/^/PRIVATE-TOKEN: /' |
  curl -sS -H @- https://gitlab.example.com/api/v4/projects/platform%2Finfra%2Fgitops |
  jq 'if has("permissions") then ([.permissions.project_access.access_level, .permissions.group_access.access_level] | map(. // 0) | max) else . end'
```

- `30` or more: the token can write (Developer or higher).
- A lower number, or `0`: the token can only read, or has no membership. On a public project,
  GitLab answers the read with no membership, so a successful read alone proves nothing.
- `{"message":"404 Project Not Found"}`: the token cannot see the project, or the path is
  wrong.
- `{"message":"401 Unauthorized"}`: the token is not valid, has expired, or is revoked.

### Check the network path

The test runs a short-lived Pod with `curl` in the cluster. `<install-context>` is the kubectl
context that `gcloud container clusters get-credentials` writes for the cluster:
`gke_<project>_<location>_<cluster>`. `<namespace>` is one of these:

- `default`: on a cluster that already exists, before the install. `kubeagents-system` does
  not exist yet, and the policies of `default` can differ from those of `kubeagents-system`.
- `kubeagents-system`: on a new cluster, after the install, together with
  [Check the connection](#check-the-connection).

```bash
kubectl --context <install-context> -n <namespace> run gitlab-reach --rm -i --restart=Never \
  --image=curlimages/curl -- curl -sS https://gitlab.example.com/api/v4/version
```

An answer of `401 Unauthorized` or a version object shows that DNS, the route and TLS work. A
DNS, timeout or certificate error shows which part does not.

A cluster that pulls only from an approved registry, or a private cluster without Cloud NAT,
cannot pull `curlimages/curl`. Use any image with `curl` from a registry that the cluster can
pull from. The install's own shell sandbox image has `curl`:
`<registry-prefix>/agent-sandbox:<RELEASE_VERSION>` (the default prefix is
`ghcr.io/gke-labs/kube-agents`). Give `--command` before `curl`, so that the image's entrypoint
does not run.

## Run the installer

Interactively, choose **GitLab** when the installer asks where the GitOps repository is. Then
give the host (press Enter for gitlab.com), the project path, and the name of the Secret to
create. The installer asks for the token only after the cluster is up, at the health check
step. The prompt does not echo.

Non-interactively, the token comes from a file, or from a process substitution such as a
password manager. No flag or variable takes the token itself:

```bash
./install.sh -y \
  --gcp-project-id="YOUR_GCP_PROJECT_ID" \
  --gke-cluster-name="platform-agent-host" \
  --gcp-region="us-central1" \
  --model-provider="gemini" \
  --permission-set="read-only" \
  --gitops-forge=gitlab \
  --gitops-host=gitlab.example.com \
  --gitops-repo=platform/infra/gitops \
  --gitlab-token-file=<(pass show gitlab/agent-token | head -n1)
```

| Flag                    | `install.env` key     | Meaning                                                                                                                                                                   |
| ----------------------- | --------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--gitops-forge`        | `GITOPS_FORGE`        | `github` (default) or `gitlab`.                                                                                                                                           |
| `--gitops-host`         | `GITOPS_HOST`         | The GitLab hostname, with no scheme, port or path. Omit it for gitlab.com. The installer refuses a GitHub host.                                                           |
| `--gitops-repo`         | `GITOPS_REPO`         | The project's full path, `group/project` or `group/subgroup/project`, or its `https://` URL on that host. `--gitops-org` is not used.                                     |
| `--gitlab-token-file`   | _never recorded_      | A file, `/dev/stdin` (not under `curl … \| bash`) or `<(command)` that holds the token. The installer reads it once, after the apply, and writes it only into the Secret. |
| `--gitlab-token-secret` | `GITLAB_TOKEN_SECRET` | The Secret's name, default `gitlab-forge-token`.                                                                                                                          |

The installer refuses `--github-app-id` and `--github-pem-path` with `--gitops-forge=gitlab`.

**The token source.** The installer reads the whole source and removes all whitespace from it,
newlines inside it too. The source must hold only the token. A `pass` entry with more lines (a
user name, a URL) needs its first line only, as in the example above.

**Process substitution and `pass`.** The file descriptor of a process substitution stays open
for the whole run, and the installer reads it after the apply. `pass` runs when the run starts.
Unlock `gpg-agent` first, for example with `pass show gitlab/agent-token >/dev/null`. If `pass`
cannot decrypt the entry, the source is empty. The installer then stops after the apply with
"The GitLab token source … is empty; nothing was stored." and exit code 1, and creates no
Secret. Create the Secret yourself, as [Rotating the token](#rotating-the-token) shows.
`/dev/stdin` works only with `./install.sh` from an unpacked release: under `curl … | bash`,
standard input is the script itself.

**No token file.** In a non-interactive run without `--gitlab-token-file`, the install
completes and prints the command that creates the Secret. Until the Secret exists, every GitLab
call answers `FORGE_CREDENTIAL_UNAVAILABLE`. The broker reads the Secret on each call, so you
do not have to restart anything after you create it.

### Dry run and generate-only

`--dry-run` and `--generate-only` check only that the token source is readable. They do not
read the token. A real run reads it once, after the apply.

`--generate-only` writes `terraform.tfvars`, and it never creates the token Secret:

- On a first install, it writes `install.env` with the GitLab keys (`GITOPS_FORGE`,
  `GITOPS_HOST`, `GITOPS_REPO` and `GITLAB_TOKEN_SECRET`).
- Over an existing `install.env`, it does not change those keys. Before you run
  `lifecycle.sh apply`, set `GITOPS_FORGE`, `GITOPS_HOST`, `GITOPS_REPO` and
  `GITLAB_TOKEN_SECRET` in `install.env` yourself. Otherwise the next `upgrade.sh` renders the
  forge that the file records, and removes the GitLab forge.

After `lifecycle.sh apply`, create the Secret in the agent's namespace, as
[Rotating the token](#rotating-the-token) shows.

## Check the connection

After the install, ask the broker for the token's user, from the shell sandbox:

```bash
kubectl exec -n kubeagents-system -c shell platform-agent-shell-0 -- \
  python3 /opt/data/skills/version-control/scripts/vcs.py identity \
  --repo https://gitlab.example.com/platform/infra/gitops
```

It prints one JSON object with the token's user name. Other answers:

- `FORGE_CREDENTIAL_UNAVAILABLE`: the Secret does not exist or has no `token` key, or the token
  is empty, has more than one line, or has a character that is not printable ASCII.
- `FORGE_UNAUTHENTICATED`: GitLab refused the token. It expired, or someone revoked it.
- A message that the forge's TLS certificate failed verification: the broker does not trust
  the instance's certificate. See [Before you start](#before-you-start).

The PlatformAgent status does not report these, so this check is the way to see them. When the
broker starts, it logs the forges that it serves:

```bash
kubectl logs -n kubeagents-system deploy/platform-agent-credential-proxy \
  -c envoy-credential-proxy | grep "version control enabled"
```

On a GitLab install, the line ends with `forges=github,gitlab`: the broker always serves the
GitHub forge too.

To see the forge that the install declared:

```bash
kubectl get platformagent platform-agent -n kubeagents-system \
  -o jsonpath='{.spec.integration.forges}{"\n"}{.spec.integration.repositories}{"\n"}'
```

## Where the token goes

The installer pipes the token straight into `kubectl create secret … --from-file=token=…`
and a server-side `kubectl apply`, so it never appears in a process's arguments, an exported environment variable, the
installer's output, `install.env`, `terraform.tfvars` or the Terraform state. Terraform and
the Helm release name the Secret and nothing more. The operator mounts the Secret's `token`
key into the credential broker's pod only; the agent and its shell sandbox never see it.

## Rotating the token

Replace the token before it expires. An expired token gives `FORGE_UNAUTHENTICATED` on every
GitLab call, and nothing else reports it. Create the new token in GitLab, put it in the Secret,
and then revoke the old token. The broker reads the new token on its next call:

```bash
kubectl create secret generic gitlab-forge-token -n kubeagents-system \
  --from-file=token=<(pass show gitlab/agent-token | head -n1) --dry-run=client -o yaml |
  kubectl apply --server-side --force-conflicts -f -
```

Use `--server-side`. A plain `kubectl apply` stores the whole object, token included, in the
Secret's `kubectl.kubernetes.io/last-applied-configuration` annotation. The broker removes
whitespace around the token when it reads it, so a trailing newline does no harm.

## The `install.env` keys

The installer reads `install.env` from the first of these places:

1. the path in `KUBE_AGENTS_INSTALL_ENV`;
2. an `install.env` beside `install.sh`;
3. an `install.env` in the current directory;
4. the source directory that the install uses: the unpacked release or checkout that it runs
   from, or `$HOME/kube-agents` under `curl … | bash`.

A first install writes the file in that same place.

`install.sh` records the forge, host, project and Secret name in `install.env`. It does this
on a first install, and on a later run that goes on to the apply with a different forge, host,
project or Secret name. Then `upgrade.sh` and the Day-2 menu render what was applied. A run
that you decline, or that ends with `--generate-only`, does not change these keys in an
existing file. A switch to GitLab drops `GITOPS_ORG`, `GITHUB_APP_ID` and `GITHUB_PEM_PATH`. A
switch back to GitHub drops the GitLab keys. Once `install.env` exists, the installer ignores a
`GITOPS_FORGE`, `GITOPS_HOST` or `GITLAB_TOKEN_SECRET` exported in your shell. Use the flags.

A re-run of `install.sh` keeps an existing Secret, unless you give it a new token file or
choose to replace the Secret at the prompt. The installer refuses an empty token (whitespace
only), and stores nothing.

## Upgrade and uninstall

- `upgrade.sh` reads the forge, host, project and Secret name from `install.env`. It needs no
  token, and it keeps the token Secret.
- The token Secret is not part of the Helm release. Deleting the PlatformAgent or uninstalling
  the release leaves it. A full teardown that deletes the cluster deletes it too. On a cluster
  that the teardown keeps, delete it yourself. Take its name and the namespace from
  `install.env`:

  ```bash
  # The install's install.env. See "The install.env keys" for where it is.
  ENV_FILE="$HOME/kube-agents/install.env"
  (
    unset NAMESPACE GITLAB_TOKEN_SECRET
    eval "$(grep -E '^(NAMESPACE|GITLAB_TOKEN_SECRET)=' "$ENV_FILE")"
    kubectl delete secret "${GITLAB_TOKEN_SECRET:-gitlab-forge-token}" \
      -n "${NAMESPACE:-kubeagents-system}" --ignore-not-found=true
  )
  ```

- After an uninstall, revoke the token in GitLab.

## Limits

- The skills read GitLab entries in `managed_repos`, but only GitHub entries in `context_repos`.
  A GitLab repository that you add as a context repository is skipped with a warning.

## What the install declares

The installer writes the forge into the `PlatformAgent` as `spec.integration.forges` and
`spec.integration.repositories`, not the `github` alias: one forge with `provider: gitlab`,
your host and the Secret as its `credentialsRef`, and the project as the `gitops`
repository. The [PlatformAgent CRD reference](/kube-agents/operator/platformagent-crd/)
describes those fields, and a GitLab forge's limits, in full.
