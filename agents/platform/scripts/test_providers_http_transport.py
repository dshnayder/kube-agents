#!/usr/bin/env python3
"""The in-process transport a forge with a REST API is reached through.

    python3 -m pytest -q agents/platform/scripts/test_providers_http_transport.py

No network: every test hands the transport an opener that records the request
and answers from a fixture, which is also how the broker's tests reach it. What
is pinned is what the transport owns and no forge may -- the URL it composes,
the bounds it enforces, how a refusal becomes a caller-facing answer, and that
the credential never follows a redirect.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import providers
import vcs_broker
from providers.transport import HttpTransport, _RefuseRedirect
from workspace_paths import WorkspaceError

BASE = "https://forge.example.test/api/v4"


class _Response(io.BytesIO):
    def __init__(self, body: bytes, status: int = 200) -> None:
        super().__init__(body)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class Opener:
    """Records each request and answers with the next fixture."""

    def __init__(self, *answers) -> None:
        self.answers = list(answers)
        self.requests: list[urllib.request.Request] = []
        self.timeouts: list[float] = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        self.timeouts.append(timeout)
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        if isinstance(answer, _Response):
            return answer
        return _Response(json.dumps(answer).encode())


def refusal(status: int, body: object) -> urllib.error.HTTPError:
    text = body if isinstance(body, str) else json.dumps(body)
    return urllib.error.HTTPError(
        f"{BASE}/x", status, "refused", {}, io.BytesIO(text.encode())
    )


def transport(opener, headers=None, **kwargs) -> HttpTransport:
    kwargs.setdefault("timeout", 7.0)
    kwargs.setdefault("max_bytes", 1 << 16)
    return HttpTransport(BASE, headers or (lambda: {"PRIVATE-TOKEN": "t"}), opener=opener, **kwargs)


class RequestTest(unittest.TestCase):
    def test_a_get_is_composed_from_the_base_the_path_and_the_params(self):
        opener = Opener([{"iid": 1}])
        answer = transport(opener).api(
            "GET", "projects/acme%2Finfra/merge_requests", params={"state": "opened", "page": None}
        )
        self.assertEqual([{"iid": 1}], answer)
        request = opener.requests[0]
        self.assertEqual("GET", request.get_method())
        self.assertEqual(f"{BASE}/projects/acme%2Finfra/merge_requests?state=opened", request.full_url)
        self.assertEqual("application/json", request.get_header("Accept"))
        self.assertEqual([7.0], opener.timeouts)

    def test_the_credential_headers_are_read_per_call(self):
        # A rotated token file is the next call's token, with no restart.
        tokens = iter(["old", "new"])
        opener = Opener({}, {})
        sender = transport(opener, headers=lambda: {"PRIVATE-TOKEN": next(tokens)})
        sender.api("GET", "user")
        sender.api("GET", "user")
        self.assertEqual(
            ["old", "new"], [r.get_header("Private-token") for r in opener.requests]
        )

    def test_a_body_is_sent_as_json(self):
        opener = Opener({"iid": 3})
        transport(opener).api("POST", "projects/1/issues", body={"title": "t"})
        request = opener.requests[0]
        self.assertEqual("POST", request.get_method())
        self.assertEqual({"title": "t"}, json.loads(request.data))
        self.assertEqual("application/json", request.get_header("Content-type"))

    def test_a_raw_answer_is_returned_as_text_under_the_media_type_asked(self):
        opener = Opener(_Response(b"diff --git a/x b/x\n"))
        text = transport(opener).api("GET", "projects/1/merge_requests/2/raw_diffs", raw="text/plain")
        self.assertEqual("diff --git a/x b/x\n", text)
        self.assertEqual("text/plain", opener.requests[0].get_header("Accept"))

    def test_a_path_that_names_a_host_or_climbs_out_is_not_sent(self):
        # The credential header goes wherever the URL does.
        for path in ("https://elsewhere.test/api", "projects/../../admin", "../user"):
            with self.subTest(path=path):
                opener = Opener()
                with self.assertRaises(WorkspaceError) as caught:
                    transport(opener).api("GET", path)
                self.assertEqual("FORGE_CALL_FAILED", caught.exception.fields["code"])
                self.assertEqual([], opener.requests)

    def test_only_https_is_accepted(self):
        with self.assertRaises(ValueError):
            HttpTransport("http://forge.example.test/api/v4", dict, timeout=1, max_bytes=1)


class BoundsTest(unittest.TestCase):
    def test_an_answer_over_the_ceiling_is_refused_not_truncated(self):
        opener = Opener(_Response(b"x" * 65))
        with self.assertRaises(WorkspaceError) as caught:
            transport(opener, max_bytes=64).api("GET", "projects")
        self.assertEqual("FORGE_RESPONSE_TOO_LARGE", caught.exception.fields["code"])

    def test_a_call_that_got_no_answer_is_a_call_failure(self):
        opener = Opener(urllib.error.URLError("connection refused"))
        with self.assertRaises(WorkspaceError) as caught:
            transport(opener).api("GET", "projects")
        self.assertEqual(502, caught.exception.status)
        self.assertEqual("FORGE_CALL_FAILED", caught.exception.fields["code"])

    def test_a_timeout_is_a_call_failure(self):
        opener = Opener(TimeoutError("timed out"))
        with self.assertRaises(WorkspaceError) as caught:
            transport(opener).api("GET", "projects")
        self.assertEqual("FORGE_CALL_FAILED", caught.exception.fields["code"])

    def test_a_redirect_is_never_followed(self):
        # The credential rides in a header; a hop would present it to wherever
        # the redirect pointed.
        handler = _RefuseRedirect()
        self.assertIsNone(
            handler.redirect_request(None, None, 302, "Found", {}, "https://elsewhere.test/")
        )
        opener = transport(None)._open.__self__
        self.assertTrue(any(isinstance(h, _RefuseRedirect) for h in opener.handlers))

    def test_a_redirect_status_is_a_refusal(self):
        opener = Opener(refusal(302, "moved"))
        with self.assertRaises(WorkspaceError) as caught:
            transport(opener).api("GET", "projects")
        self.assertEqual("FORGE_CALL_FAILED", caught.exception.fields["code"])


class RefusalTest(unittest.TestCase):
    def refuse(self, status, body, **kwargs):
        with self.assertRaises(WorkspaceError) as caught:
            transport(Opener(refusal(status, body)), **kwargs).api("GET", "projects/1")
        return caught.exception

    def test_a_status_takes_the_shared_guidance_and_the_forges_reason(self):
        err = self.refuse(404, {"message": "404 Project Not Found"})
        self.assertEqual(404, err.status)
        self.assertEqual("FORGE_NOT_FOUND", err.fields["code"])
        self.assertIn("404 Project Not Found", err.fields["detail"])

    def test_an_error_key_is_read_as_well_as_message(self):
        self.assertIn("404 Not Found", self.refuse(404, {"error": "404 Not Found"}).fields["detail"])

    def test_a_message_that_is_a_list_is_joined(self):
        err = self.refuse(409, {"message": ["Another open merge request already exists"]})
        self.assertEqual("FORGE_CONFLICT", err.fields["code"])
        self.assertIn("Another open merge request already exists", err.fields["detail"])

    def test_per_field_reasons_name_their_field(self):
        err = self.refuse(422, {"message": {"title": ["is too long"]}})
        self.assertEqual("FORGE_REJECTED", err.fields["code"])
        self.assertIn("title: is too long", err.fields["detail"])

    def test_a_plain_text_body_gives_its_first_line(self):
        self.assertIn("upstream gone", self.refuse(500, "upstream gone\nmore").fields["detail"])

    def test_a_forge_override_is_applied(self):
        named = providers.Guidance(401, "FORGE_TOKEN_EXPIRED", "the token in Secret x expired")
        err = self.refuse(401, {"message": "401 Unauthorized"}, overrides={401: named})
        self.assertEqual("FORGE_TOKEN_EXPIRED", err.fields["code"])


class WhoamiTest(unittest.TestCase):
    def test_the_declared_route_names_the_login(self):
        opener = Opener({"username": "group_42_bot_abc", "id": 9})
        login = transport(opener, whoami_route=("user", "username")).whoami()
        self.assertEqual("group_42_bot_abc", login)
        self.assertTrue(opener.requests[0].full_url.endswith("/user"))

    def test_no_route_says_so_rather_than_guessing(self):
        self.assertEqual("", transport(Opener()).whoami())

    def test_a_refused_lookup_raises_rather_than_answering_empty(self):
        opener = Opener(refusal(401, {"message": "401 Unauthorized"}))
        with self.assertRaises(WorkspaceError):
            transport(opener, whoami_route=("user", "username")).whoami()


class _HttpForge(providers.Forge):
    name = "httpforge"
    hosts = ("forge.example.test",)
    transport = "http"
    verbs = ("issue-view",)

    def __init__(self, api_url=BASE):
        super().__init__()
        self.api_url = api_url


class BrokerBuildsItTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def broker(self, **kwargs):
        return vcs_broker.VcsBroker(self.root, git_runner=lambda *a, **k: None, **kwargs)

    def test_an_http_forge_gets_the_in_process_transport_with_the_brokers_bounds(self):
        opener = Opener({})
        broker = self.broker(http_timeout=3.0, http_max_bytes=99, http_opener=opener)
        built = broker._transport(_HttpForge(), "acme/infra")
        self.assertIsInstance(built, HttpTransport)
        built.api("GET", "user")
        self.assertEqual([3.0], opener.timeouts)
        self.assertEqual(99, built._max_bytes)

    def test_an_http_forge_with_no_api_root_is_refused_by_name(self):
        with self.assertRaises(WorkspaceError) as caught:
            self.broker()._transport(_HttpForge(api_url=""), "acme/infra")
        self.assertEqual("FORGE_UNSUPPORTED", caught.exception.fields["code"])


if __name__ == "__main__":
    unittest.main()
