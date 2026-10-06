/*
Copyright 2026.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package v1alpha1

import (
	"encoding/json"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
)

func gitlabProvider(t *testing.T) *GitProvider {
	t.Helper()
	provider, err := LookupGitProvider(GitProviderGitLab)
	if err != nil {
		t.Fatalf("LookupGitProvider(gitlab) = %v", err)
	}
	return provider
}

// glForge is a GitLab forge with a credential, which every valid one has.
func glForge(name, host, namespace string) ForgeSpec {
	return ForgeSpec{
		Name: name, Provider: GitProviderGitLab, Host: host, Namespace: namespace,
		CredentialsRef: &corev1.LocalObjectReference{Name: name + "-token"},
	}
}

func TestGitLabResolvesNestedGroupsOnGitLabCom(t *testing.T) {
	provider := gitlabProvider(t)
	for repo, want := range map[string]string{
		"https://gitlab.com/acme/platform/infra.git": "https://gitlab.com/acme/platform/infra",
		"git@gitlab.com:acme/infra.git":              "https://gitlab.com/acme/infra",
		"gitlab.com/acme/platform/infra":             "https://gitlab.com/acme/platform/infra",
		"acme/platform/infra":                        "https://gitlab.com/acme/platform/infra",
		"infra":                                      "https://gitlab.com/acme/infra",
	} {
		ref, err := provider.Resolve("", repo, "acme")
		if err != nil {
			t.Errorf("Resolve(%q) = %v", repo, err)
			continue
		}
		if ref.URL() != want {
			t.Errorf("Resolve(%q) = %q, expected %q", repo, ref.URL(), want)
		}
	}
	if _, err := provider.Resolve("", "infra", ""); err == nil {
		t.Error("a bare project name with no namespace resolved")
	}
	if err := provider.ValidateNamespace("acme/platform.team/sub_group"); err != nil {
		t.Errorf("a nested group path was refused: %v", err)
	}
	for _, namespace := range []string{"-acme", "acme.", "acme//sub", "acme/.hidden"} {
		if err := provider.ValidateNamespace(namespace); err == nil {
			t.Errorf("ValidateNamespace(%q) accepted a path GitLab refuses", namespace)
		}
	}
}

// A GitLab forge never takes a GitHub repository, nor the reverse: each
// refuses the other's host rather than rewriting it onto its own.
func TestGitLabAndGitHubRefuseEachOthersHosts(t *testing.T) {
	gitlab := gitlabProvider(t)
	github, _ := LookupGitProvider(GitProviderGitHub)
	if _, err := gitlab.Resolve("", "https://github.com/acme/infra", ""); err == nil {
		t.Error("gitlab accepted a github.com repository")
	}
	if _, err := github.Resolve("", "https://gitlab.com/acme/infra", ""); err == nil {
		t.Error("github accepted a gitlab.com repository")
	}
	for _, host := range []string{"github.com", "WWW.GitHub.com", "ssh.github.com"} {
		if err := gitlab.ValidateHost(host); err == nil {
			t.Errorf("a gitlab forge was accepted at the GitHub host %q", host)
		}
	}
}

// A self-managed instance is the forge's declared host, and a repository on it
// must name that host or none. The declared host never replaces one the
// repository names: gitlab.com on a forge at gitlab.example.com is refused,
// because moving it would hand a gitlab.com project's name to another server.
func TestASelfManagedHostIsTheForgesOwnAndNeverRewritesAnother(t *testing.T) {
	provider := gitlabProvider(t)
	const host = "GitLab.Example.com"
	for repo, want := range map[string]string{
		"https://gitlab.example.com/acme/infra": "https://gitlab.example.com/acme/infra",
		"git@gitlab.example.com:acme/infra.git": "https://gitlab.example.com/acme/infra",
		"gitlab.example.com/acme/sub/infra":     "https://gitlab.example.com/acme/sub/infra",
		"acme/infra":                            "https://gitlab.example.com/acme/infra",
	} {
		ref, err := provider.Resolve(host, repo, "")
		if err != nil {
			t.Errorf("Resolve(%q) on a self-managed forge = %v", repo, err)
			continue
		}
		if ref.URL() != want {
			t.Errorf("Resolve(%q) = %q, expected %q", repo, ref.URL(), want)
		}
	}
	for _, repo := range []string{
		"https://gitlab.com/acme/infra",
		"gitlab.com/acme/infra",
		"https://other.example.com/acme/infra",
	} {
		if ref, err := provider.Resolve(host, repo, ""); err == nil {
			t.Errorf("Resolve(%q) on a forge at %s = %q, expected a refusal", repo, host, ref.URL())
		}
	}
	// And the reverse: a forge at gitlab.com does not take the instance's.
	if _, err := provider.Resolve("", "https://gitlab.example.com/acme/infra", ""); err == nil {
		t.Error("a gitlab.com forge accepted a self-managed instance's repository")
	}
	for _, bad := range []string{"localhost", "gitlab_example.com", "-gitlab.example.com"} {
		if err := provider.ValidateHost(bad); err == nil {
			t.Errorf("ValidateHost(%q) accepted something that is not a hostname", bad)
		}
	}
}

func TestEgressAddsASelfManagedHostAsALiteral(t *testing.T) {
	got := ForgeEgressPatterns(&IntegrationSpec{Forges: []ForgeSpec{glForge("gl", "gitlab.example.com", "acme")}})
	want := "github.com,*.github.com,*.githubusercontent.com,gitlab.com,*.gitlab.com,gitlab.example.com"
	if strings.Join(got, ",") != want {
		t.Errorf("ForgeEgressPatterns = %v, expected %s", got, want)
	}
}

// Without the Secret the broker has no token to call GitLab with, so the forge
// is refused at its credentialsRef and nothing on it is seeded.
func TestAGitLabForgeWithoutCredentialsIsRefused(t *testing.T) {
	in := &IntegrationSpec{
		Forges:       []ForgeSpec{{Name: "gl", Provider: GitProviderGitLab, Namespace: "acme"}},
		Repositories: []RepositorySpec{repo("gl", "infra", RepositoryRoleGitOps)},
	}
	resolved, err := in.ResolveGit()
	if err != nil {
		t.Fatal(err)
	}
	problems := resolved.Problems()
	if len(problems) != 1 || problems[0].Path.String() != "forges[0].credentialsRef" {
		t.Fatalf("Problems() = %v, expected one at forges[0].credentialsRef", problems)
	}
	if !strings.Contains(problems[0].Err.Error(), `"token"`) {
		t.Errorf("the refusal does not name the Secret key: %v", problems[0].Err)
	}
	if got := resolved.Accepted(RepositoryRoleGitOps); len(got) != 0 {
		t.Errorf("a repository on a forge without credentials was accepted: %v", got)
	}
	in.Forges[0] = glForge("gl", "", "acme")
	resolved, _ = in.ResolveGit()
	if problems := resolved.Problems(); len(problems) != 0 {
		t.Errorf("Problems() with credentials = %v", problems)
	}
	entry, err := resolved.Accepted(RepositoryRoleGitOps)[0].ManagedRepoEntry()
	if err != nil || entry.Type != GitProviderGitLab || entry.URL != "https://gitlab.com/acme/infra" {
		t.Errorf("ManagedRepoEntry = %+v, %v", entry, err)
	}
}

// A GitHub-only declaration hands the broker no configuration at all, which is
// what keeps such an install exactly as it was.
func TestBrokerForgesIsNilWithoutAForgeThatNeedsOne(t *testing.T) {
	for name, in := range map[string]*IntegrationSpec{
		"alias":                   {GitHub: &GitHubSpec{Org: "acme", GitRepo: "infra"}},
		"github list":             {Forges: []ForgeSpec{ghForge("github", "acme")}},
		"gitlab with no secret":   {Forges: []ForgeSpec{{Name: "gl", Provider: GitProviderGitLab}}},
		"gitlab at github's host": {Forges: []ForgeSpec{glForge("gl", "github.com", "")}},
		// Valid, and nothing to serve: no namespace, no repository. An entry
		// for it would have to serve the whole host, which the broker refuses
		// to infer.
		"gitlab with nothing to serve": {Forges: []ForgeSpec{glForge("gl", "", "")}},
	} {
		resolved, _ := in.ResolveGit()
		if got := resolved.BrokerForges("/creds"); got != nil {
			t.Errorf("%s: BrokerForges = %+v, expected none", name, got)
		}
	}
	var none *ResolvedIntegration
	if none.BrokerForges("/creds") != nil {
		t.Error("BrokerForges on no declaration is not nil")
	}
}

// One entry per host: the broker refuses two forges claiming one, so the
// second GitLab forge at gitlab.com is refused at admission, its repository is
// withheld, and the configuration carries the first.
func TestBrokerForgesListsGitHubFirstAndEachGitLabHostOnce(t *testing.T) {
	in := &IntegrationSpec{
		Forges: []ForgeSpec{
			ghForge("github", "acme"),
			glForge("gl", "", "acme"),
			glForge("gl-again", "gitlab.com", "other"),
			glForge("onprem", "gitlab.example.com", ""),
		},
		Repositories: []RepositorySpec{
			repo("gl", "infra", RepositoryRoleGitOps),
			repo("gl", "https://gitlab.com/platform/tools/app", RepositoryRoleManaged),
			repo("gl", "docs/runbooks", RepositoryRoleContext),
			repo("onprem", "team/svc", RepositoryRoleManaged),
			repo("gl-again", "other/thing", RepositoryRoleManaged),
		},
	}
	resolved, err := in.ResolveGit()
	if err != nil {
		t.Fatal(err)
	}
	got, err := json.Marshal(resolved.BrokerForges("/creds"))
	if err != nil {
		t.Fatal(err)
	}
	if problems := resolved.Problems(); len(problems) != 1 || problems[0].Path.String() != "forges[2].host" {
		t.Errorf("Problems() = %v, expected the second gitlab.com forge refused at its host", problems)
	}
	for _, r := range resolved.Accepted(RepositoryRoleManaged) {
		if r.ForgeName == "gl-again" {
			t.Error("a repository on the refused second forge was accepted")
		}
	}
	want := `[{"provider":"github","host":"github.com"},` +
		`{"provider":"gitlab","host":"gitlab.com","tokenPath":"/creds/gl/token","allowedPaths":["acme","docs","platform/tools"]},` +
		`{"provider":"gitlab","host":"gitlab.example.com","tokenPath":"/creds/onprem/token","allowedPaths":["team"]}]`
	if string(got) != want {
		t.Errorf("BrokerForges =\n %s\nexpected\n %s", got, want)
	}
}
