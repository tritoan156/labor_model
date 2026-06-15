"""CSV catalog persistence — save machine_clean.csv / acc_clean.csv to GitHub.

Used by the Floor Verification tab so the floor team can update labor times
through the UI and have all users see the new values after redeploy.

Concurrency model:
  • Saves always start by **fetching the current file from GitHub** so
    concurrent users editing different cells don't overwrite each other.
  • The PUT uses the GitHub SHA as an optimistic lock — if another commit
    landed between our fetch and our PUT, the API returns 409 and the caller
    can refetch + retry.

Requires a GitHub Personal Access Token in `st.secrets["github_token"]`
(same token used for facility_storage).
"""
from __future__ import annotations

import base64
import os
from typing import Optional, Tuple

import requests

GITHUB_REPO = "tritoan156/labor_model"


def _resolve_branch() -> str:
    """Pick which git branch the save helpers read from / write to.

    Resolution order:
      1. ``st.secrets["github_branch"]`` if a Streamlit context is available
         (preferred on Streamlit Cloud — set this in the secrets panel).
      2. ``GITHUB_BRANCH`` environment variable.
      3. Default ``"main"``.

    The staging Streamlit Cloud app sets ``github_branch = "staging"`` in
    its secrets so every save it does targets the ``staging`` branch on the
    same repo — keeping experimental data completely isolated from the
    live ``main`` branch the team uses.
    """
    try:
        import streamlit as st  # local import to keep this module Streamlit-optional
        try:
            val = st.secrets.get("github_branch")
            if val:
                return str(val).strip()
        except Exception:
            pass
    except Exception:
        pass
    return os.environ.get("GITHUB_BRANCH", "main").strip() or "main"


GITHUB_BRANCH = _resolve_branch()


def _api_url(file_path: str) -> str:
    return f"https://api.github.com/repos/{GITHUB_REPO}/contents/{file_path}"


def _headers(token: str) -> dict:
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
    }


def fetch_catalog_csv_from_github(
    file_path: str, token: str,
) -> Tuple[bytes, Optional[str]]:
    """Fetch the current file from GitHub for `file_path`.

    Returns `(content_bytes, sha)`. A real 404 (file doesn't exist) returns
    `(b"", None)` so the caller can treat it as an empty starting point.

    IMPORTANT — large files: the GitHub *Contents* API omits the ``content``
    field for files larger than 1 MB (it returns ``encoding: "none"`` and an
    empty ``content``). ``data/uploaded_schedules.json`` stores a full CSV per
    saved schedule and crosses 1 MB quickly, so we MUST follow the *blob* API
    (``git_url``) in that case — it returns base64 content for blobs up to
    100 MB. Skipping this is what let a save/delete read "empty", then write an
    empty dict back over every saved schedule (data loss). If the file exists
    (sha present) but we still can't get its bytes, we return `(b"", sha)` so
    callers can refuse to overwrite it rather than clobber it blind.
    """
    r = requests.get(
        _api_url(file_path),
        headers=_headers(token),
        params={"ref": GITHUB_BRANCH},
        timeout=15,
    )
    if r.status_code == 404:
        return b"", None
    r.raise_for_status()
    payload = r.json()
    sha = payload.get("sha")
    content_b64 = payload.get("content", "") or ""
    if content_b64.strip():
        try:
            return base64.b64decode(content_b64), sha
        except Exception:
            pass  # fall through to the blob fetch
    # Contents API returned no inline content → file is >1 MB (or content was
    # unparseable). Follow the blob API, which carries content up to 100 MB.
    git_url = payload.get("git_url")
    if git_url:
        try:
            rb = requests.get(git_url, headers=_headers(token), timeout=30)
            rb.raise_for_status()
            blob_b64 = rb.json().get("content", "") or ""
            if blob_b64.strip():
                return base64.b64decode(blob_b64), sha
        except Exception:
            pass  # leave content empty; caller guards on (b"", sha)
    # File exists (sha) but content unavailable → signal so callers don't
    # overwrite it with empty. (sha is None only on a true 404, handled above.)
    return b"", sha


def latest_catalog_sha(file_path: str, token: str) -> Optional[str]:
    """Lightweight call: return just the latest SHA for `file_path`, or None on 404.

    Used by the stale-data banner so we can compare against the SHA the user
    loaded their view from.
    """
    try:
        r = requests.get(
            _api_url(file_path),
            headers=_headers(token),
            params={"ref": GITHUB_BRANCH},
            timeout=10,
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json().get("sha")
    except requests.RequestException:
        return None


class GitHubConflict(Exception):
    """Raised when the PUT fails because the SHA was stale (HTTP 409 or 422)."""


def save_catalog_to_github(
    csv_text: str,
    file_path: str,
    token: str,
    message: str,
    sha: Optional[str] = None,
) -> dict:
    """Upload a CSV string to GitHub at `file_path`.

    If `sha` is provided, it's used directly (callers that already fetched the
    file can reuse it to avoid a second GET). If `sha` is None, we fetch it now.

    Returns the GitHub API response JSON.
    Raises `GitHubConflict` on 409/422 SHA mismatch so callers can retry.
    Other HTTP errors raise normally.
    """
    if sha is None:
        # Backwards-compatible path: fetch SHA ourselves
        r = requests.get(
            _api_url(file_path),
            headers=_headers(token),
            params={"ref": GITHUB_BRANCH},
            timeout=15,
        )
        if r.status_code == 200:
            sha = r.json().get("sha")
        elif r.status_code != 404:
            r.raise_for_status()

    payload = {
        "message": message,
        "content": base64.b64encode(csv_text.encode("utf-8")).decode("ascii"),
        "branch": GITHUB_BRANCH,
    }
    if sha:
        payload["sha"] = sha

    put_r = requests.put(
        _api_url(file_path),
        headers=_headers(token),
        json=payload,
        timeout=15,
    )
    if put_r.status_code in (409, 422):
        # 409 = SHA conflict; 422 = sometimes returned when SHA missing/wrong
        raise GitHubConflict(
            f"GitHub returned {put_r.status_code} (SHA conflict). "
            f"Body: {put_r.text[:200]}"
        )
    put_r.raise_for_status()
    return put_r.json()
