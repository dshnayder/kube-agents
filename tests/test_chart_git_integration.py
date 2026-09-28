"""The chart's forge declaration: one spelling reaches the CR, never two.

`spec.integration.git` is the declaration; `spec.integration.github` is a
deprecated alias for it with `provider: github`. The operator refuses a
PlatformAgent that sets both, because there is no precedence rule that would not
surprise somebody -- so the chart has to refuse it too, at `helm install`, where
the administrator can still see which values file set which field. A chart that
rendered both would produce a CR the API server rejects with an error naming
neither values key.

The minter guard is the same failure one layer down. minty issues GitHub App
installation tokens and nothing else, so `githubMinter.enabled` alongside a
non-GitHub provider is a contradiction. Rendered anyway, it surfaces as the
credential proxy handing the agent a token the forge it talks to does not
accept -- a runtime authentication error a long way from the values file that
caused it.

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
    def test_a_github_git_block_renders_as_the_alias(self):
        """`helm upgrade` never updates crds/, so an install upgraded that way
        serves a CRD with no `git` field and the API server would prune it --
        taking the repository with it, silently. A GitHub declaration therefore
        reaches the CR as `github`, which every CRD version accepts."""
        integration = _integration(
            "platformAgent.integration.git.repository=gke-labs/kube-agents"
        )
        self.assertEqual(
            integration.get("github"), {"gitRepo": "gke-labs/kube-agents"}
        )
        # Never both: the operator refuses a CR carrying both spellings.
        self.assertNotIn("git", integration)

    def test_the_deprecated_alias_still_renders_unchanged(self):
        """Existing values files are the reason the alias exists at all."""
        integration = _integration(
            "platformAgent.integration.github.org=gke-labs",
            "platformAgent.integration.github.gitRepo=gke-labs/kube-agents",
        )
        self.assertEqual(
            integration.get("github"),
            {"org": "gke-labs", "gitRepo": "gke-labs/kube-agents"},
        )
        self.assertNotIn("git", integration)

    def test_an_explicit_provider_and_github_host_map_onto_the_alias(self):
        integration = _integration(
            "platformAgent.integration.git.provider=github",
            "platformAgent.integration.git.host=github.com",
            "platformAgent.integration.git.namespace=gke-labs",
            "platformAgent.integration.git.repository=kube-agents",
        )
        self.assertEqual(
            integration.get("github"),
            {"org": "gke-labs", "gitRepo": "kube-agents"},
        )
        self.assertNotIn("git", integration)

    def test_a_host_github_does_not_serve_fails_rather_than_dropping(self):
        """The alias has no host field. Dropping a foreign host would seed
        `https://github.com/group/project` for a repository on gitlab.com --
        the rewrite the provider rules exist to refuse."""
        result = _render(
            _CR_TEMPLATE,
            "platformAgent.integration.git.host=gitlab.com",
            "platformAgent.integration.git.repository=group/project",
        )
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("platformAgent.integration.git.host", result.stderr)

    def test_declaring_both_spellings_fails_the_render(self):
        result = _render(
            _CR_TEMPLATE,
            "platformAgent.integration.git.repository=gke-labs/kube-agents",
            "platformAgent.integration.github.org=gke-labs",
        )
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("not both", result.stderr)

    def test_a_git_block_naming_no_repository_is_not_a_second_declaration(self):
        """`provider` and `host` alone name nothing the operator could seed, so
        beside the deprecated alias they are not a conflict and not a block of
        their own."""
        integration = _integration(
            "platformAgent.integration.git.provider=github",
            "platformAgent.integration.git.host=github.com",
            "platformAgent.integration.github.org=gke-labs",
        )
        self.assertEqual(integration, {"github": {"org": "gke-labs"}})
        self.assertEqual(
            _integration("platformAgent.integration.git.provider=github"), {}
        )

    def test_no_forge_declaration_renders_no_integration_key(self):
        """`integration: {}` is not the same as an absent integration: the
        operator reads a present-but-empty block as a declaration."""
        self.assertEqual(_integration(), {})

    def test_an_unregistered_provider_fails_the_render(self):
        """The CRD's enum would reject it at apply; the chart names the values
        key while the administrator is still looking at their values file."""
        result = _render(
            _CR_TEMPLATE, "platformAgent.integration.git.provider=gitlab"
        )
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("platformAgent.integration.git.provider", result.stderr)

    def test_the_minter_never_renders_for_a_non_github_provider(self):
        """Asserts the outcome, not which guard produced it.

        With `github` the only registered provider, `kube-agents.gitProvider`
        refuses `gitlab` before `github-minter.yaml`'s own check can fire. Both
        guards must hold: the minter one is what keeps a GitLab install from
        provisioning a GitHub App token minter once the registry widens.
        """
        result = _render(
            _MINTER_TEMPLATE,
            *_MINTER,
            "platformAgent.integration.git.provider=gitlab",
        )
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertNotIn("kind: Deployment", result.stdout)

    def test_the_no_repository_sentinel_does_not_collide_with_the_git_block(self):
        """`None` means no repository, so it is not a second declaration.

        Reading it as one would make the deprecated key collide with the `git`
        block that replaces it — blocking the migration for exactly the installs
        that opted out of a GitOps repository.
        """
        integration = _integration(
            "platformAgent.integration.github.gitRepo=None",
            "platformAgent.integration.git.repository=gke-labs/kube-agents",
        )
        self.assertEqual(
            integration.get("github"), {"gitRepo": "gke-labs/kube-agents"}
        )

    def test_the_minter_renders_for_github_and_for_no_declaration(self):
        for label, extra in (
            ("no declaration", ()),
            ("git provider github", ("platformAgent.integration.git.provider=github",)),
            ("deprecated alias", ("platformAgent.integration.github.org=gke-labs",)),
        ):
            with self.subTest(declaration=label):
                result = _render(_MINTER_TEMPLATE, *_MINTER, *extra)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("name: github-token-minter", result.stdout)


if __name__ == "__main__":
    unittest.main()
