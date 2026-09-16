# Kube-Agents IAM & Workload Identity Module

Reusable Terraform module for provisioning the Platform Agent's Google Service Account (GSA), its Workload Identity binding, and its project-level IAM roles.

## Relationship to the install

This is the module the full-install composition (and therefore `install.sh`) uses for the
agent's identity. The canonical identifiers also live with the installer, and the
module's defaults mirror them: the GSA `kubeagents-platform-gsa` and the namespace
`kubeagents-system` as defaults in `install.defaults.env` (an install overrides them
through `install.env`), the KSA `kubeagents-platform-agent` as a constant in
`scripts/installer/common.sh` for the dev tooling.

By default the module grants the read-only role set (the composition's
`permission_set = "read-only"`, also the installer's default). Pass `project_roles = []` to grant
nothing and manage roles yourself — but note the agent fails every GCP call until an
equivalent role set exists.

Whenever `project_roles` is non-empty the module also defines a project-level custom role,
`kubeagentsSubnetUtilizationReader`, and binds it to the same GSA. Two things follow that the
role list alone does not tell you. Its `compute.subnetworks.use` is a consumption permission
rather than a read, so the grant is not read-only in substance — see
[Security & IAM](../../../docs/site/src/content/docs/reference/security-and-iam.md) for why the
narrower custom role is still the better of the two options. And `role_id` is a constant, not
derived from `service_account_id`, so two instantiations of this module in one project collide on
it. That is a harder constraint than the one the GSA name imposes, not the same one:
`service_account_id` is a variable, so a second instantiation can be given a different name, and
the custom role has no such escape.

`terraform destroy` — which `uninstall.sh` reaches — soft-deletes that role, and GCP then holds
the name for between 7 and 37 days. Reinstalling inside the first 7 is fine, because the provider
finds the soft-deleted role and undeletes it. Past that the role can be neither created nor
changed until the window closes, and `terraform apply` fails. Wait it out, or run
`gcloud iam roles undelete kubeagentsSubnetUtilizationReader --project <project>`. `main.tf`
carries the rest, including why turning the id into a variable is not the fix it looks like.

There is no admin preset to mirror: the `gke-admin` bundle was removed (see
[Security & IAM](../../../docs/site/src/content/docs/reference/security-and-iam.md)),
and this module has never had one. Passing admin roles through `project_roles` is
possible and is the module's equivalent of `permission_set = "custom"` — it puts
the grant in your Terraform, where it is reviewed.

## The scoped service account pool

`scoped_clusters` provisions one service account per named GKE cluster, plus
`roles/iam.serviceAccountTokenCreator` for the agent bound on each member as a
resource (never at project level). The members hold no IAM grant of their own
as of 2026-08-12 — the IAM-Condition scoping they were designed around grants
nothing for Kubernetes object operations — so the default is `[]` and should
stay there until per-cluster RBAC lands. The site's
[security-and-iam reference](../../../docs/site/src/content/docs/reference/security-and-iam.md)
owns the topic, including how the mapping reaches the credential broker and
what the pool does and does not bound.

## Usage

```hcl
module "kube_agents_iam" {
  source             = "git::https://github.com/gke-labs/kube-agents.git//terraform/modules/kube-agents-iam?ref=1.2.0"
  project_id         = "my-gcp-project"
  service_account_id = "kubeagents-platform-gsa"
  namespace          = "kubeagents-system"
  ksa_name           = "kubeagents-platform-agent"
}
```

See the [Release versioning & promotion guide](../../../docs/site/src/content/docs/deploy/release-versioning.md) for SemVer pinning instructions.
