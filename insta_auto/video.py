from __future__ import annotations

import subprocess
from pathlib import Path


class VideoError(RuntimeError):
    pass


def _ffmpeg() -> str:
    try:
        from imageio_ffmpeg import get_ffmpeg_exe
    except ImportError as exc:
        raise VideoError("imageio-ffmpeg is not installed") from exc
    return get_ffmpeg_exe()


def encode_reel(
    frames: list[Path],
    out_path: Path,
    *,
    seconds: float = 5.0,
    music: Path | None = None,
) -> Path:
    if len(frames) < 1:
        raise VideoError("Reel needs at least 1 frame")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    list_path = out_path.with_suffix(".concat.txt")

    lines: list[str] = []
    for frame in frames:
        posix = Path(frame).resolve().as_posix().replace("'", r"'\''")
        lines.append(f"file '{posix}'")
        lines.append(f"duration {seconds}")
    last = Path(frames[-1]).resolve().as_posix().replace("'", r"'\''")
    lines.append(f"file '{last}'")
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    ff = _ffmpeg()
    # First pass: silent video from image concat
    silent = out_path.with_name(out_path.stem + "_silent.mp4")
    cmd_video = [
        ff,
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_path),
        "-vf",
        "fps=30,format=yuv420p",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(silent),
    ]
    result = subprocess.run(cmd_video, capture_output=True, text=True)
    if result.returncode != 0 or not silent.exists():
        raise VideoError(result.stderr[-2000:] or "ffmpeg video encode failed")

    if music and Path(music).exists():
        cmd_audio = [
            ff,
            "-y",
            "-i",
            str(silent),
            "-i",
            str(music),
            "-filter_complex",
            "[1:a]volume=0.35,afade=t=in:st=0:d=0.5[a]",
            "-map",
            "0:v",
            "-map",
            "[a]",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-shortest",
            "-movflags",
            "+faststart",
            str(out_path),
        ]
        result = subprocess.run(cmd_audio, capture_output=True, text=True)
        silent.unlink(missing_ok=True)
        if result.returncode != 0 or not out_path.exists():
            raise VideoError(result.stderr[-2000:] or "ffmpeg audio mix failed")
    else:
        # Add silent audio track for Instagram compatibility
        cmd_null = [
            ff,
            "-y",
            "-i",
            str(silent),
            "-f",
            "lavfi",
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-shortest",
            "-movflags",
            "+faststart",
            str(out_path),
        ]
        result = subprocess.run(cmd_null, capture_output=True, text=True)
        silent.unlink(missing_ok=True)
        if result.returncode != 0 or not out_path.exists():
            raise VideoError(result.stderr[-2000:] or "ffmpeg silent audio failed")

    return out_path


def run_ffmpeg(cmd: list[str], *, timeout: int = 900) -> subprocess.CompletedProcess[str]:
    """Run an ffmpeg argv (cmd[0] is the binary); raise VideoError with the stderr tail."""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout
        )
    except subprocess.TimeoutExpired as exc:
        raise VideoError(f"ffmpeg timed out after {timeout}s") from exc
    if result.returncode != 0:
        raise VideoError(result.stderr[-2000:] or f"ffmpeg exit {result.returncode}")
    return result


def probe_media(path: Path) -> dict:
    """Parse `ffmpeg -i` output (imageio-ffmpeg ships no ffprobe)."""
    import re

    result = subprocess.run(
        [_ffmpeg(), "-hide_banner", "-i", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    err = result.stderr or ""
    info: dict = {"duration": None, "video": None, "audio": None}
    m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", err)
    if m:
        info["duration"] = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    v = re.search(r"Stream #\S+.*Video: (.+)", err)
    if v:
        line = v.group(1)
        size = re.search(r"(\d{2,5})x(\d{2,5})", line)
        fps = re.search(r"([\d.]+) fps", line)
        info["video"] = {
            "codec": line.split(",")[0].strip(),
            "pix_fmt": "yuv420p" if "yuv420p" in line else None,
            "width": int(size.group(1)) if size else None,
            "height": int(size.group(2)) if size else None,
            "fps": float(fps.group(1)) if fps else None,
        }
    a = re.search(r"Stream #\S+.*Audio: (.+)", err)
    if a:
        line = a.group(1)
        rate = re.search(r"(\d+) Hz", line)
        info["audio"] = {
            "codec": line.split(",")[0].strip(),
            "sample_rate": int(rate.group(1)) if rate else None,
        }
    return info
