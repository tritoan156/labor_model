"""CSV catalog persistence — save machine_clean.csv / acc_clean.csv to GitHub.

Used by the Floor Verification tab so the floor team can update labor times
through the UI and have all users see the new values after redeploy.

Requires a GitHub Personal Access Token in `st.secrets["github_token"]`
(same token used for facility_storage).
"""
from __future__ import annotations

import base64

import requests

GITHUB_REPO = "tritoan156/labor_model"
GITHUB_BRANCH = "main"


def save_catalog_to_github(
    csv_text: str,
    file_path: str,
    token: str,
    message: str,
) -> dict:
    """Upload a CSV string to GitHub at `file_path` (e.g. "data/machine_clean.csv").

    Returns the GitHub API response JSON. Raises on HTTP error.
    """
    api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{file_path}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
    }

    # Fetch current file SHA (required to update)
    sha = None
    r = requests.get(api_url, headers=headers, params={"ref": GITHUB_BRANCH}, timeout=15)
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

    put_r = requests.put(api_url, headers=headers, json=payload, timeout=15)
    put_r.raise_for_status()
    return put_r.json()
