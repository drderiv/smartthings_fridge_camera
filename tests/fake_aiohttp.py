"""A tiny stand-in for ``aioresponses``: intercept aiohttp requests in tests.

    with aioresponses() as m:
        m.get("https://example.test/x", status=200, payload={"a": 1})
        ...
        assert m.requests[("GET", URL("https://example.test/x"))][0].kwargs["headers"]

Responses repeat by default (the most recently registered match wins); pass ``repeat=False``
to make one-shot responses, which are consumed in registration order before repeating ones.
Requests with no match raise ``aiohttp.ClientConnectionError``.
"""
from __future__ import annotations

import json as jsonlib
import re
from types import SimpleNamespace
from unittest import mock

import aiohttp
from multidict import CIMultiDict
from yarl import URL


class _FakeResponse:
    def __init__(self, method, url, status, body, headers):
        self.method = method
        self.url = url
        self.status = status
        self._body = body
        self.headers = CIMultiDict(headers)

    @property
    def ok(self):
        return self.status < 400

    @property
    def content_type(self):
        return self.headers.get("Content-Type", "application/octet-stream").split(";")[0].strip()

    async def read(self):
        return self._body

    async def text(self, encoding=None, errors="strict"):
        return self._body.decode(encoding or "utf-8", errors)

    async def json(self, *, encoding=None, loads=jsonlib.loads, content_type="application/json"):
        return loads(self._body)

    def raise_for_status(self):
        if self.status >= 400:
            info = aiohttp.RequestInfo(self.url, self.method, CIMultiDict(), self.url)
            raise aiohttp.ClientResponseError(info, (), status=self.status, message="mocked error")

    def release(self):
        return None

    async def wait_for_close(self):
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class aioresponses:  # noqa: N801 - mirrors the library name used throughout the tests
    def __init__(self):
        self._matchers = []
        self.requests = {}
        self._patch = None

    # --- registration ---------------------------------------------------------
    def add(self, method, url, *, status=200, payload=None, body=None, headers=None, repeat=True, exception=None):
        headers = dict(headers or {})
        if payload is not None:
            body = jsonlib.dumps(payload)
            headers.setdefault("Content-Type", "application/json")
        if isinstance(body, str):
            body = body.encode()
        self._matchers.append(
            {"method": method.upper(), "url": url, "status": status, "body": body or b"",
             "headers": headers, "repeat": repeat, "exception": exception, "used": False}
        )

    def get(self, url, **kwargs):
        self.add("GET", url, **kwargs)

    def post(self, url, **kwargs):
        self.add("POST", url, **kwargs)

    # --- interception ---------------------------------------------------------
    async def _request(self, session, method, url, **kwargs):
        full = URL(str(url))
        if kwargs.get("params"):
            full = full.update_query(kwargs["params"])
        method = method.upper()
        self.requests.setdefault((method, full), []).append(SimpleNamespace(args=(), kwargs=kwargs))
        one_shot = [m for m in self._matchers if not m["repeat"] and not m["used"]]
        repeating = [m for m in reversed(self._matchers) if m["repeat"]]
        for matcher in one_shot + repeating:
            if matcher["method"] != method:
                continue
            target = matcher["url"]
            if isinstance(target, re.Pattern):
                hit = target.match(str(full)) is not None
            else:
                wanted = URL(str(target))
                # A registered URL without a query matches that URL with any query string.
                hit = wanted == full if wanted.query_string else wanted == full.with_query(None)
            if not hit:
                continue
            matcher["used"] = True
            if matcher["exception"] is not None:
                raise matcher["exception"]
            return _FakeResponse(method, full, matcher["status"], matcher["body"], matcher["headers"])
        raise aiohttp.ClientConnectionError(f"No mock registered for {method} {full}")

    def __enter__(self):
        outer = self

        async def fake_request(session, method, url, **kwargs):
            return await outer._request(session, method, url, **kwargs)

        self._patch = mock.patch.object(aiohttp.ClientSession, "_request", fake_request)
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()
        return False

    # --- helpers used by assertions ------------------------------------------------
    @property
    def calls(self):
        return [call for group in self.requests.values() for call in group]

    def count(self, url_part: str) -> int:
        """Number of requests whose URL contains ``url_part``."""
        return sum(len(group) for (_method, url), group in self.requests.items() if url_part in str(url))
