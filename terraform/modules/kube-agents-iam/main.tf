resource "google_service_account" "agent" {
  project      = var.project_id
  account_id   = var.service_account_id
  display_name = var.display_name
}

resource "google_service_account_iam_member" "workload_identity" {
  service_account_id = google_service_account.agent.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[${var.namespace}/${var.ksa_name}]"
}

# `gcloud compute networks subnets list-usable` returns only the subnets the
# caller holds compute.subnetworks.use on, and that enumeration is what scopes
# the gcp-networking-fabric-audit `subnet-ip-exhaustion` check. Without the
# permission the command does not fail -- it exits 0 with an empty list, which
# the audit could not tell apart from "this project has no subnets" until the
# collector learned to corroborate against `subnets list`. On the deployed
# install that was 42 subnets reported as zero, every run.
#
# What the permission buys is the subnet list, not the measurement. gcloud's
# UsableSubnetwork carries no ipUtilization field in v1, beta or alpha, so the
# figure every verdict turns on comes from the Network Analyzer insight the two
# recommender permissions below read. Take the three together: the first finds
# the subnets, the other two say how full each one is.
#
# compute.subnetworks.use is a consumption permission, not a read: it is what
# authorizes attaching a NIC, a node pool or a load balancer to a subnet, and
# roles/compute.viewer does not carry it. So this is a deliberate exception to
# the read-only project grant rather than an instance of it.
#
# The predefined home for it is roles/compute.networkUser, which carries some
# two hundred permissions including networksecurity.sacAttachments.create and
# .delete -- writes. A custom role with three permissions is the narrower of
# the two, which is the whole argument for it; it is not a way of keeping the
# grant read-only, and the docs should not say it is.
#
# Gated on project_roles being non-empty so that `project_roles = []` still
# means "grant nothing and manage roles outside the module", as its
# description promises.
#
# The role id is a constant, and that has a sharp edge worth knowing about
# before you reach for it. `uninstall.sh` reaches terraform destroy, which
# soft-deletes this role; GCP then holds the name for between 7 and 37 days.
# Reinstalling inside the first 7 is fine -- the provider finds the soft-deleted
# role and undeletes it -- but past that the role can be neither created nor
# changed until the window closes, and terraform apply fails. Wait it out, or
# `gcloud iam roles undelete` the id in the affected project.
#
# Making the id a variable looks like the fix and is not, which is why it was
# tried and taken back out. Nothing in the installer's state model persists such
# a value: it is absent from vars.sh and from the tfvars emitter in
# installer_common.sh, so a later `./upgrade.sh` -- which applies with
# -auto-approve -- would fall back to the default, and role_id is ForceNew. That
# plans a destroy-and-recreate that fails against the still-held original name,
# turning a wedge on one id into a wedge on two, unprompted. Persisting it means
# teaching vars.sh, the tfvars emitter and verify_ci_pool_project.py, which
# pins this literal; do that first if the variable is wanted.
resource "google_project_iam_custom_role" "subnet_utilization_reader" {
  count = length(var.project_roles) > 0 ? 1 : 0

  project = var.project_id
  role_id = "kubeagentsSubnetUtilizationReader"
  title   = "Kube-Agents Subnet Utilization Reader"
  # This string is what the GCP console shows an operator reviewing the role, so it
  # carries the same correction as the comment above rather than the read-framing the
  # comment rejects. Changing it is an in-place update of the role on the next apply;
  # it touches no permission and no binding.
  description = "Grants only the three permissions the fleet audit's subnet-ip-exhaustion check needs: compute.subnetworks.use, which is a consumption permission and not a read, plus the list and get on the Network Analyzer insight that carries subnet utilization."
  permissions = [
    "compute.subnetworks.use",
    # `list-usable` turned out to answer only half the question: it reports
    # which subnets exist but carries no ipUtilization field on any API
    # version, so the check still had nothing to measure. Network Analyzer
    # publishes that measurement as google.networkanalyzer.vpcnetwork.
    # ipAddressInsight, and the two permissions below are what read it. This
    # role grants them directly rather than binding a predefined role,
    # because all thirteen predefined roles that carry the pair carry more:
    # the narrowest, roles/recommender.networkAnalyzerIpAddressViewer, adds
    # recommender.locations.get/.list and resourcemanager.projects.get/.list;
    # roles/recommender.viewer runs to some three hundred permissions --
    # viewer on every recommender in the project -- and the list ends at
    # roles/editor and roles/owner. Only those last two also carry
    # compute.subnetworks.use, so any predefined role narrow enough to be
    # worth binding would leave a second grant to make anyway.
    "recommender.networkAnalyzerIpAddressInsights.list",
    "recommender.networkAnalyzerIpAddressInsights.get",
  ]
  stage = "GA"
}

resource "google_project_iam_member" "subnet_utilization_reader" {
  count = length(var.project_roles) > 0 ? 1 : 0

  project = var.project_id
  role    = google_project_iam_custom_role.subnet_utilization_reader[0].id
  member  = "serviceAccount:${google_service_account.agent.email}"
}

locals {
  # The list that actually reaches google_project_iam_member.agent_roles.
  #
  # There is deliberately only one, and it is `var.project_roles`. An earlier
  # draft kept a second copy here as the module's "real" default; because the
  # variable is nullable = false with a default of its own, `var.project_roles`
  # is never null and that copy was unreachable -- a role list a reader would
  # take for the granted set while nothing bound it.
  #
  # This local exists as the seam the scoped_clusters coupling goes back into.
  # It was going to read:
  #
  #   length(var.scoped_clusters) > 0
  #   ? [for role in var.project_roles : role if role != "roles/container.viewer"]
  #   : var.project_roles
  #
  # so populating scoped_clusters stripped container.viewer from the agent and
  # relied on the pool to carry it per cluster. roles/container.viewer is what
  # lets an identity read Kubernetes objects in every cluster in the project;
  # without it the agent keeps roles/container.clusterViewer, which reaches the
  # Container API control plane -- listing clusters, `get-credentials` -- and
  # nothing inside a cluster.
  #
  # That residual matters more than it looks. The metadata server is reachable
  # from the agent container in a default install, so the agent can mint a token
  # for this identity whenever it likes, entirely outside the broker. Shrinking
  # what that token is worth is the only control that survives the bypass.
  #
  # SUSPENDED 2026-08-12. The pool carries nothing now: the IAM Condition
  # scoping its members grants nothing for Kubernetes object operations, so the
  # grant was removed outright. See scoped_pool.tf.
  #
  # Left as it was, this is a total outage rather than a narrowing -- the agent
  # cannot read objects and no pool member can either. The runtime flag does not
  # rescue it. CREDENTIAL_PROXY_SCOPED_SA_POOL=0 falls back to the ambient
  # credential, and the ambient credential is precisely the one this stripped.
  #
  # The reasoning above is still correct and the metadata-server argument is the
  # strongest reason to want it back. Restore it in the same change that lands
  # per-cluster RBAC, gated on the pool granting something, with a test that a
  # read still succeeds afterwards.
  agent_project_roles = var.project_roles
}

resource "google_project_iam_member" "agent_roles" {
  #checkov:skip=CKV_GCP_41:Platform agent requires serviceAccountUser role to manage agent workload identities
  #checkov:skip=CKV_GCP_42:Service account is granted non-admin project roles
  #checkov:skip=CKV_GCP_46:Dedicated custom service account used for agent workload identity
  #checkov:skip=CKV_GCP_49:Platform agent requires serviceAccountUser role to manage agent workload identities
  #checkov:skip=CKV_GCP_117:Standard GCP viewer roles granted for read-only telemetry and cluster observability
  for_each = toset(local.agent_project_roles)

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.agent.email}"
}
