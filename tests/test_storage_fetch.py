"""Regression tests for the GitHub fetch path that caused a data-loss wipe.

The bug: ``data/uploaded_schedules.json`` crossed 1 MB (it stores a full CSV per
saved schedule). GitHub's *Contents* API omits ``content`` for files > 1 MB, so
the fetch returned empty content WITH a valid sha. The save/delete code then
wrote an empty dict back over the file with that sha — wiping every saved
schedule. These tests lock in the two fixes:

  1. ``fetch_catalog_csv_from_github`` follows the *blob* API when the Contents
     API returns empty inline content (so large files actually load).
  2. ``_fetch_existing_from_github`` RAISES (never returns ``{}``) when the file
     exists (sha present) but content came back empty — so a transient fetch
     failure can't clobber the file.
"""
import base64
import json
from unittest import mock

import pytest

import core.catalog_storage as cs
import core.uploaded_schedule_storage as us
import core.scenario_storage as ss
import core.exec_scenario_storage as es
import core.facility_settings_storage as fss


class _FakeResp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _b64(s: str) -> str:
    return base64.b64encode(s.encode("utf-8")).decode("ascii")


def test_fetch_follows_blob_api_for_large_file():
    """Contents API returns empty content + git_url (the >1 MB case); the
    fetch must follow git_url (blob API) and return the real bytes + sha."""
    big = json.dumps({"Henderson": {"x": 1}})
    contents_resp = _FakeResp(200, {
        "sha": "abc123", "content": "", "encoding": "none",
        "git_url": "https://api.github.com/repos/o/r/git/blobs/abc123",
    })
    blob_resp = _FakeResp(200, {"content": _b64(big), "encoding": "base64"})

    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        return contents_resp if "git/blobs" not in url else blob_resp

    with mock.patch.object(cs.requests, "get", side_effect=fake_get):
        content, sha = cs.fetch_catalog_csv_from_github("data/x.json", "tok")
    assert sha == "abc123"
    assert content.decode("utf-8") == big
    assert any("git/blobs" in u for u in calls), "should follow the blob API"


def test_fetch_404_is_empty_start():
    with mock.patch.object(cs.requests, "get", return_value=_FakeResp(404)):
        content, sha = cs.fetch_catalog_csv_from_github("data/x.json", "tok")
    assert content == b"" and sha is None


def test_fetch_exists_but_unreadable_returns_sha_not_none():
    """Contents empty AND blob fetch yields nothing, but the file exists →
    return (b"", sha) so callers can refuse to overwrite."""
    contents_resp = _FakeResp(200, {"sha": "s9", "content": "", "git_url": None})
    with mock.patch.object(cs.requests, "get", return_value=contents_resp):
        content, sha = cs.fetch_catalog_csv_from_github("data/x.json", "tok")
    assert content == b"" and sha == "s9"


@pytest.mark.parametrize("mod", [us, ss, es, fss])
def test_fetch_existing_raises_when_exists_but_empty(mod):
    """The guard: empty content WITH a sha (file exists) must RAISE, never
    return {} — otherwise the subsequent save wipes the whole file."""
    with mock.patch.object(mod, "fetch_catalog_csv_from_github",
                           return_value=(b"", "real-sha")):
        with pytest.raises(RuntimeError):
            mod._fetch_existing_from_github("tok")


@pytest.mark.parametrize("mod", [us, ss, es, fss])
def test_fetch_existing_empty_start_on_true_404(mod):
    """A genuine 404 (sha is None) is a legitimate empty start, not an error."""
    with mock.patch.object(mod, "fetch_catalog_csv_from_github",
                           return_value=(b"", None)):
        parsed, sha = mod._fetch_existing_from_github("tok")
    assert parsed == {} and sha is None


@pytest.mark.parametrize("mod", [us, ss, es, fss])
def test_fetch_existing_parses_valid_json(mod):
    payload = json.dumps({"Henderson": {"plan": {"rows": 3}}}).encode("utf-8")
    with mock.patch.object(mod, "fetch_catalog_csv_from_github",
                           return_value=(payload, "sha1")):
        parsed, sha = mod._fetch_existing_from_github("tok")
    assert parsed["Henderson"]["plan"]["rows"] == 3 and sha == "sha1"
