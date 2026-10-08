# The GitOps forge: a GitHub install renders the deprecated github alias and
# nothing else, a GitLab install renders the forges/repositories lists with a
# credentialsRef and never the alias, and the plan refuses a GitLab install that
# also asks for the GitHub token minter. The providers are mocked and the
# cluster module's outputs fixed, so a plan here creates nothing.

mock_provider "google" {}
mock_provider "google-beta" {}
mock_provider "helm" {}
mock_provider "random" {}
mock_provider "tls" {}

override_module {
  target = module.gke_cluster
  outputs = {
    cluster_endpoint        = "10.0.0.1"
    cluster_endpoint_is_dns = false
    cluster_ca_certificate  = "Y2E="
    cluster_location        = "us-central1"
    cluster_name            = "c"
    network_policy_enforced = true
    workload_identity_pool  = "tftest-project.svc.id.goog"
  }
}

variables {
  project_id     = "tftest-project"
  cluster_name   = "c"
  location       = "us-central1"
  api_server_key = "k"
}

run "github_with_no_repo_renders_no_integration" {
  command = plan
  assert {
    condition     = length(local.platform_agent_integration) == 0
    error_message = "integration: ${jsonencode(local.platform_agent_integration)}"
  }
}

run "github_renders_the_alias" {
  command = plan
  variables {
    github_repo = "acme/infra"
  }
  assert {
    condition     = jsonencode(local.platform_agent_integration) == jsonencode({ github = { gitRepo = "acme/infra", org = "acme" } })
    error_message = "integration: ${jsonencode(local.platform_agent_integration)}"
  }
}

run "gitlab_renders_the_lists" {
  command = plan
  variables {
    gitops_forge              = "gitlab"
    gitops_host               = "gitlab.example.com"
    gitlab_repo               = "platform/infra/gitops"
    gitlab_credentials_secret = "gl-token"
  }
  assert {
    condition = local.gitlab_forges == [{
      name = "gitlab", provider = "gitlab", host = "gitlab.example.com", credentialsRef = { name = "gl-token" }
    }]
    error_message = "gitlab forge: ${jsonencode(local.gitlab_forges)}"
  }
  assert {
    condition = local.gitlab_repositories == [{
      forge = "gitlab", repository = "platform/infra/gitops", role = "gitops"
    }]
    error_message = "gitlab repository: ${jsonencode(local.gitlab_repositories)}"
  }
  assert {
    condition     = keys(local.platform_agent_integration) == ["forges", "repositories"]
    error_message = "integration keys: ${jsonencode(keys(local.platform_agent_integration))}"
  }
}

run "gitlab_on_gitlab_com_names_no_host" {
  command = plan
  variables {
    gitops_forge = "gitlab"
    gitlab_repo  = "g/p"
  }
  assert {
    condition     = !contains(keys(local.gitlab_forges[0]), "host")
    error_message = "gitlab.com forge carries a host: ${jsonencode(local.gitlab_forges)}"
  }
}

run "gitlab_refuses_a_minter" {
  command = plan
  variables {
    gitops_forge         = "gitlab"
    gitlab_repo          = "g/p"
    enable_github_minter = true
    github_repo          = "acme/infra"
    github_app_id        = "1"
  }
  expect_failures = [helm_release.kube_agents]
}

run "gitlab_needs_a_repo" {
  command = plan
  variables {
    gitops_forge = "gitlab"
  }
  expect_failures = [helm_release.kube_agents]
}

run "the_forge_is_github_or_gitlab" {
  command = plan
  variables {
    gitops_forge = "bitbucket"
  }
  expect_failures = [var.gitops_forge]
}

run "the_host_is_bare" {
  command = plan
  variables {
    gitops_forge = "gitlab"
    gitlab_repo  = "g/p"
    gitops_host  = "https://gitlab.example.com"
  }
  expect_failures = [var.gitops_host]
}
