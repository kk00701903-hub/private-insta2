import json

import pytest
from pydantic import ValidationError

from insta_auto import reel_planner
from insta_auto.reel_planner import Storyboard, normalize_storyboard, plan_offline, plan_reel

from .conftest import SAMPLES

LOOKS = ["natural", "warm_film", "mono"]


def _shot(**kw):
    base = {
        "source": "photo",
        "photo_index": 0,
        "look": "natural",
        "overlay_text": "훅",
        "motion_prompt": "slow push-in",
        "camera": "zoom_in",
        "duration": 4,
    }
    base.update(kw)
    return base


def _board(shots, **kw):
    data = {"topic": "주제", "shots": shots, "caption": "캡션", "hashtags": ["#카페", "카페", "가을 여행"]}
    data.update(kw)
    return data


def test_valid_storyboard_and_hashtag_cleanup():
    sb = Storyboard.model_validate(_board([_shot(), _shot(), _shot()]))
    assert sb.total_duration == 12
    assert sb.hashtags == ["카페", "가을여행"]  # '#' stripped, de-duplicated, no spaces
    analysis = sb.to_analysis()
    assert [s.role for s in analysis.slides] == ["cover", "body", "ending"]


@pytest.mark.parametrize(
    "shots, msg",
    [
        ([_shot(), _shot()], "at least 3"),
        ([_shot()] * 7, "at most 6"),
        ([_shot(duration=2)] * 3, "outside"),  # 6 s total < 7 s
        ([_shot(duration=8)] * 4, "outside"),  # 32 s total > 30 s
        ([_shot(photo_index=None), _shot(), _shot()], "photo_index"),
        ([_shot(source="generate", photo_index=None, image_prompt=""), _shot(), _shot()], "image_prompt"),
        ([_shot(duration=9), _shot(), _shot()], "less than or equal"),
    ],
)
def test_storyboard_schema_rejects(shots, msg):
    with pytest.raises(ValidationError) as exc:
        Storyboard.model_validate(_board(shots))
    assert msg in str(exc.value)


def test_real_place_cannot_use_generated_images():
    shots = [_shot(), _shot(source="generate", photo_index=None, image_prompt="cafe"), _shot()]
    with pytest.raises(ValidationError):
        Storyboard.model_validate(_board(shots, is_real_place=True))


def test_normalize_fixes_model_output():
    raw = _board(
        [
            _shot(photo_index=9, look="unknown", duration=1),
            _shot(source="generate", photo_index=None, image_prompt="latte art", duration=20),
            _shot(camera="spin", text_position="left"),
            _shot(), _shot(), _shot(), _shot(),  # 7 shots -> truncated to 6
        ]
    )
    sb = normalize_storyboard(raw, n_photos=2, looks=LOOKS, allow_generate=False, target_seconds=15)
    assert 3 <= len(sb.shots) <= 6
    assert abs(sb.total_duration - 15) < 0.05
    assert all(s.source == "photo" for s in sb.shots)  # generate not allowed in free mode
    assert all(0 <= s.photo_index < 2 for s in sb.shots)
    assert sb.shots[0].look == "natural"
    assert sb.shots[2].camera in reel_planner.CAMERA_MOVES
    assert sb.shots[2].text_position in {"top", "center", "bottom"}


def test_normalize_pads_short_plans_and_handles_no_photos():
    sb = normalize_storyboard(_board([_shot(photo_index=None)]), n_photos=0, looks=LOOKS, allow_generate=False)
    assert len(sb.shots) == 3
    assert all(s.source == "color" for s in sb.shots)
    assert 7 <= sb.total_duration <= 30

    sb = normalize_storyboard(_board([_shot(photo_index=None)] * 3), n_photos=0, looks=LOOKS, allow_generate=True)
    assert all(s.source == "generate" and s.image_prompt for s in sb.shots)


def test_plan_reel_uses_claude_json(monkeypatch, cfg):
    cfg.claude_api_key = "test-key-not-real"
    seen = {}
    answer = _board([_shot(look="warm_film"), _shot(photo_index=1), _shot(photo_index=2, duration=5)])

    def fake_call(c, system, content):
        seen["model"] = c.claude_model
        seen["n_images"] = sum(1 for b in content if b["type"] == "image")
        seen["prompt"] = content[-1]["text"]
        return "Here you go:\n```json\n" + json.dumps(answer, ensure_ascii=False) + "\n```"

    monkeypatch.setattr(reel_planner, "_call_claude", fake_call)
    sb = plan_reel("가을 카페", SAMPLES, cfg=cfg, looks={k: k for k in LOOKS}, allow_generate=False)
    assert seen["n_images"] == len(SAMPLES)
    assert seen["model"] == cfg.claude_model
    assert "Do NOT use source=generate" in seen["prompt"]
    assert sb.shots[0].look == "warm_film"
    assert sb.total_duration == 13


def test_call_claude_uses_configured_model(monkeypatch, cfg):
    import sys
    import types

    cfg.claude_api_key = "test-key-not-real"
    cfg.claude_model = "claude-test-model"
    captured = {}

    class FakeMessages:
        def create(self, **kw):
            captured.update(kw)
            block = types.SimpleNamespace(type="text", text='{"ok": true}')
            return types.SimpleNamespace(content=[block])

    class FakeClient:
        def __init__(self, api_key):
            captured["api_key"] = api_key
            self.messages = FakeMessages()

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=FakeClient))
    text = reel_planner._call_claude(cfg, "sys", [{"type": "text", "text": "hi"}])
    assert text == '{"ok": true}'
    assert captured["model"] == "claude-test-model"
    assert captured["api_key"] == "test-key-not-real"


def test_plan_offline(cfg):
    sb = plan_offline("첫 줄 주제\n둘째\n셋째", SAMPLES, cfg=cfg, looks=LOOKS, allow_generate=False, target_seconds=10)
    assert len(sb.shots) == 3
    assert sb.topic == "첫 줄 주제"
    assert abs(sb.total_duration - 10) < 0.05
