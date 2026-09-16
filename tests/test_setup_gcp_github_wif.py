"""Tests for scripts/dev/setup-gcp-github-wif.sh.

Asserts that required GCP APIs and IAM roles (for both standard CI and extended
--admin E2E pipelines) remain consistent and complete.
"""

import pathlib
import re
import subprocess
import unittest

from tests.testing.common import get_isolated_test_env

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_WIF_SCRIPT = _REPO_ROOT / "scripts" / "dev" / "setup-gcp-github-wif.sh"


_EXPECTED_APIS = [
    "iamcredentials.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "container.googleapis.com",
    "storage.googleapis.com",
    "pubsub.googleapis.com",
    "gkebackup.googleapis.com",
    "logging.googleapis.com",
    "artifactregistry.googleapis.com",
]

_STANDARD_ROLES = [
    "roles/cloudkms.admin",
    "roles/container.admin",
    "roles/compute.viewer",
    "roles/serviceusage.serviceUsageAdmin",
    "roles/serviceusage.serviceUsageConsumer",
]

_ADMIN_ROLES = [
    "roles/iam.roleAdmin",
    "roles/iam.serviceAccountAdmin",
    "roles/resourcemanager.projectIamAdmin",
    "roles/pubsub.admin",
    "roles/gkebackup.admin",
    "roles/storage.admin",
    "roles/logging.configWriter",
    "roles/artifactregistry.admin",
]


class SetupGcpGithubWifTest(unittest.TestCase):
    def setUp(self):
        self.assertTrue(_WIF_SCRIPT.is_file(), f"WIF script not found at {_WIF_SCRIPT}")
        self.script_content = _WIF_SCRIPT.read_text()

    def _get_enabled_services_invocation(self) -> str:
        match = re.search(r"gcloud services enable\s+([\s\S]*?)--project=", self.script_content)
        self.assertIsNotNone(match, "Could not find 'gcloud services enable ... --project=' invocation")
        return match.group(1)

    def _split_script_by_admin_boundary(self) -> tuple[str, str]:
        boundary = 'echo "Admin mode selected.'
        self.assertIn(boundary, self.script_content, "Admin branch boundary not found in script")
        base_part, admin_part = self.script_content.split(boundary, 1)
        return base_part, admin_part

    def test_missing_required_env_vars_fails(self):
        """Executing the script without required env vars should fail with an informative message."""
        env = get_isolated_test_env(
            overrides={
                "PROJECT_ID": "",
                "SA_NAME": "",
                "GITHUB_REPO": "",
            }
        )
        proc = subprocess.run(
            ["bash", str(_WIF_SCRIPT)],
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("Please set the required variables", proc.stdout)

    def test_required_services_enabled_in_invocation(self):
        """Ensures all required APIs are explicitly present in the gcloud services enable call."""
        invocation = self._get_enabled_services_invocation()
        active_tokens = [
            line.strip().rstrip("\\").strip()
            for line in invocation.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        for api in _EXPECTED_APIS:
            self.assertIn(
                api,
                active_tokens,
                f"Expected API '{api}' missing from gcloud services enable invocation in {_WIF_SCRIPT}",
            )
        self.assertEqual(len(active_tokens), len(_EXPECTED_APIS))

    def test_standard_roles_defined_in_base_tier_only(self):
        """Verifies base standard CI roles are in base ROLES array and admin roles are excluded."""
        base_part, _ = self._split_script_by_admin_boundary()
        base_roles_match = re.search(r"^\s*ROLES=\(\s*\n([\s\S]*?)^\s*\)", base_part, re.MULTILINE)
        self.assertIsNotNone(base_roles_match, "Base ROLES array definition not found")
        base_block = base_roles_match.group(1)

        for role in _STANDARD_ROLES:
            self.assertIn(
                f'"{role}"',
                base_block,
                f"Standard role '{role}' missing in base ROLES array of {_WIF_SCRIPT}",
            )
        for role in _ADMIN_ROLES:
            self.assertNotIn(
                f'"{role}"',
                base_block,
                f"Admin role '{role}' unexpectedly found in base ROLES array of {_WIF_SCRIPT}",
            )

    def test_admin_extended_roles_defined_in_admin_tier_only(self):
        """Verifies extended admin roles are in admin ROLES+= array and base roles are excluded."""
        _, admin_part = self._split_script_by_admin_boundary()
        admin_roles_match = re.search(r"^\s*ROLES\+=\(\s*\n([\s\S]*?)^\s*\)", admin_part, re.MULTILINE)
        self.assertIsNotNone(admin_roles_match, "Admin ROLES+= array definition not found")
        admin_block = admin_roles_match.group(1)

        for role in _ADMIN_ROLES:
            self.assertIn(
                f'"{role}"',
                admin_block,
                f"Admin lifecycle role '{role}' missing in ROLES+= array of {_WIF_SCRIPT}",
            )
        for role in _STANDARD_ROLES:
            self.assertNotIn(
                f'"{role}"',
                admin_block,
                f"Base role '{role}' unexpectedly found in admin ROLES+= array of {_WIF_SCRIPT}",
            )


class CustomRoleNeedsRoleAdminTest(unittest.TestCase):
    """A Terraform module that *defines* a custom role needs `iam.roles.create`.

    `roles/resourcemanager.projectIamAdmin` -- what every installer principal
    used to get, on the reasoning that the IAM module "binds and unbinds
    project IAM policies" -- carries no `iam.roles.*` permission whatsoever.
    That was true for as long as the module only bound predefined roles. The
    moment one `google_project_iam_custom_role` appeared, apply and destroy
    both started failing PERMISSION_DENIED for every principal short of Owner,
    and nothing in the tree said so: the grant lists and the module that
    outgrew them are four directories apart.

    So this asserts the join rather than the two ends of it. Adding a custom
    role to any module fails here until the grant follows, which is the
    direction the mistake actually travels -- nobody removes `roleAdmin`; they
    add a resource that needs it.
    """

    #: Grant sites, and the literal each one writes a role as. Both are
    #: installer principals that run `terraform apply` on the composition.
    _GRANT_SITES = (
        (_REPO_ROOT / "scripts" / "dev" / "setup-gcp-github-wif.sh", '"roles/iam.roleAdmin"'),
        (_REPO_ROOT / "scripts" / "provision_ci_pool_project.sh", "roles/iam.roleAdmin"),
        (_REPO_ROOT / "scripts" / "verify_ci_pool_project.py", '"roles/iam.roleAdmin"'),
    )

    #: Any scope, not just the project-scoped resource that exists today.
    #: `roles/iam.roleAdmin` is what carries `iam.roles.create`, and an
    #: org-scoped `google_organization_iam_custom_role` needs it just as much
    #: -- more, since it has to be held at the organization. Naming the one
    #: literal meant a module that moved its role up a scope silently stopped
    #: being covered here.
    _CUSTOM_ROLE_RESOURCE = re.compile(r"google_\w*_iam_custom_role")

    def _modules_defining_a_custom_role(self) -> list[pathlib.Path]:
        return sorted(
            path
            for path in (_REPO_ROOT / "terraform").rglob("*.tf")
            if self._CUSTOM_ROLE_RESOURCE.search(path.read_text())
        )

    def test_a_module_defines_a_custom_role(self):
        """Guards the glob, not the assertion below.

        The test below asserts the grant unconditionally -- `defining` only
        feeds its failure message -- so it does not pass vacuously when this
        returns nothing. What it does instead is fail with a sentence that has
        a hole where the module name goes. And an empty result here has one
        likely cause, since the tree has carried a custom role since the
        subnet-utilization one landed: this glob or that regex stopped matching
        what Terraform now writes. Say so where a maintainer will read it,
        rather than letting the join go unwatched with the suite green.
        """
        self.assertTrue(
            self._modules_defining_a_custom_role(),
            "No terraform/**/*.tf declares a custom-role resource at any scope. The tree has "
            "carried one since the subnet-utilization role landed, so this is most likely a "
            "stale glob or regex rather than a real removal -- fix it, or delete both tests "
            "if the last custom role really is gone.",
        )

    def test_every_installer_principal_can_create_it(self):
        defining = self._modules_defining_a_custom_role()
        for path, literal in self._GRANT_SITES:
            with self.subTest(grant_site=path.relative_to(_REPO_ROOT).as_posix()):
                self.assertIn(
                    literal,
                    path.read_text(),
                    f"{path.relative_to(_REPO_ROOT)} grants no roles/iam.roleAdmin, but "
                    f"{', '.join(p.relative_to(_REPO_ROOT).as_posix() for p in defining)} "
                    f"declares an IAM custom role. projectIamAdmin cannot "
                    f"create one -- it holds no iam.roles.* permission -- so terraform "
                    f"apply fails PERMISSION_DENIED for this principal.",
                )


if __name__ == "__main__":
    unittest.main()
