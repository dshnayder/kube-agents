"""GitLab as the installer's GitOps forge.

A GitLab install names a project path and a Kubernetes Secret holding an
access token. The installer takes the token from a no-echo prompt or a file
and pipes it into that Secret; it is never in argv, an exported variable, a
file the installer writes, its output, the tfvars or the Terraform state. The
sentinel tests below hold it to that by planting a recognisable token and
looking for it everywhere a stubbed kubectl and the installer can leave it.

A GitHub install is unchanged: no forge keys in its tfvars or install.env.
"""

import base64
import os
import pathlib
import pty
import re
import select
import shlex
import subprocess
import tempfile
import time
import unittest

from tests.test_install_script import (
    _INSTALL_SH,
    _INSTALLER_COMMON,
    _REPO_ROOT,
    _run_installer_bash,
)
# The module, not the class: a TestCase imported by name is collected here too.
from tests import test_installer_common as _installer_common
from tests.testing.common import get_isolated_test_env

_SENTINEL = "glpat-SENTINEL-7d1f0c9e4b2a"
_FULL_INSTALL = _REPO_ROOT / "terraform" / "examples" / "full-install"

# A kubectl that records every call's argv and environment, answers `get
# secret` as absent, renders `create secret` from the --from-file source the
# way kubectl does (base64), and keeps what `apply -f -` was sent.
_KUBECTL_STUB = r"""#!/usr/bin/env bash
log="$STUB_DIR/kubectl.log"
# $$, not a count: the two halves of a pipeline run at once.
n=$$
printf '%s\n' "$@" > "$STUB_DIR/call.$n.argv"
env > "$STUB_DIR/call.$n.env"
echo "kubectl $*" >> "$log"
case "$1 $2" in
  "get secret") exit "${STUB_SECRET_EXISTS_RC:-1}" ;;
  "create secret")
    src=""
    for a in "$@"; do case "$a" in --from-file=token=*) src="${a#--from-file=token=}" ;; esac; done
    val=$(cat "$src")
    printf 'apiVersion: v1\nkind: Secret\ndata:\n  token: %s\n' "$(printf '%s' "$val" | base64 | tr -d '\n')"
    ;;
  "apply -n") cat > "$STUB_DIR/applied.$n.yaml" ;;
esac
exit 0
"""


def _write_stub(stub_dir):
    bin_dir = stub_dir / "bin"
    bin_dir.mkdir()
    k = bin_dir / "kubectl"
    k.write_text(_KUBECTL_STUB)
    k.chmod(0o755)
    return bin_dir


class GitLabTfvarsTest(unittest.TestCase):
    _run = _installer_common.InstallerCommonTest._run

    def _tfvars(self, env):
        with tempfile.TemporaryDirectory() as out_dir:
            dest = pathlib.Path(out_dir) / "terraform.tfvars"
            proc = self._run(
                f'write_tfvars_from_state "{dest}"; echo "rc=$?"',
                env={"API_SERVER_KEY": "k", **env},
                describe_stub=_installer_common._autopilot_describe_stub(),
            )
            self.assertIn("rc=0", proc.stdout, proc.stderr)
            return dest.read_text()

    def test_gitlab_renders_the_forge_and_no_minter(self):
        content = self._tfvars(
            {
                "GITOPS_FORGE": "gitlab",
                "GITOPS_HOST": "gitlab.example.com",
                "GITOPS_REPO": "platform/infra/gitops",
                "GITLAB_TOKEN_SECRET": "gl-token",
                # Left over from a GitHub install: must not arm the minter.
                "GITOPS_ORG": "acme",
                "GITHUB_APP_ID": "123",
            }
        )
        self.assertIn('gitops_forge              = "gitlab"', content)
        self.assertIn('gitops_host               = "gitlab.example.com"', content)
        self.assertIn('gitlab_repo               = "platform/infra/gitops"', content)
        self.assertIn('gitlab_credentials_secret = "gl-token"', content)
        self.assertNotIn("github_repo", content)
        self.assertRegex(content, r"enable_github_minter\s*=\s*false")

    def test_gitlab_secret_name_defaults(self):
        content = self._tfvars({"GITOPS_FORGE": "gitlab", "GITOPS_REPO": "g/p"})
        self.assertIn('gitlab_credentials_secret = "gitlab-forge-token"', content)
        self.assertIn('gitops_host               = ""', content)

    def test_github_tfvars_carry_no_forge_keys(self):
        github = {"GITOPS_ORG": "acme", "GITOPS_REPO": "infra", "GITHUB_APP_ID": "123"}
        unset = self._tfvars(github)
        explicit = self._tfvars({**github, "GITOPS_FORGE": "github"})
        self.assertEqual(unset, explicit)
        self.assertIn('github_repo = "acme/infra"', unset)
        for key in ("gitops_forge", "gitops_host", "gitlab_repo", "gitlab_credentials_secret"):
            self.assertNotIn(key, unset)


class GitLabFlagsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self._tmp = pathlib.Path(tmp.name)
        self._empty_install_env = self._tmp / "install.env"
        self._empty_install_env.write_text("")

    def _run_install_func(self, func_call, env=None):
        # installer_common.sh is sourced by main before any of these run.
        setup = f'source "{_INSTALLER_COMMON}"\nKUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n{func_call}\n'
        overrides = {"KUBE_AGENTS_INSTALL_ENV": str(self._empty_install_env)}
        overrides.update(env or {})
        return _run_installer_bash(setup, get_isolated_test_env(overrides=overrides))

    def test_parse_args_takes_the_four_flags(self):
        proc = self._run_install_func(
            "parse_args --gitops-forge=gitlab --gitops-host=git.corp.example --gitlab-token-file=/x/tok "
            "--gitlab-token-secret=s1; "
            'echo "f=$PARAM_GITOPS_FORGE h=$PARAM_GITOPS_HOST t=$PARAM_GITLAB_TOKEN_FILE s=$PARAM_GITLAB_TOKEN_SECRET"'
        )
        self.assertIn("f=gitlab h=git.corp.example t=/x/tok s=s1", proc.stdout, proc.stderr)

    def test_there_is_no_token_value_flag(self):
        source = _INSTALL_SH.read_text()
        self.assertNotRegex(source, r"--gitlab-token=")
        self.assertNotIn("PARAM_GITLAB_TOKEN=", source)
        self.assertNotRegex(source, r"\bGITLAB_TOKEN=")

    def _validate(self, assignments):
        body = "".join(f"{k}={shlex.quote(v)}; " for k, v in assignments.items())
        return self._run_install_func(
            f'{body}validate_gitops_forge_flags && rc=0 || rc=$?; echo "rc=$rc repo=$PARAM_GITOPS_REPO"'
        )

    def _gitlab(self, **extra):
        base = {
            "PARAM_GITOPS_FORGE": "gitlab",
            "PARAM_GITOPS_REPO": "group/sub/project",
            "PARAM_GITLAB_TOKEN_SECRET": "gitlab-forge-token",
            "PARAM_GITHUB_APP_ID": "",
            "PARAM_GITHUB_PEM_PATH": "",
        }
        base.update(extra)
        return base

    def test_validator_accepts(self):
        tok = self._tmp / "tok"
        tok.write_text("x")
        for case, want_repo in (
            (self._gitlab(), "group/sub/project"),
            (self._gitlab(PARAM_GITOPS_HOST="gitlab.corp.example"), "group/sub/project"),
            (self._gitlab(PARAM_GITOPS_REPO="https://gitlab.com/a/b.git"), "a/b"),
            (self._gitlab(PARAM_GITOPS_REPO="a/b/"), "a/b"),
            (self._gitlab(PARAM_GITLAB_TOKEN_FILE=str(tok)), "group/sub/project"),
            ({"PARAM_GITOPS_FORGE": "github", "PARAM_GITOPS_REPO": "infra"}, "infra"),
        ):
            with self.subTest(case=case):
                proc = self._validate(case)
                self.assertIn(f"rc=0 repo={want_repo}", proc.stdout, proc.stderr + proc.stdout)

    def test_validator_refuses(self):
        for case in (
            {"PARAM_GITOPS_FORGE": "bitbucket"},
            {"PARAM_GITOPS_FORGE": "github", "PARAM_GITOPS_HOST": "gitlab.com"},
            {"PARAM_GITOPS_FORGE": "github", "PARAM_GITLAB_TOKEN_FILE": "/etc/hostname"},
            self._gitlab(PARAM_GITOPS_HOST="https://gitlab.com"),
            self._gitlab(PARAM_GITOPS_HOST="gitlab.com:8443"),
            self._gitlab(PARAM_GITOPS_HOST="GitLab.com"),
            self._gitlab(PARAM_GITOPS_HOST="github.com"),
            self._gitlab(PARAM_GITOPS_HOST="api.github.com"),
            self._gitlab(PARAM_GITOPS_REPO="project"),
            self._gitlab(PARAM_GITOPS_REPO="a/../b"),
            self._gitlab(PARAM_GITOPS_REPO="a/b c"),
            self._gitlab(PARAM_GITOPS_REPO="a/*"),
            self._gitlab(PARAM_GITOPS_REPO="git@gitlab.com:a/b.git"),
            self._gitlab(PARAM_GITOPS_REPO="https://evil.example/a/b"),
            self._gitlab(PARAM_GITHUB_APP_ID="123"),
            self._gitlab(PARAM_GITLAB_TOKEN_SECRET="Bad_Name"),
            self._gitlab(PARAM_GITLAB_TOKEN_FILE=str(self._tmp / "missing")),
            self._gitlab(PARAM_GITOPS_REPO="", PARAM_NON_INTERACTIVE="true"),
        ):
            with self.subTest(case=case):
                proc = self._validate(case)
                self.assertRegex(proc.stdout, r"rc=[1-9]", proc.stderr + proc.stdout)

    def test_install_env_records_the_secret_name_only(self):
        tok = self._tmp / "tok"
        tok.write_text(_SENTINEL)
        dest = self._tmp / "out.env"
        body = (
            f'source "{_INSTALLER_COMMON}"\n'
            f'KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n'
            'PARAM_DRY_RUN="false"\n'
            "export GITOPS_FORGE=gitlab GITOPS_HOST=gitlab.corp.example GITOPS_REPO=g/p GITLAB_TOKEN_SECRET=gl-tok\n"
            f"PARAM_GITLAB_TOKEN_FILE={shlex.quote(str(tok))}\n"
            f'bootstrap_install_env_file "{dest}" 0.5.0\n'
        )
        proc = subprocess.run(
            ["bash", "-c", body],
            capture_output=True,
            text=True,
            env=get_isolated_test_env(overrides={"KUBE_AGENTS_INSTALL_ENV": str(self._empty_install_env)}),
            cwd=str(_REPO_ROOT),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        text = dest.read_text()
        self.assertIn("GITOPS_FORGE=gitlab", text)
        self.assertIn("GITOPS_HOST=gitlab.corp.example", text)
        self.assertIn("GITLAB_TOKEN_SECRET=gl-tok", text)
        self.assertNotIn(_SENTINEL, text)
        self.assertNotIn(str(tok), text)
        self.assertNotIn("TOKEN_FILE", text)

    def test_install_env_of_a_github_install_has_no_forge_keys(self):
        dest = self._tmp / "out.env"
        body = (
            f'source "{_INSTALLER_COMMON}"\n'
            f'KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n'
            'PARAM_DRY_RUN="false"\n'
            "export GITOPS_ORG=acme GITOPS_REPO=infra\n"
            f'bootstrap_install_env_file "{dest}" 0.5.0\n'
        )
        proc = subprocess.run(
            ["bash", "-c", body],
            capture_output=True,
            text=True,
            env=get_isolated_test_env(overrides={"KUBE_AGENTS_INSTALL_ENV": str(self._empty_install_env)}),
            cwd=str(_REPO_ROOT),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        for key in ("GITOPS_FORGE", "GITOPS_HOST", "GITLAB_TOKEN_SECRET"):
            self.assertNotIn(key, dest.read_text())


class GitLabTokenNeverLeaksTest(unittest.TestCase):
    """The sentinel token reaches the Secret and nowhere else."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self._tmp = pathlib.Path(tmp.name)
        self._stub_dir = self._tmp / "stub"
        self._stub_dir.mkdir()
        self._bin = _write_stub(self._stub_dir)
        self._work = self._tmp / "work"
        self._work.mkdir()
        (self._work / "install.env").write_text("")

    def _env(self, **extra):
        overrides = {
            "KUBE_AGENTS_INSTALL_ENV": str(self._work / "install.env"),
            "STUB_DIR": str(self._stub_dir),
            "GITOPS_FORGE": "gitlab",
            "GITLAB_TOKEN_SECRET": "gitlab-forge-token",
        }
        overrides.update(extra)
        return get_isolated_test_env(overrides=overrides, bin_dir=self._bin)

    def _assert_only_in_the_secret(self, *outputs):
        encoded = base64.b64encode(_SENTINEL.encode()).decode()
        applied = list(self._stub_dir.glob("applied.*.yaml"))
        self.assertEqual(len(applied), 1, (self._stub_dir / "kubectl.log").read_text())
        self.assertIn(f"token: {encoded}", applied[0].read_text())
        for f in self._stub_dir.glob("call.*"):
            text = f.read_text()
            self.assertNotIn(_SENTINEL, text, f.name)
            self.assertNotIn(encoded, text, f.name)
        for out in outputs:
            self.assertNotIn(_SENTINEL, out)
            self.assertNotIn(encoded, out)
        for f in self._work.rglob("*"):
            if f.is_file():
                self.assertNotIn(_SENTINEL, f.read_text(errors="replace"), str(f))

    def test_token_file(self):
        tok = self._tmp / "token-file"
        tok.write_text(_SENTINEL)
        body = (
            f'KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n'
            f"PARAM_NON_INTERACTIVE=true PARAM_GITLAB_TOKEN_FILE={shlex.quote(str(tok))}\n"
            'create_gitlab_token_secret agents ctx1; echo "rc=$?"\n'
        )
        proc = subprocess.run(
            ["bash", "-c", body], capture_output=True, text=True, env=self._env(),
            cwd=str(self._work), stdin=subprocess.DEVNULL, start_new_session=True, timeout=60,
        )
        self.assertIn("rc=0", proc.stdout, proc.stderr)
        self.assertIn("--from-file=token=" + str(tok), (self._stub_dir / "kubectl.log").read_text())
        self._assert_only_in_the_secret(proc.stdout, proc.stderr)

    def test_non_interactive_without_a_file_writes_nothing_and_says_how(self):
        body = (
            f'KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n'
            "PARAM_NON_INTERACTIVE=true\n"
            'create_gitlab_token_secret agents ctx1; echo "rc=$?"\n'
        )
        proc = subprocess.run(
            ["bash", "-c", body], capture_output=True, text=True, env=self._env(),
            cwd=str(self._work), stdin=subprocess.DEVNULL, start_new_session=True, timeout=60,
        )
        self.assertIn("rc=0", proc.stdout, proc.stderr)
        self.assertIn("kubectl create secret generic gitlab-forge-token -n agents", proc.stdout)
        self.assertFalse(list(self._stub_dir.glob("applied.*")))

    def test_prompt_is_not_echoed_and_reaches_only_the_secret(self):
        body = (
            f'KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n'
            "PARAM_NON_INTERACTIVE=false\n"
            'create_gitlab_token_secret agents ctx1; echo "rc=$?"\n'
            # Anything the function exported, or left set, would show here.
            'env > "$STUB_DIR/after.env"; set > "$STUB_DIR/after.set"\n'
        )
        env = self._env()
        pid, fd = pty.fork()
        if pid == 0:  # pragma: no cover - child
            try:
                os.chdir(str(self._work))
                os.execvpe("bash", ["bash", "-c", body], env)
            finally:
                os._exit(127)
        out = b""
        sent = False
        deadline = time.time() + 60
        while time.time() < deadline:
            r, _, _ = select.select([fd], [], [], 0.5)
            if not r:
                continue
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
            if not sent and b"Paste the GitLab access token" in out:
                time.sleep(0.2)
                os.write(fd, (_SENTINEL + "\n").encode())
                sent = True
        os.waitpid(pid, 0)
        text = out.decode(errors="replace")
        self.assertTrue(sent, text)
        self.assertIn("rc=0", text)
        self.assertIn("--from-file=token=/dev/stdin", (self._stub_dir / "kubectl.log").read_text())
        after = (self._stub_dir / "after.env").read_text() + (self._stub_dir / "after.set").read_text()
        self.assertNotIn(_SENTINEL, after)
        (self._stub_dir / "after.env").unlink()
        (self._stub_dir / "after.set").unlink()
        self._assert_only_in_the_secret(text)


class GitLabTerraformCompositionTest(unittest.TestCase):
    """Source-level: the composition names the Secret and never the token."""

    def setUp(self):
        self.main = (_FULL_INSTALL / "main.tf").read_text()
        self.variables = (_FULL_INSTALL / "variables.tf").read_text()

    def test_variables_exist_with_github_defaults(self):
        for name, default in (
            ("gitops_forge", '"github"'),
            ("gitops_host", '""'),
            ("gitlab_repo", '""'),
            ("gitlab_credentials_secret", '"gitlab-forge-token"'),
        ):
            m = re.search(r'variable "%s" \{(.*?)\n\}' % name, self.variables, re.S)
            self.assertIsNotNone(m, name)
            self.assertRegex(m.group(1), r"default\s*=\s*%s" % re.escape(default), name)
            self.assertNotIn("sensitive", m.group(1))

    def test_no_token_variable(self):
        self.assertNotRegex(self.variables, r'variable "gitlab_token')

    def test_gitlab_suppresses_the_github_alias(self):
        self.assertIn("!local.gitops_is_gitlab && (local.github_org", self.main)
        self.assertIn("credentialsRef = { name = var.gitlab_credentials_secret }", self.main)
        self.assertIn('role = "gitops"', self.main)

    def test_precondition_refuses_a_minter_on_gitlab(self):
        self.assertIn("!var.enable_github_minter", self.main)


if __name__ == "__main__":
    unittest.main()
