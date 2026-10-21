"""``save_facility_crew_to_github`` must go through ``catalog_storage`` so the
target branch is the one resolved from ``st.secrets["github_branch"]`` -- not a
hardcoded ``"main"``, which is what sent the staging app's crew saves into the
live data file -- and so it gets the same SHA-conflict retry as every other
storage module.
"""
import base64
import json
from unittest import mock

import pandas as pd
import pytest

import core.catalog_storage as cs
import core.facility_storage as fs


def _crew_df():
    return pd.DataFrame(
        {"HC": [2, 4], "Conc": [2, 4], "Crew": [1, 1]},
        index=pd.Index(["Warehouse (Pick)", "Wire Assembly"], name="Station"),
    )


EXISTING = {
    "Henderson": {"Warehouse (Pick)": {"HC": 1, "Conc": 1, "Crew": 1}},
    "Cypress": {"QC": {"HC": 3, "Conc": 3, "Crew": 1}},
}


@pytest.fixture
def local_crew(monkeypatch, tmp_path):
    path = tmp_path / "facility_crew.json"
    monkeypatch.setattr(fs, "FACILITY_CREW_PATH", path)
    return path


def test_module_has_no_branch_or_http_client_of_its_own():
    assert not hasattr(fs, "GITHUB_BRANCH")
    assert not hasattr(fs, "GITHUB_REPO")
    assert not hasattr(fs, "requests")


def test_save_goes_through_catalog_storage_and_keeps_other_facilities(monkeypatch, local_crew):
    fetch = mock.Mock(return_value=(json.dumps(EXISTING).encode("utf-8"), "sha-1"))
    save = mock.Mock(return_value={"commit": {"sha": "abc1234"}})
    monkeypatch.setattr(fs, "fetch_catalog_csv_from_github", fetch)
    monkeypatch.setattr(fs, "save_catalog_to_github", save)

    out = fs.save_facility_crew_to_github("Henderson", _crew_df(), "tok")

    assert out == {"commit": {"sha": "abc1234"}}
    fetch.assert_called_once_with(fs.GITHUB_FILE_PATH, "tok")
    (content, path, token), kwargs = save.call_args
    assert (path, token, kwargs["sha"]) == (fs.GITHUB_FILE_PATH, "tok", "sha-1")
    assert "Henderson crew" in kwargs["message"]
    written = json.loads(content)
    assert written["Cypress"] == EXISTING["Cypress"]          # untouched
    assert written["Henderson"] == {
        "Warehouse (Pick)": {"HC": 2, "Conc": 2, "Crew": 1},
        "Wire Assembly": {"HC": 4, "Conc": 4, "Crew": 1},
    }
    # mirrored locally so the same container sees it before the redeploy
    assert json.loads(local_crew.read_text(encoding="utf-8")) == written


class _Resp:
    def __init__(self, status, payload):
        self.status_code, self._payload, self.text = status, payload, ""

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def test_save_targets_the_branch_catalog_storage_resolved(monkeypatch, local_crew):
    """The regression: with github_branch = "staging" both the GET and the PUT
    must name the staging branch."""
    monkeypatch.setattr(cs, "GITHUB_BRANCH", "staging")
    calls = {}

    def fake_get(url, headers=None, params=None, timeout=None):
        calls["get_ref"] = (params or {}).get("ref")
        content = base64.b64encode(json.dumps(EXISTING).encode("utf-8")).decode("ascii")
        return _Resp(200, {"sha": "sha-1", "content": content})

    def fake_put(url, headers=None, json=None, timeout=None):
        calls["put_branch"] = json["branch"]
        calls["put_sha"] = json.get("sha")
        return _Resp(200, {"commit": {"sha": "zzz"}})

    monkeypatch.setattr(cs.requests, "get", fake_get)
    monkeypatch.setattr(cs.requests, "put", fake_put)

    fs.save_facility_crew_to_github("Henderson", _crew_df(), "tok")

    assert calls == {"get_ref": "staging", "put_branch": "staging", "put_sha": "sha-1"}


def test_save_retries_once_on_sha_conflict(monkeypatch, local_crew):
    fetch = mock.Mock(side_effect=[
        (json.dumps(EXISTING).encode("utf-8"), "sha-1"),
        (json.dumps(EXISTING).encode("utf-8"), "sha-2"),
    ])
    save = mock.Mock(side_effect=[cs.GitHubConflict("409"), {"ok": True}])
    monkeypatch.setattr(fs, "fetch_catalog_csv_from_github", fetch)
    monkeypatch.setattr(fs, "save_catalog_to_github", save)

    assert fs.save_facility_crew_to_github("Henderson", _crew_df(), "tok") == {"ok": True}
    assert fetch.call_count == 2
    assert [c.kwargs["sha"] for c in save.call_args_list] == ["sha-1", "sha-2"]


def test_save_gives_up_after_the_second_conflict(monkeypatch, local_crew):
    monkeypatch.setattr(fs, "fetch_catalog_csv_from_github",
                        mock.Mock(return_value=(json.dumps(EXISTING).encode("utf-8"), "sha-1")))
    save = mock.Mock(side_effect=cs.GitHubConflict("409"))
    monkeypatch.setattr(fs, "save_catalog_to_github", save)
    with pytest.raises(cs.GitHubConflict):
        fs.save_facility_crew_to_github("Henderson", _crew_df(), "tok")
    assert save.call_count == 2


def test_unreadable_existing_file_aborts_instead_of_clobbering(monkeypatch, local_crew):
    monkeypatch.setattr(fs, "fetch_catalog_csv_from_github",
                        mock.Mock(return_value=(b"", "sha-1")))   # exists, content missing
    save = mock.Mock()
    monkeypatch.setattr(fs, "save_catalog_to_github", save)
    with pytest.raises(RuntimeError):
        fs.save_facility_crew_to_github("Henderson", _crew_df(), "tok")
    save.assert_not_called()
    assert not local_crew.exists()


def test_true_404_seeds_from_the_local_copy(monkeypatch, local_crew):
    local_crew.write_text(
        json.dumps({"Spartanburg": {"PDI": {"HC": 2, "Conc": 2, "Crew": 1}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(fs, "fetch_catalog_csv_from_github",
                        mock.Mock(return_value=(b"", None)))      # genuine 404
    save = mock.Mock(return_value={})
    monkeypatch.setattr(fs, "save_catalog_to_github", save)

    fs.save_facility_crew_to_github("Henderson", _crew_df(), "tok")

    (content, _, _), kwargs = save.call_args
    assert kwargs["sha"] is None
    written = json.loads(content)
    assert set(written) == {"Spartanburg", "Henderson"}
