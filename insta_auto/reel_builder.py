"""ffmpeg: stills -> motion clips (Ken Burns) and clips -> finished Instagram Reel.

Reels spec targeted: 1080x1920, H.264 High, yuv420p, 30 fps, AAC 48 kHz stereo, <= 90 s,
+faststart. Text overlays (transparent PNGs from photocraft.py) are composited per shot so text
stays sharp and inside the safe zone while the picture moves. The command builders are pure
functions (unit-tested); run_* functions execute them via video.run_ffmpeg.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from insta_auto.video import VideoError, _ffmpeg, probe_media, run_ffmpeg

W, H = 1080, 1920
FPS = 30
MAX_REEL_SECONDS = 90.0
AUDIO_RATE = 48000

MUSIC_EXTS = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".flac"}


def _num(x: float) -> str:
    return f"{x:.3f}".rstrip("0").rstrip(".")


# ------------------------------------------------------------------------------- Ken Burns


def kenburns_filter(camera: str, frames: int, *, size: tuple[int, int] = (W, H), zoom: float = 1.12, fps: int = FPS) -> str:
    w, h = size
    n = max(1, frames - 1)
    t = f"(on/{n})"
    zmax = _num(zoom)
    center_x, center_y = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    if camera == "zoom_out":
        z, x, y = f"{zmax}-({zmax}-1)*{t}", center_x, center_y
    elif camera == "pan_left":
        z, x, y = zmax, f"(iw-iw/zoom)*(1-{t})", center_y
    elif camera == "pan_right":
        z, x, y = zmax, f"(iw-iw/zoom)*{t}", center_y
    elif camera == "pan_up":
        z, x, y = zmax, center_x, f"(ih-ih/zoom)*(1-{t})"
    elif camera == "pan_down":
        z, x, y = zmax, center_x, f"(ih-ih/zoom)*{t}"
    elif camera == "static":
        z, x, y = "1", "0", "0"
    else:  # zoom_in
        z, x, y = f"1+({zmax}-1)*{t}", center_x, center_y
    # Upscale first so sub-pixel zoompan steps don't jitter.
    return (
        f"scale={w * 2}:{h * 2}:flags=lanczos,"
        f"zoompan=z='{z}':x='{x}':y='{y}':d=1:s={w}x{h}:fps={fps},"
        f"setsar=1,format=yuv420p"
    )


def build_kenburns_command(
    still: Path,
    out: Path,
    *,
    duration: float,
    camera: str = "zoom_in",
    size: tuple[int, int] = (W, H),
    fps: int = FPS,
    ff: str | None = None,
) -> list[str]:
    frames = max(1, round(duration * fps))
    return [
        ff or _ffmpeg(),
        "-y",
        "-loop", "1",
        "-framerate", str(fps),
        "-t", _num(frames / fps),
        "-i", str(still),
        "-vf", kenburns_filter(camera, frames, size=size, fps=fps),
        "-frames:v", str(frames),
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-an",
        str(out),
    ]


def make_kenburns_clip(still: Path, out: Path, *, duration: float, camera: str = "zoom_in", size: tuple[int, int] = (W, H)) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(build_kenburns_command(still, out, duration=duration, camera=camera, size=size))
    if not out.exists():
        raise VideoError(f"Ken Burns clip not written: {out}")
    return out


# ------------------------------------------------------------------------------- assembly


@dataclass
class Segment:
    clip: Path  # any size/fps video (Kling clip or Ken Burns clip)
    duration: float  # visible length of this shot in the final reel
    overlay: Path | None = None  # transparent PNG composited on top
    has_audio: bool = False


def segment_lengths(durations: list[float], fade: float) -> list[float]:
    """Each segment except the last is extended by the crossfade so the total stays sum(durations)."""
    return [d + (fade if i < len(durations) - 1 else 0.0) for i, d in enumerate(durations)]


def xfade_offsets(durations: list[float]) -> list[float]:
    """Offset of crossfade k (into clip k+1) = end of shot k in the final timeline."""
    out, acc = [], 0.0
    for d in durations[:-1]:
        acc += d
        out.append(round(acc, 3))
    return out


def build_assembly_command(
    segments: list[Segment],
    out: Path,
    *,
    music: Path | None = None,
    music_volume: float = 0.8,
    duck: bool = True,
    keep_clip_audio: bool = False,
    fade: float = 0.5,
    size: tuple[int, int] = (W, H),
    fps: int = FPS,
    ff: str | None = None,
) -> list[str]:
    if not segments:
        raise VideoError("no segments to assemble")
    w, h = size
    durations = [float(s.duration) for s in segments]
    total = round(sum(durations), 3)
    if total > MAX_REEL_SECONDS:
        raise VideoError(f"Reel is {total:.1f}s; Instagram Reels must be <= {MAX_REEL_SECONDS:.0f}s")
    fade = min(fade, min(durations) / 2) if len(segments) > 1 else 0.0
    lengths = segment_lengths(durations, fade)

    args: list[str] = [ff or _ffmpeg(), "-y"]
    filters: list[str] = []
    idx = 0
    clip_idx: list[int] = []
    for seg, length in zip(segments, lengths):
        args += ["-i", str(seg.clip)]
        clip_idx.append(idx)
        idx += 1
    ov_idx: dict[int, int] = {}
    for i, (seg, length) in enumerate(zip(segments, lengths)):
        if seg.overlay:
            args += ["-loop", "1", "-framerate", str(fps), "-t", _num(length), "-i", str(seg.overlay)]
            ov_idx[i] = idx
            idx += 1

    use_clip_audio = keep_clip_audio and all(s.has_audio for s in segments)
    music_idx = None
    if music:
        args += ["-stream_loop", "-1", "-i", str(music)]
        music_idx = idx
        idx += 1
    silence_idx = None
    if not music and not use_clip_audio:
        args += ["-f", "lavfi", "-t", _num(total), "-i", f"anullsrc=channel_layout=stereo:sample_rate={AUDIO_RATE}"]
        silence_idx = idx
        idx += 1

    # --- video: normalise every clip, overlay its text, then crossfade chain
    for i, length in enumerate(lengths):
        base = (
            f"[{clip_idx[i]}:v]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
            f"setsar=1,fps={fps},tpad=stop_mode=clone:stop_duration={_num(length)},"
            f"trim=duration={_num(length)},setpts=PTS-STARTPTS"
        )
        if i in ov_idx:
            filters.append(base + f"[b{i}]")
            filters.append(f"[{ov_idx[i]}:v]format=rgba,scale={w}:{h},fps={fps},setpts=PTS-STARTPTS[t{i}]")
            # xfade needs a constant frame rate and a common timebase on every input
            filters.append(f"[b{i}][t{i}]overlay=0:0:shortest=1,fps={fps},format=yuv420p,settb=AVTB[v{i}]")
        else:
            filters.append(base + f",fps={fps},format=yuv420p,settb=AVTB[v{i}]")
    if len(segments) == 1:
        vout = "v0"
    else:
        prev = "v0"
        for k, off in enumerate(xfade_offsets(durations)):
            label = f"x{k + 1}"
            filters.append(f"[{prev}][v{k + 1}]xfade=transition=fade:duration={_num(fade)}:offset={_num(off)}[{label}]")
            prev = label
        vout = prev

    # --- audio
    aout = "aout"
    if use_clip_audio:
        parts = [
            f"[{clip_idx[i]}:a]aresample={AUDIO_RATE},aformat=channel_layouts=stereo,atrim=duration={_num(d)},asetpts=PTS-STARTPTS[ca{i}]"
            for i, d in enumerate(durations)
        ]
        filters += parts
        filters.append("".join(f"[ca{i}]" for i in range(len(durations))) + f"concat=n={len(durations)}:v=0:a=1[voice]")
    if music_idx is not None:
        fade_out = max(0.0, total - 1.5)
        filters.append(
            f"[{music_idx}:a]aresample={AUDIO_RATE},aformat=channel_layouts=stereo,atrim=duration={_num(total)},"
            f"asetpts=PTS-STARTPTS,volume={_num(music_volume)},afade=t=in:st=0:d=1,"
            f"afade=t=out:st={_num(fade_out)}:d=1.5[mus]"
        )
        if use_clip_audio:
            if duck:
                filters.append("[voice]asplit=2[vsc][vmix]")
                filters.append("[mus][vsc]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=400[ducked]")
                filters.append(f"[ducked][vmix]amix=inputs=2:duration=first:normalize=0[{aout}]")
            else:
                filters.append(f"[mus][voice]amix=inputs=2:duration=first:normalize=0[{aout}]")
        else:
            filters.append(f"[mus]anull[{aout}]")
    elif use_clip_audio:
        filters.append(f"[voice]anull[{aout}]")
    else:
        filters.append(f"[{silence_idx}:a]atrim=duration={_num(total)}[{aout}]")

    args += [
        "-filter_complex", ";".join(filters),
        "-map", f"[{vout}]",
        "-map", f"[{aout}]",
        "-c:v", "libx264",
        "-profile:v", "high",
        "-level:v", "4.2",
        "-preset", "medium",
        "-crf", "20",
        "-maxrate", "12M",
        "-bufsize", "24M",
        "-pix_fmt", "yuv420p",
        "-r", str(fps),
        "-g", str(fps * 2),
        "-c:a", "aac",
        "-b:a", "192k",
        "-ar", str(AUDIO_RATE),
        "-ac", "2",
        "-t", _num(total),
        "-movflags", "+faststart",
        str(out),
    ]
    return args


def assemble_reel(
    segments: list[Segment],
    out: Path,
    *,
    music: Path | None = None,
    keep_clip_audio: bool = False,
    fade: float = 0.5,
) -> dict:
    """Run the assembly and verify the result against the Reels spec. Returns probe info."""
    out.parent.mkdir(parents=True, exist_ok=True)
    for seg in segments:
        if keep_clip_audio:
            seg.has_audio = probe_media(seg.clip).get("audio") is not None
    cmd = build_assembly_command(segments, out, music=music, keep_clip_audio=keep_clip_audio, fade=fade)
    run_ffmpeg(cmd)
    info = probe_media(out)
    v = info.get("video") or {}
    if (v.get("width"), v.get("height")) != (W, H) or v.get("pix_fmt") != "yuv420p":
        raise VideoError(f"assembled reel does not match spec: {info}")
    if not info.get("audio"):
        raise VideoError("assembled reel has no audio track")
    return info


def list_music(music_dir: Path) -> list[Path]:
    if not music_dir.exists():
        return []
    return sorted(p for p in music_dir.iterdir() if p.is_file() and p.suffix.lower() in MUSIC_EXTS)
