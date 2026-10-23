from pathlib import Path

import pytest

from insta_auto import reel_builder as rb
from insta_auto.video import VideoError, _ffmpeg, probe_media, run_ffmpeg


def _filter(cmd):
    return cmd[cmd.index("-filter_complex") + 1]


def _opt(cmd, flag):
    return cmd[cmd.index(flag) + 1]


def test_kenburns_command():
    cmd = rb.build_kenburns_command(Path("s.png"), Path("c.mp4"), duration=4, camera="pan_left", ff="ffmpeg")
    assert cmd[0] == "ffmpeg"
    assert _opt(cmd, "-frames:v") == "120"
    vf = _opt(cmd, "-vf")
    assert "zoompan=" in vf and "s=1080x1920" in vf and "fps=30" in vf
    assert "(iw-iw/zoom)*(1-(on/119))" in vf
    assert "zoom" in rb.kenburns_filter("zoom_in", 90) and "1+(1.12-1)*(on/89)" in rb.kenburns_filter("zoom_in", 90)


def test_timeline_math():
    assert rb.segment_lengths([3, 4, 5], 0.5) == [3.5, 4.5, 5.0]
    assert rb.xfade_offsets([3, 4, 5]) == [3.0, 7.0]


def test_assembly_command_spec_and_xfade():
    segs = [
        rb.Segment(Path("c1.mp4"), 3, Path("t1.png")),
        rb.Segment(Path("c2.mp4"), 4, None),
        rb.Segment(Path("c3.mp4"), 5, Path("t3.png")),
    ]
    cmd = rb.build_assembly_command(segs, Path("out.mp4"), ff="ffmpeg")
    f = _filter(cmd)
    assert f.count("xfade=transition=fade") == 2
    assert "offset=3:" not in f and "offset=3[" in f and "offset=7[" in f
    assert "overlay=0:0" in f and f.count("overlay=0:0") == 2
    assert "anullsrc" in " ".join(cmd)  # silent AAC track when no music
    assert _opt(cmd, "-profile:v") == "high"
    assert _opt(cmd, "-pix_fmt") == "yuv420p"
    assert _opt(cmd, "-r") == "30"
    assert _opt(cmd, "-ar") == "48000"
    assert _opt(cmd, "-c:a") == "aac"
    assert _opt(cmd, "-movflags") == "+faststart"
    assert cmd[len(cmd) - 1 - cmd[::-1].index("-t") + 1] == "12"  # output duration (last -t)
    assert "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920" in f


def test_assembly_with_music_fades_and_ducking():
    segs = [rb.Segment(Path("a.mp4"), 4, has_audio=True), rb.Segment(Path("b.mp4"), 4, has_audio=True)]
    cmd = rb.build_assembly_command(segs, Path("o.mp4"), music=Path("m.mp3"), keep_clip_audio=True, ff="ffmpeg")
    f = _filter(cmd)
    assert "-stream_loop" in cmd
    assert "afade=t=in:st=0" in f and "afade=t=out:st=6.5:d=1.5" in f
    assert "sidechaincompress" in f and "amix=inputs=2" in f

    cmd = rb.build_assembly_command(segs, Path("o.mp4"), music=Path("m.mp3"), ff="ffmpeg")
    assert "sidechaincompress" not in _filter(cmd)  # clip audio not kept -> plain music bed


def test_assembly_rejects_over_90_seconds():
    segs = [rb.Segment(Path(f"{i}.mp4"), 8) for i in range(12)]  # 96 s
    with pytest.raises(VideoError):
        rb.build_assembly_command(segs, Path("o.mp4"), ff="ffmpeg")


def test_real_ffmpeg_reel(tmp_path):
    """Two stills -> Ken Burns clips -> reel with music; verify the Reels spec."""
    from PIL import Image

    stills = []
    for i, color in enumerate([(200, 80, 60), (40, 90, 200)]):
        p = tmp_path / f"s{i}.png"
        Image.new("RGB", (1080, 1920), color).save(p)
        stills.append(p)
    overlay = tmp_path / "t.png"
    Image.new("RGBA", (1080, 1920), (255, 255, 255, 0)).save(overlay)
    music = tmp_path / "m.wav"
    run_ffmpeg([_ffmpeg(), "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2", str(music)])

    clips = [
        rb.make_kenburns_clip(s, tmp_path / f"c{i}.mp4", duration=2.0 + (0.5 if i == 0 else 0), camera="zoom_in")
        for i, s in enumerate(stills)
    ]
    info = rb.assemble_reel(
        [rb.Segment(clips[0], 2.0, overlay), rb.Segment(clips[1], 2.0)],
        tmp_path / "reel.mp4",
        music=music,
    )
    assert abs(info["duration"] - 4.0) < 0.15
    assert info["video"]["width"] == 1080 and info["video"]["height"] == 1920
    assert info["video"]["fps"] == 30 and "h264" in info["video"]["codec"] and "High" in info["video"]["codec"]
    assert info["audio"]["sample_rate"] == 48000 and "aac" in info["audio"]["codec"]
    head = (tmp_path / "reel.mp4").read_bytes()[:4096]
    assert b"moov" in head  # +faststart: moov atom before mdat
    assert probe_media(tmp_path / "reel.mp4")["video"]["pix_fmt"] == "yuv420p"
