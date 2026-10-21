"""Keep app.py on Streamlit's current sizing API.

``use_container_width=`` is deprecated (Streamlit logs "will be removed after
2025-12-31 ... use width='stretch'" on every run) and ``requirements.txt`` has
no upper cap, so the first Cloud release that drops the kwarg would break the
app. The 90 call sites were migrated to ``width="stretch"`` in one pass; this
stops the old kwarg coming back and keeps the Streamlit floor at a version
whose elements all accept ``width`` (plotly_chart was the last, in 1.51).
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_app_does_not_use_the_deprecated_use_container_width_kwarg():
    hits = [
        f"app.py:{i}"
        for i, line in enumerate(
            (ROOT / "app.py").read_text(encoding="utf-8").splitlines(), 1)
        if "use_container_width" in line
    ]
    assert not hits, (
        'use_container_width= is deprecated -- use width="stretch" (or '
        f'width="content") instead: {hits}'
    )


def test_streamlit_floor_covers_the_width_parameter():
    req = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    m = re.search(r"^streamlit\s*>=\s*(\d+)\.(\d+)", req, re.MULTILINE)
    assert m, "requirements.txt has no `streamlit>=` floor"
    assert (int(m.group(1)), int(m.group(2))) >= (1, 51), (
        'width="stretch" on st.plotly_chart needs Streamlit 1.51+ '
        "(buttons 1.48, dataframe/data_editor/graphviz 1.49)"
    )
