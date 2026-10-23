from insta_auto.caption import (
    CAPTION_LIMIT,
    HASHTAG_LIMIT,
    check_caption,
    count_chars,
    full_caption,
    hashtag_count,
    normalize_hashtags,
)
from insta_auto.post_instagram import _caption


def test_count_includes_spaces_and_newlines():
    assert count_chars("가 나") == 3
    assert count_chars("a  b\nc") == 6
    assert count_chars("") == 0
    assert count_chars(None) == 0


def test_count_korean_and_emoji_as_one_char_each():
    assert count_chars("안녕하세요") == 5
    assert count_chars("커피☕") == 3


def test_normalize_hashtags_from_string_and_list():
    assert normalize_hashtags("#카페, 가을  #카페 #Cafe cafe") == ["카페", "가을", "Cafe"]
    assert normalize_hashtags(["#a", "b", "", "#a"]) == ["a", "b"]
    assert normalize_hashtags(None) == []


def test_full_caption_matches_posted_text():
    text = full_caption("  본문 ", ["a", "#b"])
    assert text == "본문\n\n#a #b"
    assert _caption("  본문 ", ["a", "#b"]) == text


def test_hashtag_count_includes_body_tags_dedup():
    assert hashtag_count("오늘 #카페 #가을", ["카페", "라떼"]) == 3


def test_check_ok_at_exact_limits():
    caption = "x" * CAPTION_LIMIT
    res = check_caption(caption, [])
    assert res["chars"] == CAPTION_LIMIT and res["ok"]
    tags = [f"t{i}" for i in range(HASHTAG_LIMIT)]
    assert check_caption("", tags)["ok"]


def test_check_warns_over_limits():
    res = check_caption("x" * CAPTION_LIMIT, ["tag"])  # + "\n\n#tag" pushes it over
    assert not res["ok"]
    assert res["chars"] == CAPTION_LIMIT + 6
    assert any("2,200" in w for w in res["warnings"])
    res = check_caption("", [f"t{i}" for i in range(HASHTAG_LIMIT + 1)])
    assert not res["ok"] and res["hashtags"] == 31
