"""Verify Envoy file-based SDS rendering for credential-proxy TLS in start-services.sh."""

from __future__ import annotations

import pathlib
import subprocess
import sys
import tempfile
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_SCRIPT = _REPO_ROOT / "deploy" / "shared" / "start-services.sh"
_BASE_ENVOY_CONFIG = _REPO_ROOT / "deploy" / "shared" / "envoy-credential-proxy.yaml"
_FUNC_START = "render_envoy_tls_config() {\n"
_FUNC_END = "\n}\n\nstart_envoy() {"


def _extract_render_fn() -> str:
    text = _SCRIPT.read_text(encoding="utf-8")
    begin = text.index(_FUNC_START)
    end = text.index(_FUNC_END, begin) + len("\n}\n")
    return text[begin:end]


class StartServicesTLSSDSTest(unittest.TestCase):
    def test_renders_file_based_sds_with_watched_directory(self) -> None:
        fn_body = _extract_render_fn()
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = pathlib.Path(tmpdir)
            dst_path = tmp / "envoy-tls.yaml"
            sds_path = tmp / "envoy-sds.yaml"
            cert_file = "/run/servicedns-cert/credential-bundle.pem"
            program = "\n".join(
                [
                    "set -euo pipefail",
                    f'PROXY_PYTHON="{sys.executable}"',
                    fn_body,
                    f'render_envoy_tls_config "{_BASE_ENVOY_CONFIG}" "{dst_path}" "{sds_path}" "8767" "{cert_file}" "{cert_file}"',
                ]
            )
            result = subprocess.run(
                ["bash", "-c", program],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(0, result.returncode, result.stderr)

            envoy_doc = yaml.safe_load(dst_path.read_text(encoding="utf-8"))
            self.assertEqual(
                {"id": "credential-proxy", "cluster": "credential-proxy"},
                envoy_doc.get("node"),
            )
            listeners = envoy_doc["static_resources"]["listeners"]
            tls_listeners = [l for l in listeners if l["name"] == "credential_proxy_tls"]
            self.assertEqual(1, len(tls_listeners))
            tls_listener = tls_listeners[0]
            self.assertEqual(8767, tls_listener["address"]["socket_address"]["port_value"])

            ts = tls_listener["filter_chains"][0]["transport_socket"]
            self.assertEqual("envoy.transport_sockets.tls", ts["name"])
            common_tls = ts["typed_config"]["common_tls_context"]
            self.assertNotIn("tls_certificates", common_tls)
            self.assertEqual("TLSv1_2", common_tls["tls_params"]["tls_minimum_protocol_version"])

            sds_configs = common_tls["tls_certificate_sds_secret_configs"]
            self.assertEqual(1, len(sds_configs))
            self.assertEqual("credential_proxy_tls_cert", sds_configs[0]["name"])
            sds_cfg = sds_configs[0]["sds_config"]
            self.assertEqual("V3", sds_cfg["resource_api_version"])
            self.assertEqual(
                {
                    "path": str(sds_path),
                    "watched_directory": {"path": "/run/servicedns-cert"},
                },
                sds_cfg["path_config_source"],
            )

            sds_doc = yaml.safe_load(sds_path.read_text(encoding="utf-8"))
            self.assertEqual(
                {
                    "resources": [
                        {
                            "@type": "type.googleapis.com/envoy.extensions.transport_sockets.tls.v3.Secret",
                            "name": "credential_proxy_tls_cert",
                            "tls_certificate": {
                                "certificate_chain": {"filename": cert_file},
                                "private_key": {"filename": cert_file},
                            },
                        }
                    ]
                },
                sds_doc,
            )

    def test_rejects_out_of_range_tls_port(self) -> None:
        fn_body = _extract_render_fn()
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = pathlib.Path(tmpdir)
            program = "\n".join(
                [
                    "set -euo pipefail",
                    f'PROXY_PYTHON="{sys.executable}"',
                    fn_body,
                    f'render_envoy_tls_config "{_BASE_ENVOY_CONFIG}" "{tmp}/out.yaml" "{tmp}/sds.yaml" "70000" "/run/servicedns-cert/credential-bundle.pem" "/run/servicedns-cert/credential-bundle.pem"',
                ]
            )
            result = subprocess.run(
                ["bash", "-c", program],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(0, result.returncode)
            self.assertIn("outside 1-65535", result.stderr)


if __name__ == "__main__":
    unittest.main()
