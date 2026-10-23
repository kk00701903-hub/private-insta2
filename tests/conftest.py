import dataclasses
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from insta_auto.config import load_config  # noqa: E402

SAMPLES = sorted((ROOT / "samples").glob("photo_*.jpg"))


@pytest.fixture
def cfg(tmp_path):
    """Config with output in tmp and no real credentials (never call paid/remote APIs)."""
    return dataclasses.replace(
        load_config(),
        output_dir=tmp_path / "output",
        claude_api_key=None,
        kling_api_key=None,
        kling_secret_key=None,
        ig_user_id=None,
        ig_access_token=None,
    )


@pytest.fixture(autouse=True)
def _no_worklog(monkeypatch):
    """Tests must not append to the real 작업기록.md."""
    from insta_auto import aireels

    calls = []
    monkeypatch.setattr(aireels, "append_worklog", lambda **kw: calls.append(kw))
    return calls
