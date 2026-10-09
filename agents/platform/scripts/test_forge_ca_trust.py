#!/usr/bin/env python3
"""A self-managed forge behind a private CA (#2750).

The broker trusts a CA that a forge names in its configuration (`caFile`) for
that forge's host only, in both of its clients: the in-process API transport
and git. These tests make a throwaway CA, server key and certificate with the
`openssl` binary at test time, so no key is kept in the repository, and talk
to a real TLS listener on loopback. They are skipped where `openssl` is absent.
"""

from __future__ import annotations

import http.server
import os
import shutil
import ssl
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import credential_proxy
import providers
from providers import transport as transport_module
from providers.registry import load_forge_entries
from providers.transport import HttpTransport, ca_context
from workspace_paths import WorkspaceError

OPENSSL = shutil.which("openssl")
STRICT = getattr(ssl, "VERIFY_X509_STRICT", 0)


def _openssl(*args: str, cwd: Path) -> None:
    subprocess.run([OPENSSL, *args], cwd=cwd, check=True, capture_output=True)


def make_pki(root: Path, *, key_usage: bool) -> tuple[Path, Path, Path]:
    """A CA, and a server certificate for localhost and 127.0.0.1 it signed.

    `key_usage=False` makes a CA certificate with no Key Usage extension, which
    Python's strict X.509 check refuses; many private CAs are issued that way.
    """
    root.mkdir(parents=True, exist_ok=True)
    ca_args = [
        "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
        "-keyout", "ca.key", "-out", "ca.crt", "-subj", "/CN=test private CA",
        # Spelled out, because the openssl builds differ in what they add by
        # default: with these, the only defect `key_usage=False` leaves is the
        # missing Key Usage.
        "-addext", "basicConstraints=critical,CA:TRUE",
        "-addext", "subjectKeyIdentifier=hash",
        "-addext", "authorityKeyIdentifier=keyid:always",
    ]
    if key_usage:
        ca_args += ["-addext", "keyUsage=critical,keyCertSign,cRLSign"]
    _openssl(*ca_args, cwd=root)
    (root / "leaf.ext").write_text(
        "subjectAltName=DNS:localhost,IP:127.0.0.1\n"
        "basicConstraints=CA:FALSE\n"
        "keyUsage=critical,digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\n"
        "authorityKeyIdentifier=keyid\n"
        "subjectKeyIdentifier=hash\n"
    )
    _openssl(
        "req", "-new", "-newkey", "rsa:2048", "-nodes", "-keyout", "leaf.key",
        "-out", "leaf.csr", "-subj", "/CN=localhost", cwd=root,
    )
    _openssl(
        "x509", "-req", "-in", "leaf.csr", "-CA", "ca.crt", "-CAkey", "ca.key",
        "-CAcreateserial", "-days", "2", "-out", "leaf.crt", "-extfile", "leaf.ext",
        cwd=root,
    )
    return root / "ca.crt", root / "leaf.crt", root / "leaf.key"


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - the stdlib's name
        body = b'{"username": "bot"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def serve_tls(cert: Path, key: Path) -> tuple[http.server.HTTPServer, int]:
    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(cert), str(key))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def api(port: int, ca_file: str = "") -> HttpTransport:
    return HttpTransport(
        f"https://localhost:{port}/api/v4",
        lambda: {"PRIVATE-TOKEN": "t"},
        timeout=5.0,
        max_bytes=1 << 16,
        ca_file=ca_file,
    )


@unittest.skipUnless(OPENSSL, "openssl is needed to make a throwaway CA")
class HttpTransportTrustTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        transport_module._CA_CONTEXTS.clear()

    def serve(self, *, key_usage: bool) -> tuple[Path, int]:
        ca, cert, key = make_pki(Path(self.tmp.name) / ("ku" if key_usage else "noku"), key_usage=key_usage)
        server, port = serve_tls(cert, key)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return ca, port

    def test_a_forge_that_names_its_ca_is_reached(self):
        ca, port = self.serve(key_usage=True)
        self.assertEqual({"username": "bot"}, api(port, str(ca)).api("GET", "user"))

    @unittest.skipUnless(STRICT, "this Python has no strict X.509 mode")
    def test_a_ca_with_key_usage_passes_the_strict_default_too(self):
        # So the CA above is a fair one: the strict default accepts it, and
        # the test of the CA without Key Usage tests that alone.
        ca, port = self.serve(key_usage=True)
        strict = ssl.create_default_context(cafile=str(ca))
        with urllib.request.urlopen(f"https://localhost:{port}/api/v4/user", context=strict, timeout=5) as answer:
            self.assertEqual(200, answer.status)

    def test_without_the_ca_the_call_is_untrusted_not_retryable(self):
        ca, port = self.serve(key_usage=True)
        with self.assertRaises(WorkspaceError) as caught:
            api(port).api("GET", "user")
        self.assertEqual("FORGE_TLS_UNTRUSTED", caught.exception.fields["code"])
        self.assertEqual(502, caught.exception.status)
        self.assertIn(f"localhost:{port}", caught.exception.fields["detail"])
        self.assertNotIn("retry is reasonable", str(caught.exception))

    def test_a_ca_with_no_key_usage_is_trusted_for_its_forge(self):
        # Python 3.13's strict check refuses this CA, and many enterprise CAs
        # are issued this way, so the forge
        # that names its CA gets the check off -- and only that forge.
        ca, port = self.serve(key_usage=False)
        self.assertEqual({"username": "bot"}, api(port, str(ca)).api("GET", "user"))

    @unittest.skipUnless(STRICT, "this Python has no strict X.509 mode")
    def test_the_same_ca_fails_under_the_strict_default(self):
        # The control for the test above: a default context that trusts the
        # same CA file refuses it while the strict flag is on, so the success
        # above is the relaxation and nothing else.
        ca, port = self.serve(key_usage=False)
        strict = ssl.create_default_context(cafile=str(ca))
        if not strict.verify_flags & STRICT:
            self.skipTest("this Python does not turn the strict check on by default")
        with self.assertRaises(urllib.error.URLError) as caught:
            urllib.request.urlopen(f"https://localhost:{port}/api/v4/user", context=strict, timeout=5)
        self.assertIsInstance(caught.exception.reason, ssl.SSLCertVerificationError)

    def test_the_relaxation_is_on_the_forges_context_only(self):
        ca, _port = self.serve(key_usage=True)
        forge_context = ca_context(str(ca), "gitlab.internal")
        public = ssl.create_default_context()
        if STRICT and public.verify_flags & STRICT:
            self.assertFalse(forge_context.verify_flags & STRICT)
            self.assertTrue(ssl.create_default_context().verify_flags & STRICT)
        # Chain and hostname checks stay on.
        self.assertEqual(ssl.CERT_REQUIRED, forge_context.verify_mode)
        self.assertTrue(forge_context.check_hostname)

    def test_a_ca_file_that_is_not_mounted_names_the_host(self):
        with self.assertRaises(WorkspaceError) as caught:
            api(1, str(Path(self.tmp.name) / "missing.crt")).api("GET", "user")
        self.assertEqual("FORGE_TLS_UNTRUSTED", caught.exception.fields["code"])
        self.assertIn("localhost:1", caught.exception.fields["detail"])
        self.assertIn("not mounted", caught.exception.fields["detail"])

    def test_a_changed_ca_file_is_read_again(self):
        # kubelet rewrites the projected ConfigMap in place; the next call
        # uses the new bundle, with no restart.
        ca, _port = self.serve(key_usage=True)
        target = Path(self.tmp.name) / "bundle.crt"
        shutil.copy(ca, target)
        first = ca_context(str(target), "gitlab.internal")
        self.assertIs(first, ca_context(str(target), "gitlab.internal"))
        other, _leaf, _key = make_pki(Path(self.tmp.name) / "other", key_usage=True)
        shutil.copy(other, target)
        os.utime(target, ns=(1, 1))
        self.assertIsNot(first, ca_context(str(target), "gitlab.internal"))


class ForgeConfigurationTest(unittest.TestCase):
    def write(self, document: str) -> str:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "forges.json"
        path.write_text(document)
        return str(path)

    def test_ca_file_is_read_for_the_forge_that_names_it(self):
        path = self.write(
            '{"forges": [{"provider": "gitlab", "host": "gitlab.internal", "tokenPath": "/t",'
            ' "allowedPaths": ["g"], "caFile": "/etc/kube-agents/forge-ca/gitlab/ca.crt"},'
            ' {"provider": "gitlab", "host": "gitlab.com", "tokenPath": "/u", "allowedPaths": ["h"]}]}'
        )
        entries = load_forge_entries(path)
        self.assertEqual("/etc/kube-agents/forge-ca/gitlab/ca.crt", entries[0]["ca_file"])
        self.assertEqual("", entries[1]["ca_file"])
        forges = [f for f in providers.build_forges({"forges": entries}) if f.name == "gitlab"]
        self.assertEqual("/etc/kube-agents/forge-ca/gitlab/ca.crt", forges[0].ca_file)
        self.assertEqual("", forges[1].ca_file)

    def test_the_configmap_and_key_reach_the_forge(self):
        path = self.write(
            '{"forges": [{"provider": "gitlab", "host": "gitlab.internal", "tokenPath": "/t",'
            ' "allowedPaths": ["g"], "caFile": "/ca/gitlab/ca.crt", "caConfigMap": "gl-ca", "caKey": "root.pem"}]}'
        )
        forge = [f for f in providers.build_forges({"forges": load_forge_entries(path)}) if f.name == "gitlab"][0]
        self.assertEqual("the ConfigMap gl-ca or its key root.pem", forge.ca_source)

    def test_gitlab_com_never_takes_a_ca(self):
        # The operator refuses it; a configuration written
        # another way is refused when the broker builds its forges.
        for host in ("gitlab.com", "www.gitlab.com"):
            with self.subTest(host=host), self.assertRaisesRegex(ValueError, "only a self-managed host"):
                providers.build_forges({"forges": [
                    {"provider": "gitlab", "host": host, "token_path": "/t", "allowed_paths": ("g",),
                     "ca_file": "/ca/ca.crt"},
                ]})

    def test_a_ca_file_that_is_not_pem_is_unloadable_in_the_api_client(self):
        # The ConfigMap exists, but its key holds no certificate: a different
        # fix from a missing ConfigMap, and the same answer as git's.
        transport_module._CA_CONTEXTS.clear()
        for name, content in (("text", "not a certificate\n"), ("empty", ""),
                              ("garbled", "-----BEGIN CERTIFICATE-----\nnotbase64!!\n-----END CERTIFICATE-----\n")):
            with self.subTest(name=name):
                path = self.write(content)
                with self.assertRaises(WorkspaceError) as caught:
                    HttpTransport(
                        "https://gitlab.internal/api/v4", lambda: {}, timeout=5.0, max_bytes=1024,
                        ca_file=path, ca_source="the ConfigMap gl-ca or its key root.pem",
                    ).api("GET", "user")
                self.assertEqual(providers.errors.TLS_GUIDANCE["ca_unloadable"], str(caught.exception))
                self.assertIn("could not be loaded (not PEM, or no certificate in it)",
                              caught.exception.fields["detail"])

    def test_a_missing_ca_file_names_its_configmap_and_key_in_the_api_client(self):
        transport_module._CA_CONTEXTS.clear()
        with self.assertRaises(WorkspaceError) as caught:
            HttpTransport(
                "https://gitlab.internal/api/v4", lambda: {}, timeout=5.0, max_bytes=1024,
                ca_file="/nonexistent/ca.crt", ca_source="the ConfigMap gl-ca or its key root.pem",
            ).api("GET", "user")
        self.assertEqual("FORGE_TLS_UNTRUSTED", caught.exception.fields["code"])
        self.assertIn("the ConfigMap gl-ca or its key root.pem is missing", caught.exception.fields["detail"])
        self.assertEqual(providers.errors.TLS_GUIDANCE["ca_missing"], str(caught.exception))

    def test_a_relative_ca_file_is_refused(self):
        path = self.write(
            '{"forges": [{"provider": "gitlab", "host": "gitlab.internal", "tokenPath": "/t",'
            ' "allowedPaths": ["g"], "caFile": "ca.crt"}]}'
        )
        with self.assertRaisesRegex(ValueError, "caFile .* must be an absolute path"):
            load_forge_entries(path)

    def test_the_broker_hands_the_forges_ca_to_its_transport(self):
        import vcs_broker

        forge, saas = providers.build_forges({"forges": [
            {"provider": "gitlab", "host": "gitlab.internal", "token_path": "/t", "allowed_paths": ("g",),
             "ca_file": "/ca/ca.crt"},
            {"provider": "gitlab", "host": "gitlab.com", "token_path": "/t", "allowed_paths": ("g",)},
        ]})[-2:]
        broker = vcs_broker.VcsBroker.__new__(vcs_broker.VcsBroker)
        broker._http_timeout = 5.0
        broker._http_max_bytes = 1024
        broker._http_opener = None
        broker._request_deadline = lambda: None
        built = broker._transport(forge, "g/p")
        self.assertEqual("/ca/ca.crt", built._ca_file)
        self.assertEqual("", broker._transport(saas, "g/p")._ca_file)


class GitTrustTest(unittest.TestCase):
    ENTRIES = (
        {"provider": "github", "host": "github.com", "ca_file": ""},
        {"provider": "gitlab", "host": "gitlab.internal", "ca_file": "/ca/internal.crt"},
        {"provider": "gitlab", "host": "gitlab.example.com", "ca_file": ""},
    )

    def test_only_a_forge_that_names_a_ca_gets_a_url_scoped_pin(self):
        self.assertEqual(
            (
                ("http.https://gitlab.internal/.sslCAInfo", "/ca/internal.crt"),
                ("http.https://gitlab.internal/.followRedirects", "false"),
            ),
            credential_proxy.forge_ca_git_config(self.ENTRIES),
        )
        self.assertEqual((), credential_proxy.forge_ca_git_config(None))

    @unittest.skipUnless(shutil.which("git"), "git is needed to read the layer back")
    def test_git_applies_the_ca_to_that_host_and_to_no_other(self):
        # The layer the broker builds, read back by git itself: the URL match
        # is git's, so this is the scoping the broker relies on.
        environment = {
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            **credential_proxy._git_forced_config_environment(
                credential_proxy.forge_ca_git_config(self.ENTRIES)
            ),
        }

        def ca_for(url: str) -> str:
            result = subprocess.run(
                ["git", "config", "--get-urlmatch", "http.sslCAInfo", url],
                env=environment, capture_output=True, text=True, check=False,
            )
            return result.stdout.strip()

        self.assertEqual("/ca/internal.crt", ca_for("https://gitlab.internal/platform/infra.git"))
        self.assertEqual("", ca_for("https://github.com/acme/infra.git"))
        self.assertEqual("", ca_for("https://gitlab.com/acme/infra.git"))
        self.assertEqual("", ca_for("https://gitlab.internal.evil.test/x.git"))

        def redirects_for(url: str) -> str:
            result = subprocess.run(
                ["git", "config", "--get-urlmatch", "http.followRedirects", url],
                env=environment, capture_output=True, text=True, check=False,
            )
            return result.stdout.strip()

        # curl keeps the CA across a redirect, so the CA host
        # follows none; every other host keeps git's default.
        self.assertEqual("false", redirects_for("https://gitlab.internal/platform/infra.git"))
        self.assertEqual("", redirects_for("https://github.com/acme/infra.git"))

    def test_every_git_the_executor_runs_carries_the_pin(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with mock.patch.object(credential_proxy, "_configured_forge_entries", return_value=self.ENTRIES):
            executor = credential_proxy.CommandExecutor(
                timeout_seconds=5, max_output_bytes=1024, state_dir=tmp.name, scoped_pool=None
            )
        pairs = [
            (executor.environment[f"GIT_CONFIG_KEY_{i}"], executor.environment[f"GIT_CONFIG_VALUE_{i}"])
            for i in range(int(executor.environment["GIT_CONFIG_COUNT"]))
        ]
        self.assertIn(("http.https://gitlab.internal/.sslCAInfo", "/ca/internal.crt"), pairs)
        # The forced pins still come last, so nothing ahead of them can turn
        # one off.
        self.assertEqual(credential_proxy.GIT_FORCED_CONFIG[-1], pairs[-1])

    def test_an_unreadable_configuration_adds_no_pin(self):
        with mock.patch.object(providers, "load_forge_entries", side_effect=ValueError("bad")):
            self.assertEqual((), credential_proxy._configured_forge_entries())


@unittest.skipUnless(OPENSSL and shutil.which("git"), "openssl and git are needed")
class GitLoopbackTrustTest(unittest.TestCase):
    """The pin on the paths that clone and push: `execute_vcs_git` and
    `execute_workspace_git`, which rebuild the forced layer with the caller's
    credential config ahead of it. A real git talks TLS to a
    loopback server that a test CA signed: with the pin the handshake passes,
    and without it git refuses the certificate."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.ca, cert, key = make_pki(root / "pki", key_usage=True)
        server, self.port = serve_tls(cert, key)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.url = f"https://localhost:{self.port}/g/p.git"

    def executor(self, *, pinned: bool):
        entries = (
            {"host": f"localhost:{self.port}", "ca_file": str(self.ca),
             "ca_source": "the ConfigMap test-ca or its key ca.crt"},
        ) if pinned else ()
        state = Path(self.tmp.name) / ("pinned" if pinned else "bare")
        with mock.patch.object(credential_proxy, "_configured_forge_entries", return_value=entries), \
                mock.patch.dict(os.environ, {"CREDENTIAL_PROXY_CONTENT_WORKSPACE": "1"}):
            executor = credential_proxy.CommandExecutor(
                timeout_seconds=20, max_output_bytes=1 << 16, state_dir=str(state), scoped_pool=None
            )
        executor.vcs_root.mkdir(parents=True, exist_ok=True)
        executor.content_workspace_root.mkdir(parents=True, exist_ok=True)
        return executor

    # A credential's own config rides ahead of the pins on these paths; one is
    # passed so the layer is rebuilt the way a stored token's clone rebuilds it.
    CREDENTIAL_CONFIG = (("credential.helper", ""),)

    def vcs_ls_remote(self, executor):
        return executor.execute_vcs_git(
            ["git", "ls-remote", self.url], executor.vcs_root, check=False, config=self.CREDENTIAL_CONFIG
        )

    def test_the_vcs_path_trusts_the_pinned_ca(self):
        pinned = self.vcs_ls_remote(self.executor(pinned=True))
        self.assertIsNone(providers.classify_tls(pinned.stderr)[0] or None, pinned.stderr)
        bare = self.vcs_ls_remote(self.executor(pinned=False))
        self.assertEqual("untrusted", providers.classify_tls(bare.stderr)[0], bare.stderr)

    def test_the_workspace_path_trusts_the_pinned_ca(self):
        for pinned, expect in ((True, ""), (False, "untrusted")):
            with self.subTest(pinned=pinned):
                executor = self.executor(pinned=pinned)
                target = executor.content_workspace_root / "clone"
                result = executor.execute_workspace_git(
                    ["git", "clone", "--quiet", self.url, str(target / "repo")],
                    executor.content_workspace_root,
                    config=self.CREDENTIAL_CONFIG,
                )
                self.assertEqual(expect, providers.classify_tls(result.stderr)[0], result.stderr)

    def test_a_missing_ca_file_names_its_configmap_and_key(self):
        executor = self.executor(pinned=True)
        Path(self.ca).unlink()
        result = self.vcs_ls_remote(executor)
        refusal = executor.tls_refusal(result.stderr)
        self.assertIsNotNone(refusal, result.stderr)
        self.assertEqual("FORGE_TLS_UNTRUSTED", refusal.fields["code"])
        self.assertIn("the ConfigMap test-ca or its key ca.crt is missing", refusal.fields["detail"])


class CertificateFailureTest(unittest.TestCase):
    def test_gits_certificate_errors_are_recognised(self):
        for line in (
            "fatal: unable to access 'https://gitlab.internal/g/p.git/': server verification failed: "
            "certificate signer not trusted. (CAfile: /etc/ssl/certs/ca-certificates.crt CRLfile: none)",
            "fatal: unable to access 'https://h/x/': SSL certificate problem: unable to get local issuer certificate",
            "fatal: unable to access 'https://h/x/': error setting certificate file: /etc/kube-agents/forge-ca/gitlab/ca.crt",
        ):
            with self.subTest(line=line[:40]):
                self.assertTrue(providers.classify_tls("Cloning into 'repo'...\n" + line)[0])

    def test_an_older_gnutls_verification_failure_reads_as_untrusted(self):
        # Ubuntu's git (the CI runner's) gives no reason with the failure.
        line = ("fatal: unable to access 'https://localhost:40867/g/p.git/': "
                "server certificate verification failed. CAfile: none CRLfile: none")
        self.assertEqual("untrusted", providers.classify_tls(line)[0])

    # git 2.47.3 with libcurl-gnutls, as measured in the broker image.
    GNUTLS = {
        "untrusted": "fatal: unable to access 'https://gitlab.internal/g/p.git/': server verification failed: "
                     "certificate signer not trusted. (CAfile: /etc/ssl/certs/ca-certificates.crt CRLfile: none)",
        "hostname": "fatal: unable to access 'https://gitlab.internal/g/p.git/': SSL: certificate subject name "
                    "(other.example) does not match target hostname 'gitlab.internal'",
        "expired": "fatal: unable to access 'https://gitlab.internal/g/p.git/': server verification failed: "
                   "certificate has expired. (CAfile: /tmp/combined.pem CRLfile: none)",
        "ca_missing": "fatal: unable to access 'https://gitlab.internal/g/p.git/': Problem with the SSL CA cert "
                      "(path? access rights?)",
    }
    # Another git and curl build words a missing CA file this way.
    OTHER_CA_MISSING = ("fatal: unable to access 'https://gitlab.internal/g/p.git/': error adding trust anchors "
                        "from file: /etc/kube-agents/forge-ca/gitlab/ca.crt")

    def test_each_cause_is_told_apart_with_its_own_advice(self):
        # Before, an expired certificate and a wrong name read as a
        # missing CA, and git's hostname mismatch matched nothing at all.
        for kind, line in self.GNUTLS.items():
            with self.subTest(kind=kind):
                refusal = providers.tls_refusal("Cloning into 'repo'...\n" + line,
                                                {"gitlab.internal": "the ConfigMap gl-ca or its key ca.crt"})
                self.assertEqual("FORGE_TLS_UNTRUSTED", refusal.fields["code"])
                self.assertEqual(502, refusal.status)
                self.assertTrue(refusal.fields["detail"].startswith("gitlab.internal: "))
                # git words a missing CA file and a malformed one alike.
                self.assertEqual("ca_load" if kind == "ca_missing" else kind, providers.classify_tls(line)[0])
        advice = {kind: str(providers.tls_refusal(line)) for kind, line in self.GNUTLS.items()}
        self.assertIn("caBundleRef", advice["untrusted"])
        self.assertIn("TLS-inspecting proxy", advice["untrusted"])
        self.assertIn("public host", advice["untrusted"])
        self.assertNotIn("caBundleRef", advice["expired"])
        self.assertNotIn("caBundleRef", advice["hostname"])
        self.assertIn("renews", advice["expired"])
        self.assertIn("does not name the host", advice["hostname"])

    def test_a_missing_ca_file_from_git_names_the_configmap_and_key(self):
        for line in (self.GNUTLS["ca_missing"], self.OTHER_CA_MISSING):
            with self.subTest(line=line[60:100]):
                refusal = providers.tls_refusal(line, {"gitlab.internal": "the ConfigMap gl-ca or its key root.pem"})
                self.assertIn("the ConfigMap gl-ca or its key root.pem is missing", refusal.fields["detail"])

    def test_a_ca_file_git_could_not_load_is_unloadable_when_it_is_there(self):
        # GnuTLS prints "Problem with the SSL CA cert" for a missing file and
        # for a garbled one alike (measured in the broker image), so the
        # broker asks whether the forge's file exists.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        present = Path(tmp.name) / "ca.crt"
        present.write_text("-----BEGIN CERTIFICATE-----\nnotbase64!!\n-----END CERTIFICATE-----\n")
        sources = {"gitlab.internal": "the ConfigMap gl-ca or its key root.pem"}
        for line in (self.GNUTLS["ca_missing"], self.OTHER_CA_MISSING):
            with self.subTest(line=line[60:100]):
                there = providers.tls_refusal(line, sources, {"gitlab.internal": str(present)})
                self.assertEqual(providers.errors.TLS_GUIDANCE["ca_unloadable"], str(there))
                self.assertIn("the CA file from ConfigMap gl-ca or its key root.pem could not be loaded",
                              there.fields["detail"])
                gone = providers.tls_refusal(line, sources, {"gitlab.internal": str(Path(tmp.name) / "gone.crt")})
                self.assertEqual(providers.errors.TLS_GUIDANCE["ca_missing"], str(gone))
                self.assertIn("is missing", gone.fields["detail"])

    def test_the_servers_own_words_are_not_read_as_a_verdict(self):
        # A hook or a project description can print anything.
        self.assertEqual(("", ""), providers.classify_tls("remote: certificate verify failed\nremote: done"))
        self.assertIsNone(providers.tls_refusal("remote: SSL certificate problem: self-signed certificate"))

    def test_python_names_the_same_causes_from_the_verifiers_code(self):
        def failure(code, message):
            error = ssl.SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
            error.verify_code = code
            error.verify_message = message
            return urllib.error.URLError(error)

        for code, message, kind in (
            (20, "unable to get local issuer certificate", "untrusted"),
            (10, "certificate has expired", "expired"),
            (9, "certificate is not yet valid", "expired"),
            (62, "Hostname mismatch, certificate is not valid for 'gitlab.internal'.", "hostname"),
        ):
            with self.subTest(code=code):
                self.assertEqual(kind, transport_module._certificate_failure(failure(code, message))[0])
                with self.assertRaises(WorkspaceError) as caught:
                    HttpTransport(
                        "https://gitlab.internal/api/v4", lambda: {}, timeout=5.0, max_bytes=1024,
                        opener=lambda request, timeout: (_ for _ in ()).throw(failure(code, message)),
                    ).api("GET", "user")
                self.assertEqual(providers.errors.TLS_GUIDANCE[kind], str(caught.exception))

    def test_other_failures_are_not(self):
        self.assertEqual(("", ""), providers.classify_tls("fatal: could not read Username for 'https://h': No such device"))
        self.assertEqual(("", ""), providers.classify_tls(""))

    def test_the_content_workspace_answers_a_certificate_by_name(self):
        import content_workspace

        stderr = (
            "fatal: unable to access 'https://gitlab.internal/g/p.git/': server verification failed: "
            "certificate signer not trusted."
        )

        class Result:
            exit_code = 128
            stdout = ""

        Result.stderr = stderr
        store = content_workspace.ContentWorkspaceStore.__new__(content_workspace.ContentWorkspaceStore)
        store._runner = lambda argv, cwd, **kwargs: Result()
        store._redact = lambda text: str(text)
        store._tls_refusal = providers.tls_refusal
        with self.assertRaises(content_workspace.TlsUntrusted) as caught:
            store._git(Path("/tmp"), ["clone", "--quiet", "https://gitlab.internal/g/p.git", "repo"])
        self.assertEqual("FORGE_TLS_UNTRUSTED", caught.exception.code)
        self.assertEqual(502, caught.exception.status)
        self.assertIn("caBundleRef", str(caught.exception))
        Result.stderr = "fatal: repository not found"
        with self.assertRaises(content_workspace.GitFailed):
            store._git(Path("/tmp"), ["clone", "--quiet", "https://gitlab.internal/g/p.git", "repo"])


if __name__ == "__main__":
    unittest.main()
