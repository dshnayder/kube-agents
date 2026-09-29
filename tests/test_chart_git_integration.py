"""The chart's forge declaration: one spelling reaches the CR, never two.

`spec.integration.forges` and `spec.integration.repositories` are the
declaration; `spec.integration.github` is a deprecated alias for one forge with
`provider: github` and one gitops repository. The operator refuses a
PlatformAgent that sets both, because there is no precedence rule that would not
surprise somebody -- so the chart has to refuse it too, at `helm install`, where
the administrator can still see which values file set which field. A chart that
rendered both would produce a CR the API server rejects with an error naming
neither values key.

The minter guard is the same failure one layer down. minty issues GitHub App
installation tokens and nothing else, so `githubMinter.enabled` alongside
forges none of which is GitHub is a contradiction. Rendered anyway, it surfaces
as the credential proxy handing the agent a token no forge it talks to accepts
-- a runtime authentication error a long way from the values file that caused
it.

See docs/designs/version-control-support.md §6.
"""

import pathlib
import shutil
import subprocess
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CHART = _REPO_ROOT / "charts" / "kube-agents"

# The three harness fields every render needs, whatever it is testing.
_HARNESS = (
    "platformAgent.harness.projectId=p",
    "platformAgent.harness.clusterName=c",
    "platformAgent.harness.location=us-central1",
)

# githubMinter's own required fields, so a minter render fails on the guard
# under test rather than on a missing value.
_MINTER = (
    "githubMinter.enabled=true",
    "githubMinter.org=gke-labs",
    "githubMinter.repo=gke-labs/kube-agents",
)

_CR_TEMPLATE = "templates/platform-agent-cr.yaml"
_MINTER_TEMPLATE = "templates/github-minter.yaml"

_P = "platformAgent.integration."


def _forge(i: int, **fields: str) -> tuple:
    return tuple(f"{_P}forges[{i}].{k}={v}" for k, v in fields.items())


def _repo(i: int, **fields: str) -> tuple:
    return tuple(f"{_P}repositories[{i}].{k}={v}" for k, v in fields.items())


def _render(template: str, *sets: str) -> subprocess.CompletedProcess:
    args = ["helm", "template", "t", str(_CHART), "--show-only", template]
    for value in _HARNESS + sets:
        args += ["--set", value]
    return subprocess.run(args, capture_output=True, text=True)


def _integration(*sets: str) -> dict:
    result = _render(_CR_TEMPLATE, *sets)
    if result.returncode != 0:
        raise AssertionError(f"helm template failed: {result.stderr}")
    return (yaml.safe_load(result.stdout)["spec"]).get("integration") or {}


@unittest.skipUnless(shutil.which("helm"), "helm is not installed")
class ChartGitIntegrationTest(unittest.TestCase):
    def test_a_declaration_the_alias_can_carry_renders_as_the_alias(self):
        """`helm upgrade` never updates crds/, so an install upgraded that way
        serves a CRD with no `forges` field and the API server would prune it --
        taking the repositories with it, silently. One GitHub forge with one
        gitops repository therefore reaches the CR as `github`, which every CRD
        version accepts."""
        integration = _integration(
            *_forge(0, name="github", provider="github", host="github.com",
                    namespace="gke-labs"),
            *_repo(0, forge="github", repository="kube-agents", role="gitops"),
        )
        self.assertEqual(
            integration.get("github"),
            {"org": "gke-labs", "gitRepo": "kube-agents"},
        )
        # Never both: the operator refuses a CR carrying both spellings.
        self.assertNotIn("forges", integration)
        self.assertNotIn("repositories", integration)

    def test_the_deprecated_alias_still_renders_unchanged(self):
        """Existing values files are the reason the alias exists at all."""
        integration = _integration(
            f"{_P}github.org=gke-labs",
            f"{_P}github.gitRepo=gke-labs/kube-agents",
        )
        self.assertEqual(
            integration,
            {"github": {"org": "gke-labs", "gitRepo": "gke-labs/kube-agents"}},
        )

    def test_what_the_alias_cannot_carry_renders_as_lists(self):
        """A managed or context repository, a second forge, a credentialsRef or
        a per-repository namespace has no spelling in the alias. Folding any of
        them into it would drop the part it cannot say."""
        gitops = _repo(0, forge="github", repository="infra", role="gitops")
        cases = {
            "managed repository": (
                *_forge(0, name="github", namespace="gke-labs"),
                *gitops,
                *_repo(1, forge="github", repository="app", role="managed"),
            ),
            "context repository": (
                *_forge(0, name="github", namespace="gke-labs"),
                *_repo(0, forge="github", repository="runbooks", role="context"),
            ),
            "two forges": (
                *_forge(0, name="github", namespace="gke-labs"),
                *_forge(1, name="github-ssh", host="ssh.github.com"),
                *gitops,
            ),
            "credentialsRef": (
                *_forge(0, name="github", namespace="gke-labs"),
                f"{_P}forges[0].credentialsRef.name=token",
                *gitops,
            ),
            "namespace override": (
                *_forge(0, name="github", namespace="gke-labs"),
                *_repo(0, forge="github", repository="infra", role="gitops",
                       namespace="other-org"),
            ),
        }
        for label, sets in cases.items():
            with self.subTest(declaration=label):
                integration = _integration(*sets)
                self.assertNotIn("github", integration)
                self.assertIn("forges", integration)
                self.assertEqual(integration["forges"][0]["name"], "github")
                self.assertEqual(integration["forges"][0]["provider"], "github")

        integration = _integration(*cases["managed repository"])
        self.assertEqual(
            integration["repositories"],
            [
                {"forge": "github", "repository": "infra", "role": "gitops"},
                {"forge": "github", "repository": "app", "role": "managed"},
            ],
        )
        integration = _integration(*cases["credentialsRef"])
        self.assertEqual(
            integration["forges"][0]["credentialsRef"], {"name": "token"}
        )
        # Whole lists, so a host or a namespace the render drops is caught: a
        # lost override would qualify infra under gke-labs, another repository.
        integration = _integration(*cases["two forges"])
        self.assertEqual(
            integration["forges"],
            [
                {"name": "github", "provider": "github", "namespace": "gke-labs"},
                {"name": "github-ssh", "provider": "github", "host": "ssh.github.com"},
            ],
        )
        integration = _integration(*cases["namespace override"])
        self.assertEqual(
            integration["forges"],
            [{"name": "github", "provider": "github", "namespace": "gke-labs"}],
        )
        self.assertEqual(
            integration["repositories"],
            [
                {"forge": "github", "repository": "infra", "role": "gitops",
                 "namespace": "other-org"},
            ],
        )

    def test_a_host_github_does_not_serve_fails_rather_than_dropping(self):
        """The alias has no host field. Dropping a foreign host would seed
        `https://github.com/group/project` for a repository on gitlab.com --
        the rewrite the provider rules exist to refuse."""
        result = _render(
            _CR_TEMPLATE,
            *_forge(0, name="github", host="gitlab.com"),
            *_repo(0, forge="github", repository="group/project", role="gitops"),
        )
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(f"{_P}forges[0].host", result.stderr)

    def test_a_namespace_github_refuses_fails_naming_the_forge(self):
        """A single-forge declaration renders as the alias, whose `org` carries
        GitHub's grammar in the CRD. Unchecked, the API server would refuse
        `spec.integration.github.org` -- a key no values file set."""
        for namespace in ("my_org", "my.org", "a" * 40):
            with self.subTest(namespace=namespace):
                result = _render(
                    _CR_TEMPLATE,
                    *_forge(0, name="github", namespace=namespace),
                    *_repo(0, forge="github", repository="infra", role="gitops"),
                )
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn(f"{_P}forges[0].namespace", result.stderr)

    def test_an_empty_repository_fails_the_render(self):
        """Under `gitops` an empty repository folds into the alias as no
        `gitRepo` at all, and the declaration vanishes without an error."""
        for role in ("gitops", "managed"):
            with self.subTest(role=role):
                result = _render(
                    _CR_TEMPLATE,
                    *_forge(0, name="github", namespace="gke-labs"),
                    *_repo(0, forge="github", repository="", role=role),
                )
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn(f"{_P}repositories[0].repository is required", result.stderr)

    def test_a_bare_repository_with_no_namespace_fails_the_render(self):
        """One forge and one gitops repository fold into the alias, so the
        operator would refuse `github.gitRepo`, a key the values file never
        set. A namespace on the entry, or a qualified name, is enough."""
        result = _render(
            _CR_TEMPLATE,
            *_forge(0, name="github"),
            *_repo(0, forge="github", repository="infra", role="gitops"),
        )
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(f"{_P}repositories[0].repository", result.stderr)
        for repo in (
            {"repository": "infra", "namespace": "gke-labs"},
            {"repository": "gke-labs/infra"},
        ):
            with self.subTest(**repo):
                _integration(
                    *_forge(0, name="github"),
                    *_repo(0, forge="github", role="gitops", **repo),
                )
        # An scp remote with one path segment names a host, not a bare name: a
        # namespace would not fix it, so it reaches the operator as the lists
        # and is refused there, on depth, against the entry written.
        integration = _integration(
            *_forge(0, name="github"),
            *_repo(0, forge="github", repository="git@github.com:infra", role="gitops"),
        )
        self.assertNotIn("github", integration)
        self.assertEqual(integration["repositories"][0]["repository"], "git@github.com:infra")

    def test_a_declaration_the_alias_would_misname_renders_as_the_lists(self):
        """A repository the operator would refuse for GitHub -- another host, a
        URL or remote missing its owner, a deeper path, an owner GitHub would
        not accept, or a name that is a traversal or a flag -- folded into
        `github.gitRepo` would be refused against that key; a forge with no
        namespace and no repository would fold into nothing. Each renders as
        the lists."""
        for repo in (
            "https://gitlab.com/group/project",
            "https://github.com/infra",
            "https://github.com.evil.example/gke-labs/infra",
            "gke-labs/infra/extra",
            "-org/infra",
            "org-/infra",
            "a" * 40 + "/infra",
            "gke-labs/-x",
            "gke-labs/..",
            "gke-labs/.",
        ):
            with self.subTest(repo=repo):
                integration = _integration(
                    *_forge(0, name="github", namespace="gke-labs"),
                    *_repo(0, forge="github", repository=repo, role="gitops"),
                )
                self.assertNotIn("github", integration)
                self.assertEqual(integration["repositories"][0]["repository"], repo)
        integration = _integration(*_forge(0, name="github", host="ssh.github.com"))
        self.assertNotIn("github", integration)
        self.assertEqual(integration["forges"][0]["name"], "github")

    def test_a_name_github_accepts_still_folds(self):
        """The tightened fold must not unfold what the operator accepts: a
        leading dot, a github.com URL or remote, and an empty credentialsRef
        name included."""
        for repo in (
            "gke-labs/.github",
            "gke-labs/my_repo.v2",
            "infra",
            "https://github.com/gke-labs/infra",
            "https://www.github.com/gke-labs/infra.git",
            "git@github.com:gke-labs/infra",
        ):
            with self.subTest(repo=repo):
                integration = _integration(
                    *_forge(0, name="github", namespace="gke-labs"),
                    *_repo(0, forge="github", repository=repo, role="gitops"),
                )
                self.assertEqual(integration["github"]["gitRepo"], repo)
        integration = _integration(
            *_forge(0, name="github", namespace="gke-labs"),
            f"{_P}forges[0].credentialsRef.name=",
            *_repo(0, forge="github", repository="infra", role="gitops"),
        )
        self.assertEqual(integration["github"], {"org": "gke-labs", "gitRepo": "infra"})

    def test_an_empty_credentials_name_is_not_rendered(self):
        integration = _integration(
            *_forge(0, name="a", namespace="gke-labs"),
            f"{_P}forges[0].credentialsRef.name=",
            *_forge(1, name="b", namespace="kubernetes"),
        )
        self.assertNotIn("credentialsRef", integration["forges"][0])

    def test_two_forges_with_one_name_fail_the_render(self):
        result = _render(
            _CR_TEMPLATE,
            *_forge(0, name="github", namespace="gke-labs"),
            *_forge(1, name="github", host="ssh.github.com"),
            *_repo(0, forge="github", repository="infra", role="gitops"),
        )
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(f'{_P}forges[1].name "github" is already declared', result.stderr)

    def test_declaring_both_spellings_fails_the_render(self):
        result = _render(
            _CR_TEMPLATE,
            *_forge(0, name="github"),
            f"{_P}github.org=gke-labs",
        )
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("not both", result.stderr)

    def test_a_repository_on_an_undeclared_forge_fails_the_render(self):
        result = _render(
            _CR_TEMPLATE,
            *_forge(0, name="github"),
            *_repo(0, forge="gitlab", repository="group/project", role="gitops"),
        )
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(f"{_P}repositories[0].forge", result.stderr)

    def test_the_no_repository_sentinel_in_a_list_fails_the_render(self):
        """`None` is the alias's "no repository". In a list it would render as
        the alias for a gitops entry -- silently meaning nothing -- and as a
        repository named None for any other role."""
        for role in ("gitops", "managed"):
            with self.subTest(role=role):
                result = _render(
                    _CR_TEMPLATE,
                    *_forge(0, name="github", namespace="gke-labs"),
                    *_repo(0, forge="github", repository="None", role=role),
                )
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn(f"{_P}repositories[0].repository", result.stderr)

    def test_no_forge_declaration_renders_no_integration_key(self):
        """With no forge declared the chart writes none of the three git keys.
        `forges: []` is not the same as no `forges`: CEL's has() counts an
        empty list as set, so an empty list beside `github` is refused as two
        spellings."""
        integration = _integration()
        for key in ("github", "forges", "repositories"):
            self.assertNotIn(key, integration)

    def test_an_unregistered_provider_fails_the_render(self):
        """The CRD's enum would reject it at apply; the chart names the values
        key while the administrator is still looking at their values file."""
        result = _render(
            _CR_TEMPLATE, *_forge(0, name="gitlab", provider="gitlab")
        )
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(f"{_P}forges[0].provider", result.stderr)

    def test_the_minter_never_renders_without_a_github_forge(self):
        """Asserts the outcome, not which guard produced it.

        With `github` the only registered provider,
        `kube-agents.forgeProviders` refuses `gitlab` before
        `github-minter.yaml`'s own check can fire. Both guards must hold: the
        minter one is what keeps a GitLab-only install from provisioning a
        GitHub App token minter once the registry widens.
        """
        result = _render(
            _MINTER_TEMPLATE,
            *_MINTER,
            *_forge(0, name="gitlab", provider="gitlab"),
        )
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertNotIn("kind: Deployment", result.stdout)

    def test_the_no_repository_sentinel_does_not_collide_with_the_lists(self):
        """`None` means no repository, so it is not a second declaration.

        Reading it as one would make the deprecated key collide with the lists
        that replace it -- blocking the migration for exactly the installs that
        opted out of a GitOps repository.
        """
        integration = _integration(
            f"{_P}github.gitRepo=None",
            *_forge(0, name="github", namespace="gke-labs"),
            *_repo(0, forge="github", repository="kube-agents", role="gitops"),
        )
        self.assertEqual(
            integration.get("github"),
            {"org": "gke-labs", "gitRepo": "kube-agents"},
        )

    def test_the_minter_renders_for_github_and_for_no_declaration(self):
        for label, extra in (
            ("no declaration", ()),
            ("github forge", _forge(0, name="github", provider="github")),
            ("deprecated alias", (f"{_P}github.org=gke-labs",)),
        ):
            with self.subTest(declaration=label):
                result = _render(_MINTER_TEMPLATE, *_MINTER, *extra)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("name: github-token-minter", result.stdout)


if __name__ == "__main__":
    unittest.main()
