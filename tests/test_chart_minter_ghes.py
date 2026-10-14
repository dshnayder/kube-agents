"""The token minter on an install whose GitHub forge is a GitHub Enterprise Server.

minty calls api.github.com unless SOURCE_SYSTEM_API_BASE_URL names another
REST API root (github-token-minter pkg/server/config.go). Its HTTP client is
Go's default transport, which reads the system roots: the bundle file in the
image and every certificate file in the directories SSL_CERT_DIR lists. So a
GitHub forge at a GitHub Enterprise Server host sets the URL, and a forge that
names a CA in caBundleRef mounts that Secret key into the minter and points
SSL_CERT_DIR at it. A github.com install renders neither.
"""

import pathlib
import shutil
import subprocess
import tempfile
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CHART = _REPO_ROOT / "charts" / "kube-agents"
_TEMPLATE = "templates/github-minter.yaml"
_P = "platformAgent.integration."

_BASE = (
    "platformAgent.harness.projectId=p",
    "platformAgent.harness.clusterName=c",
    "platformAgent.harness.location=us-central1",
    "githubMinter.enabled=true",
    "githubMinter.org=acme",
    "githubMinter.repo=acme/infra",
)


def _render(*sets: str, files: tuple[pathlib.Path, ...] = ()) -> subprocess.CompletedProcess:
    args = ["helm", "template", "t", str(_CHART), "--show-only", _TEMPLATE]
    for path in files:
        args += ["-f", str(path)]
    for value in _BASE + sets:
        args += ["--set", value]
    return subprocess.run(args, capture_output=True, text=True)


def _minter(result: subprocess.CompletedProcess) -> dict:
    if result.returncode != 0:
        raise AssertionError(f"helm template failed: {result.stderr}")
    for document in yaml.safe_load_all(result.stdout):
        if document and document.get("kind") == "Deployment":
            return document["spec"]["template"]["spec"]
    raise AssertionError("no minter Deployment rendered")


def _env(pod: dict) -> dict:
    return {e["name"]: e.get("value") for e in pod["containers"][0]["env"]}


def _ghes(*extra: str) -> tuple:
    return (
        f"{_P}forges[0].name=github",
        f"{_P}forges[0].provider=github",
        f"{_P}forges[0].host=GHE.example.com",
        f"{_P}forges[0].namespace=acme",
        f"{_P}repositories[0].forge=github",
        f"{_P}repositories[0].repository=infra",
        f"{_P}repositories[0].role=gitops",
        *extra,
    )


@unittest.skipUnless(shutil.which("helm"), "helm is not installed")
class MinterOnGitHubEnterpriseServerTest(unittest.TestCase):
    def test_a_github_enterprise_server_forge_points_the_minter_at_its_api(self):
        pod = _minter(_render(*_ghes()))
        env = _env(pod)
        self.assertEqual("https://ghe.example.com/api/v3", env["SOURCE_SYSTEM_API_BASE_URL"])
        # No CA named: the image's bundle only.
        self.assertNotIn("SSL_CERT_DIR", env)
        self.assertNotIn("ghes-ca", [v["name"] for v in pod["volumes"]])

    def test_the_forge_ca_is_mounted_into_the_minter_and_trusted_beside_the_bundle(self):
        pod = _minter(_render(*_ghes(f"{_P}forges[0].caBundleRef.name=ghe-ca",
                                     f"{_P}forges[0].caBundleRef.key=root.pem")))
        env = _env(pod)
        self.assertEqual("/etc/kube-agents/minter-ca", env["SSL_CERT_DIR"])
        [volume] = [v for v in pod["volumes"] if v["name"] == "ghes-ca"]
        self.assertEqual(
            {"secretName": "ghe-ca", "items": [{"key": "root.pem", "path": "ca.crt"}],
             "defaultMode": 0o444, "optional": True},
            volume["secret"],
        )
        [mount] = [m for m in pod["containers"][0]["volumeMounts"] if m["name"] == "ghes-ca"]
        self.assertEqual(("/etc/kube-agents/minter-ca", True, None),
                         (mount["mountPath"], mount.get("readOnly"), mount.get("subPath")))

    def test_a_github_com_install_renders_neither(self):
        for sets in (
            (),
            (f"{_P}github.org=acme", f"{_P}github.gitRepo=acme/infra"),
            (f"{_P}forges[0].name=github", f"{_P}forges[0].host=www.github.com", f"{_P}forges[0].namespace=acme"),
        ):
            with self.subTest(sets=sets):
                pod = _minter(_render(*sets))
                env = _env(pod)
                self.assertNotIn("SOURCE_SYSTEM_API_BASE_URL", env)
                self.assertNotIn("SSL_CERT_DIR", env)
                self.assertNotIn("ghes-ca", [v["name"] for v in pod["volumes"]])

    def test_a_values_file_can_replace_the_github_alias_with_the_enterprise_forge(self):
        """The documented Terraform path: the composition writes the github alias,
        and an extra values document sets it to null and declares the forge. Helm
        removes a key set to null in a later values document, so the two
        spellings never meet."""
        with tempfile.TemporaryDirectory() as tmp:
            first = pathlib.Path(tmp) / "computed.yaml"
            first.write_text(yaml.safe_dump(
                {"platformAgent": {"integration": {"github": {"org": "acme", "gitRepo": "acme/infra"}}}}))
            extra = pathlib.Path(tmp) / "extra.yaml"
            extra.write_text(yaml.safe_dump({"platformAgent": {"integration": {
                "github": None,
                "forges": [{"name": "github", "provider": "github", "host": "ghe.example.com",
                            "namespace": "acme", "caBundleRef": {"name": "ghe-ca"}}],
                "repositories": [{"forge": "github", "repository": "infra", "role": "gitops"}],
            }}}))
            minter = _render(files=(first, extra))
            cr = subprocess.run(
                ["helm", "template", "t", str(_CHART), "--show-only", "templates/platform-agent-cr.yaml",
                 "-f", str(first), "-f", str(extra), *[a for s in _BASE for a in ("--set", s)]],
                capture_output=True, text=True,
            )
        self.assertEqual("https://ghe.example.com/api/v3", _env(_minter(minter))["SOURCE_SYSTEM_API_BASE_URL"])
        self.assertEqual(cr.returncode, 0, cr.stderr)
        integration = yaml.safe_load(cr.stdout)["spec"]["integration"]
        self.assertNotIn("github", integration)
        self.assertEqual("ghe.example.com", integration["forges"][0]["host"])
        self.assertEqual({"name": "ghe-ca"}, integration["forges"][0]["caBundleRef"])

    def test_the_minter_egress_admits_any_address_on_https(self):
        result = _render(*_ghes())
        policy = next(d for d in yaml.safe_load_all(result.stdout) if d and d.get("kind") == "NetworkPolicy")
        https = [r for r in policy["spec"]["egress"]
                 if any(p.get("port") == 443 for p in r.get("ports", []))]
        self.assertTrue(any(peer.get("ipBlock") == {"cidr": "0.0.0.0/0"} for r in https for peer in r["to"]))


if __name__ == "__main__":
    unittest.main()
