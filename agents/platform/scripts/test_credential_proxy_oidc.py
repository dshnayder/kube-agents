#!/usr/bin/env python3
"""Unit tests for generic OIDC JWT authentication in credential_proxy.py."""

from __future__ import annotations

import base64
import datetime
import ipaddress
import json
import os
import ssl
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from unittest import mock

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa, utils
from cryptography.x509.oid import NameOID

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import credential_proxy  # noqa: E402


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _generate_self_signed_tls_files(directory: Path) -> tuple[Path, Path, Path]:
    key = ec.generate_private_key(ec.SECP256R1())
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    ca_path = directory / "ca.pem"
    cert_path = directory / "cert.pem"
    key_path = directory / "key.pem"
    pem_cert = cert.public_bytes(serialization.Encoding.PEM)
    ca_path.write_bytes(pem_cert)
    cert_path.write_bytes(pem_cert)
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return ca_path, cert_path, key_path


def _ec_jwk(public_key: ec.EllipticCurvePublicKey, kid: str) -> dict[str, str]:
    numbers = public_key.public_numbers()
    return {
        "kty": "EC",
        "use": "sig",
        "alg": "ES256",
        "kid": kid,
        "crv": "P-256",
        "x": _b64url(numbers.x.to_bytes(32, "big")),
        "y": _b64url(numbers.y.to_bytes(32, "big")),
    }


def _rsa_jwk(public_key: rsa.RSAPublicKey, kid: str) -> dict[str, str]:
    numbers = public_key.public_numbers()
    n_len = (numbers.n.bit_length() + 7) // 8
    e_len = (numbers.e.bit_length() + 7) // 8
    return {
        "kty": "RSA",
        "use": "sig",
        "alg": "RS256",
        "kid": kid,
        "n": _b64url(numbers.n.to_bytes(n_len, "big")),
        "e": _b64url(numbers.e.to_bytes(e_len, "big")),
    }


def _sign_es256(private_key: ec.EllipticCurvePrivateKey, kid: str, claims: dict[str, Any]) -> str:
    header = _b64url(json.dumps({"alg": "ES256", "typ": "JWT", "kid": kid}, separators=(",", ":")).encode("utf-8"))
    payload = _b64url(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{header}.{payload}".encode("ascii")
    der_sig = private_key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r_int, s_int = utils.decode_dss_signature(der_sig)
    raw_sig = r_int.to_bytes(32, "big") + s_int.to_bytes(32, "big")
    return f"{header}.{payload}.{_b64url(raw_sig)}"


def _sign_rs256(private_key: rsa.RSAPrivateKey, kid: str, claims: dict[str, Any]) -> str:
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT", "kid": kid}, separators=(",", ":")).encode("utf-8"))
    payload = _b64url(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{header}.{payload}".encode("ascii")
    sig = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{payload}.{_b64url(sig)}"


class _OIDCTestServer:
    def __init__(self, cert_path: Path, key_path: Path) -> None:
        self.keys: list[dict[str, Any]] = []
        self.discovery_hits = 0
        self.jwks_hits = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/.well-known/openid-configuration":
                    outer.discovery_hits += 1
                    body = json.dumps(
                        {
                            "issuer": outer.issuer_url,
                            "jwks_uri": f"{outer.issuer_url}/jwks",
                        }
                    ).encode("utf-8")
                elif self.path == "/jwks":
                    outer.jwks_hits += 1
                    body = json.dumps({"keys": outer.keys}).encode("utf-8")
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt: str, *args: Any) -> None:  # noqa: ARG002
                return

        self._httpd = HTTPServer(("127.0.0.1", 0), Handler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
        self._httpd.socket = ctx.wrap_socket(self._httpd.socket, server_side=True)
        port = self._httpd.server_address[1]
        self.issuer_url = f"https://127.0.0.1:{port}"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5.0)


class OIDCActorAuthenticationTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmpdir.name)
        self.ca_path, cert_path, key_path = _generate_self_signed_tls_files(self.tmpdir)
        self.token_file = self.tmpdir / "ksa-token"
        self.token_file.write_text("own-ksa-token\n", encoding="utf-8")
        self.server = _OIDCTestServer(cert_path, key_path)
        self.ec_key = ec.generate_private_key(ec.SECP256R1())
        self.rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.server.keys = [
            _ec_jwk(self.ec_key.public_key(), "ec-1"),
            _rsa_jwk(self.rsa_key.public_key(), "rsa-1"),
        ]
        self.now = 1_700_000_000.0

    def tearDown(self) -> None:
        self.server.close()
        self._tmpdir.cleanup()

    def _authenticator(
        self,
        *,
        required_sub_prefix: str = "",
        uid_claim: str = "sub",
    ) -> credential_proxy.ServiceAccountAuthenticator:
        return credential_proxy.ServiceAccountAuthenticator(
            audience_roles={
                credential_proxy.DEFAULT_CREDENTIAL_PROXY_AUDIENCE: credential_proxy.CALLER_ROLE_SHELL,
                credential_proxy.DEFAULT_CREDENTIAL_PROXY_CHAT_AUDIENCE: credential_proxy.CALLER_ROLE_CHAT,
            },
            allowed_callers=frozenset({"system:serviceaccount:kubeagents-system:platform-agent-a2a-gateway"}),
            api_host="127.0.0.1",
            api_port="6443",
            ca_file=str(self.ca_path),
            token_file=str(self.token_file),
            oidc_issuer_url=self.server.issuer_url,
            oidc_ca_file=str(self.ca_path),
            oidc_required_sub_prefix=required_sub_prefix,
            oidc_uid_claim=uid_claim,
            oidc_clock=lambda: self.now,
        )

    def test_verifies_generic_defaults_with_any_non_empty_subject(self) -> None:
        auth = self._authenticator()
        token = _sign_es256(
            self.ec_key,
            "ec-1",
            {
                "iss": self.server.issuer_url,
                "sub": "workload/alpha",
                "aud": ["kubeagents-credential-proxy"],
                "exp": int(self.now) + 300,
                "nbf": int(self.now) - 10,
            },
        )
        principal = auth.authenticate({"Authorization": f"Bearer {token}"})
        self.assertEqual(principal.workload, "workload/alpha")
        self.assertEqual(principal.uid, "workload/alpha")
        self.assertEqual(principal.role, credential_proxy.CALLER_ROLE_SHELL)

    def test_verifies_es256_and_rs256_with_escaped_dot_uid_claim_and_prefix(self) -> None:
        auth = self._authenticator(
            required_sub_prefix="actor/",
            uid_claim=r"actor\.ate\.dev.actorUID",
        )
        es_token = _sign_es256(
            self.ec_key,
            "ec-1",
            {
                "iss": self.server.issuer_url,
                "sub": "actor/default/platform-agent",
                "aud": ["kubeagents-credential-proxy"],
                "exp": int(self.now) + 300,
                "nbf": int(self.now) - 10,
                "actor.ate.dev": {"atespace": "default", "actorUID": "uid-es256-123"},
            },
        )
        principal = auth.authenticate({"Authorization": f"Bearer {es_token}"})
        self.assertEqual(principal.workload, "actor/default/platform-agent")
        self.assertEqual(principal.uid, "uid-es256-123")
        self.assertEqual(principal.role, credential_proxy.CALLER_ROLE_SHELL)
        self.assertEqual(self.server.discovery_hits, 1)
        self.assertEqual(self.server.jwks_hits, 1)

        rs_token = _sign_rs256(
            self.rsa_key,
            "rsa-1",
            {
                "iss": self.server.issuer_url,
                "sub": "actor/default/cluster-agent-001",
                "aud": "kubeagents-credential-proxy",
                "exp": int(self.now) + 300,
            },
        )
        rs_principal = auth.authenticate(f"Bearer {rs_token}")
        self.assertEqual(rs_principal.workload, "actor/default/cluster-agent-001")
        # Falls back to sub when the configured claim path is absent.
        self.assertEqual(rs_principal.uid, "actor/default/cluster-agent-001")
        self.assertEqual(rs_principal.role, credential_proxy.ROLE_SHELL)
        # Cached JWKS serves the second token without another HTTP round trip.
        self.assertEqual(self.server.jwks_hits, 1)

    def test_rejects_expired_not_yet_valid_wrong_audience_prefix_mismatch_and_bad_signature(self) -> None:
        auth = self._authenticator(
            required_sub_prefix="actor/",
            uid_claim=r"actor\.ate\.dev.actorUID",
        )
        wrong_key = ec.generate_private_key(ec.SECP256R1())
        cases = [
            (
                "expired",
                _sign_es256(
                    self.ec_key,
                    "ec-1",
                    {
                        "iss": self.server.issuer_url,
                        "sub": "actor/default/platform-agent",
                        "aud": "kubeagents-credential-proxy",
                        "exp": int(self.now) - 120,
                    },
                ),
            ),
            (
                "nbf_in_future",
                _sign_es256(
                    self.ec_key,
                    "ec-1",
                    {
                        "iss": self.server.issuer_url,
                        "sub": "actor/default/platform-agent",
                        "aud": "kubeagents-credential-proxy",
                        "exp": int(self.now) + 300,
                        "nbf": int(self.now) + 120,
                    },
                ),
            ),
            (
                "wrong_audience",
                _sign_es256(
                    self.ec_key,
                    "ec-1",
                    {
                        "iss": self.server.issuer_url,
                        "sub": "actor/default/platform-agent",
                        "aud": "kubeagents-credential-proxy-chat",
                        "exp": int(self.now) + 300,
                    },
                ),
            ),
            (
                "non_actor_subject",
                _sign_es256(
                    self.ec_key,
                    "ec-1",
                    {
                        "iss": self.server.issuer_url,
                        "sub": "user/alice",
                        "aud": "kubeagents-credential-proxy",
                        "exp": int(self.now) + 300,
                    },
                ),
            ),
            (
                "bare_prefix_subject",
                _sign_es256(
                    self.ec_key,
                    "ec-1",
                    {
                        "iss": self.server.issuer_url,
                        "sub": "actor/",
                        "aud": "kubeagents-credential-proxy",
                        "exp": int(self.now) + 300,
                    },
                ),
            ),
            (
                "bad_signature",
                _sign_es256(
                    wrong_key,
                    "ec-1",
                    {
                        "iss": self.server.issuer_url,
                        "sub": "actor/default/platform-agent",
                        "aud": "kubeagents-credential-proxy",
                        "exp": int(self.now) + 300,
                    },
                ),
            ),
        ]
        for name, token in cases:
            with self.subTest(name=name):
                with mock.patch.object(auth, "_review", side_effect=AssertionError("TokenReview should not be called")):
                    with self.assertRaises(credential_proxy.AuthenticationError):
                        auth.authenticate({"Authorization": f"Bearer {token}"})

    def test_rejects_malformed_uid_claim_paths(self) -> None:
        for bad_claim in (".leading", "trailing.", "a..b", "trailing\\"):
            with self.subTest(bad_claim=bad_claim):
                with self.assertRaises(ValueError):
                    self._authenticator(uid_claim=bad_claim)

    def test_falls_through_to_token_review_for_non_oidc_tokens(self) -> None:
        auth = self._authenticator()
        expected = credential_proxy.Principal(
            workload="system:serviceaccount:kubeagents-system:platform-agent-a2a-gateway",
            uid="ksa-uid",
            role=credential_proxy.CALLER_ROLE_CHAT,
        )
        other_jwt = _sign_es256(
            self.ec_key,
            "ec-1",
            {
                "iss": "https://kubernetes.default.svc.cluster.local",
                "sub": "system:serviceaccount:kubeagents-system:platform-agent-a2a-gateway",
                "aud": ["kubeagents-credential-proxy-chat"],
                "exp": int(self.now) + 300,
            },
        )
        with mock.patch.object(auth, "_review", return_value=expected) as review_mock:
            got = auth.authenticate({"Authorization": f"Bearer {other_jwt}"})
        self.assertEqual(got, expected)
        review_mock.assert_called_once_with(other_jwt)

    def test_build_authenticator_wires_oidc_env(self) -> None:
        env = {
            "CREDENTIAL_PROXY_AUTH_MODE": "serviceaccount",
            "CREDENTIAL_PROXY_ALLOWED_CALLERS": "system:serviceaccount:ns:sa",
            "CREDENTIAL_PROXY_CHAT_AUDIENCE": "kubeagents-credential-proxy-chat",
            "KUBERNETES_SERVICE_HOST": "127.0.0.1",
            "CREDENTIAL_PROXY_OIDC_ISSUER_URL": self.server.issuer_url,
            "CREDENTIAL_PROXY_OIDC_CA_FILE": str(self.ca_path),
            "CREDENTIAL_PROXY_OIDC_REQUIRED_SUB_PREFIX": "actor/",
            "CREDENTIAL_PROXY_OIDC_UID_CLAIM": r"actor\.ate\.dev.actorUID",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            built = credential_proxy.build_authenticator()
        self.assertIsInstance(built, credential_proxy.ServiceAccountAuthenticator)
        assert isinstance(built, credential_proxy.ServiceAccountAuthenticator)
        self.assertEqual(built.oidc_issuer_url, self.server.issuer_url)
        self.assertEqual(built.oidc_ca_file, str(self.ca_path))
        self.assertEqual(built.oidc_required_sub_prefix, "actor/")
        self.assertEqual(built.oidc_uid_claim, r"actor\.ate\.dev.actorUID")

    def test_oidc_principal_skips_pod_binding_while_sa_enforces_it(self) -> None:
        auth = self._authenticator()
        auth.pod_binding = credential_proxy.PodAddressBinding(
            api_host="127.0.0.1",
            api_port="6443",
            ca_file="",
            token_file="",
        )
        oidc_token = _sign_es256(
            self.ec_key,
            "ec-1",
            {
                "iss": self.server.issuer_url,
                "sub": "actor/default/platform-agent-sandbox",
                "aud": "kubeagents-credential-proxy",
                "exp": int(self.now) + 300,
                "actor.ate.dev": {"actorUID": "actor-uid-42"},
            },
        )
        oidc_principal = auth.authenticate({"Authorization": f"Bearer {oidc_token}"})
        self.assertEqual(oidc_principal.auth_source, "oidc")

        def _run_authenticated(principal: credential_proxy.Principal) -> tuple[credential_proxy.Principal | None, list[tuple[Any, Any]]]:
            responses: list[tuple[Any, Any]] = []
            handler = object.__new__(credential_proxy.CredentialProxyHandler)
            handler.authenticator = mock.Mock(pod_binding=auth.pod_binding)
            handler.authenticator.authenticate.return_value = principal
            handler.headers = {"Authorization": "Bearer dummy"}
            handler.path = "/v1/exec"
            handler.client_address = ("10.0.0.99", 54321)
            handler.server = mock.Mock()
            handler._json = lambda status, body: responses.append((status, body))  # type: ignore[method-assign]
            handler._role_permits = lambda p: True  # type: ignore[method-assign]
            return handler._authenticated(), responses

        got_oidc, oidc_responses = _run_authenticated(oidc_principal)
        self.assertEqual(got_oidc, oidc_principal)
        self.assertEqual(oidc_responses, [])

        unbound_sa = credential_proxy.Principal(
            workload="system:serviceaccount:kubeagents-system:platform-agent-shell",
            uid="ksa-uid",
            role=credential_proxy.CALLER_ROLE_SHELL,
        )
        got_sa, sa_responses = _run_authenticated(unbound_sa)
        self.assertIsNone(got_sa)
        self.assertEqual(len(sa_responses), 1)
        self.assertEqual(sa_responses[0][0], credential_proxy.HTTPStatus.UNAUTHORIZED)

    def test_credential_proxy_source_has_no_hardcoded_substrate_strings(self) -> None:
        src = (SCRIPT_DIR / "credential_proxy.py").read_text(encoding="utf-8")
        self.assertNotIn("ate.dev", src)
        self.assertNotIn('"actor/"', src)
        self.assertNotIn("'actor/'", src)


if __name__ == "__main__":
    unittest.main()
