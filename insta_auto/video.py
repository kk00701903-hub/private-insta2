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
