from __future__ import annotations

from datetime import datetime
from pathlib import Path

from insta_auto.config import ROOT


def append_worklog(
    *,
    job_id: str,
    topic: str,
    tools: list[str],
    result_files: list[str | Path],
    media_ids: list[str] | None = None,
    notes: str = "",
    credits: str = "",
    log_path: Path | None = None,
) -> Path:
    path = log_path or (ROOT / "작업기록.md")
    if not path.exists():
        path.write_text("# 작업 기록\n\n", encoding="utf-8")

    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    files = "\n".join(f"- `{Path(f)}`" for f in result_files) or "- (없음)"
    ids = "\n".join(f"- `{m}`" for m in (media_ids or [])) or "- (미게시)"
    block = f"""
## {ts} — {job_id}

- **주제**: {topic}
- **도구**: {", ".join(tools) if tools else "-"}
- **크레딧**: {credits or "-"}
- **결과 파일**:
{files}
- **게시 media id**:
{ids}
- **메모**: {notes or "-"}

"""
    with path.open("a", encoding="utf-8") as f:
        f.write(block)
    return path
