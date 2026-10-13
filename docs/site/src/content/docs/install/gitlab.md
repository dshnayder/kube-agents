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
  `--gitops-forge` flag. To check a release, run `./install.sh --help | grep -- --gitops-forge`
  in it.
- **The project.** Create the GitOps project first; the installer does not create it. Note
  its full path, nested groups included: `platform/infra/gitops`.
- **The token.** A group or project access token with the `api` and `write_repository`
  scopes and the Developer role. On a GitLab tier without group or project access tokens,
  use a personal access token of a dedicated account that is a Developer member of the
  project, never a person's own. Two limits apply:
  - The credential broker acts only on the groups of the repositories that the install
    declares. For `platform/infra/gitops`, that is the group `platform/infra`.
  - The token limits what GitLab lets the broker do. Give it the smallest reach: a project
    access token on the GitOps project, or a group access token on the project's own group
    (`platform/infra`, not `platform`).
- **Token expiry.** GitLab access tokens expire, by default after at most one year. An
  administrator of a self-managed instance can change that limit. Record the expiry date, and replace the token
  before it expires (see [Rotating the token](#rotating-the-token)).
- **Network.** Only the credential broker pod (`<agent>-credential-proxy`) connects to
  GitLab, over HTTPS on port 443. The NetworkPolicy that kube-agents ships for that pod limits
  ingress only, so its egress is open, to private ranges too. The agent pod's egress policy
  excludes private ranges, but the agent never connects to GitLab. If you add a policy that
  limits egress in the agent's namespace, let the broker pod reach the GitLab host on 443.
  The broker resolves the hostname through the cluster DNS. For a private hostname on GKE, a
  Cloud DNS private zone attached to the cluster's VPC works.
- **The installer host.** `install.sh` never connects to GitLab. The machine that runs it
  needs no route to the GitLab host and no trust for its certificate.
- **The certificate.** On a self-managed instance, the certificate must chain to a CA that
  the broker image trusts: a public CA, or a proxy that terminates TLS with such a
  certificate. 0.9.0 has no setting for a private CA.
- **`gh`.** The installer checks for the GitHub CLI on every install, and installs it when it
  is missing (a dry run stops instead). A GitLab install does not use it to reach GitLab.

## Run the installer

Interactively, choose **GitLab** when the installer asks where the GitOps repository lives,
then give the host (enter for gitlab.com), the project path, and the name of the Secret to
create. The installer asks for the token itself only after the cluster is up, at the health
check step. The prompt does not echo.

Non-interactively, the token comes from a file you name, or anything readable that a
shell can name, such as a password manager through process substitution:
`--gitlab-token-file=<(pass show gitlab/agent-token)`. There is no flag or variable that
takes the token's value:

```bash
./install.sh -y \
  --gitops-forge=gitlab \
  --gitops-host=gitlab.example.com \
  --gitops-repo=platform/infra/gitops \
  --gitlab-token-file="$HOME/secure/gitlab-token"
```

| Flag                    | `install.env` key     | Meaning                                                                                                                                                        |
| ----------------------- | --------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--gitops-forge`        | `GITOPS_FORGE`        | `github` (default) or `gitlab`.                                                                                                                                |
| `--gitops-host`         | `GITOPS_HOST`         | The GitLab hostname, with no scheme, port or path. Omit for gitlab.com. A GitHub host is refused.                                                              |
| `--gitops-repo`         | `GITOPS_REPO`         | The project's full path, `group/project` or `group/subgroup/project`, or its `https://` URL on that host. `--gitops-org` is not used.                          |
| `--gitlab-token-file`   | _never recorded_      | A file (or `/dev/stdin`, or `<(command)`) holding the token. Read once, piped into the Secret, and neither its path nor its contents is written anywhere else. |
| `--gitlab-token-secret` | `GITLAB_TOKEN_SECRET` | The Secret's name, default `gitlab-forge-token`.                                                                                                               |

`--github-app-id` and `--github-pem-path` are refused with `--gitops-forge=gitlab`.

**The token source.** The installer reads the whole source and removes all whitespace from it,
newlines inside it too. So the source must hold only the token. A `pass` entry with more lines
(a user name, a URL) needs its first line only: `--gitlab-token-file=<(pass show gitlab/agent-token | head -n1)`.

**Dry run and generate-only.** `--dry-run` and `--generate-only` check only that the token
source is readable. They do not read the token. A real run reads it once, after the apply. With
`--generate-only`, the installer never creates the Secret: create it after `lifecycle.sh apply`,
as [Rotating the token](#rotating-the-token) shows.

Without a token file in a non-interactive run, the install completes and prints the
command that creates the Secret. Until it exists the agent answers every GitLab call with
`FORGE_CREDENTIAL_UNAVAILABLE`; it reads the Secret on each call, so creating it later needs
no restart.

## Check the connection

After the install, ask the broker who the token is, from the shell sandbox:

```bash
kubectl exec -n kubeagents-system -c shell platform-agent-shell-0 -- \
  python3 /opt/data/skills/version-control/scripts/vcs.py identity \
  --repo https://gitlab.example.com/platform/infra/gitops
```

It prints one JSON object with the token's user name. Other answers:

- `FORGE_CREDENTIAL_UNAVAILABLE`: the Secret does not exist, or has no `token` key.
- `FORGE_UNAUTHENTICATED`: GitLab refused the token. It expired, or someone revoked it.
- A message that the forge's TLS certificate failed verification: the broker does not trust
  the instance's certificate (see [Before you start](#before-you-start)).

The PlatformAgent status does not report these, so this check is the way to see them. The
broker also logs `version control enabled … forges=github,gitlab` when it starts.

## Where the token goes

The installer pipes the token straight into `kubectl create secret … --from-file=token=…`
and a server-side `kubectl apply`, so it never appears in a process's arguments, an exported environment variable, the
installer's output, `install.env`, `terraform.tfvars` or the Terraform state. Terraform and
the Helm release name the Secret and nothing more. The operator mounts the Secret's `token`
key into the credential broker's pod only; the agent and its shell sandbox never see it.

## Rotating the token

Replace the token before it expires: an expired token gives `FORGE_UNAUTHENTICATED` on every
GitLab call, and nothing else reports it. Create the new token in GitLab, put it in the
Secret, then revoke the old token. Replace the Secret's value; the broker picks the new token
up on its next call:

```bash
kubectl create secret generic gitlab-forge-token -n <namespace> \
  --from-file=token="$HOME/secure/gitlab-token" --dry-run=client -o yaml \
  | kubectl apply --server-side --force-conflicts -f -
```

Use `--server-side`. A plain `kubectl apply` stores the whole object, token
included, in the Secret's `kubectl.kubernetes.io/last-applied-configuration`
annotation.

`install.sh` records the forge, host, project and Secret name in `install.env`: on a
first install, and on any later run that goes on to apply with a forge, host, project or
Secret name different from the file's. So `upgrade.sh` and the Day-2 menu go on rendering
what was applied, and on a GitLab install a `--gitops-repo`, `--gitops-host` or
`--gitlab-token-secret` is recorded, not a one-run override. A run you decline or end with
`--generate-only` records nothing. A switch to GitLab drops the file's `GITOPS_ORG`,
`GITHUB_APP_ID` and `GITHUB_PEM_PATH`; a switch back to GitHub drops the GitLab keys. Once
`install.env` exists, a `GITOPS_FORGE`, `GITOPS_HOST` or `GITLAB_TOKEN_SECRET` exported in
your shell is ignored; use the flags.

A re-run of `install.sh` keeps an existing Secret unless you give it a new token file, or
choose to replace it at the prompt. An empty token (whitespace only) is refused, and
nothing is stored.

## What the install declares

The installer writes the forge into the `PlatformAgent` as `spec.integration.forges` and
`spec.integration.repositories`, not the `github` alias: one forge with `provider: gitlab`,
your host and the Secret as its `credentialsRef`, and the project as the `gitops`
repository. The [PlatformAgent CRD reference](/kube-agents/operator/platformagent-crd/)
describes those fields, and a GitLab forge's limits, in full.
