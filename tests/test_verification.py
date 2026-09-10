import pytest

from bot.telethon_client import PostLinkError, parse_post_url
from bot.verification import score_activity


@pytest.mark.parametrize(
    "url,expected_username,expected_id",
    [
        ("https://t.me/channelname/123", "channelname", 123),
        ("https://t.me/Hfearnmoneyv4bot?start=6649475697".replace("?start=6649475697", "/6649475697"), "Hfearnmoneyv4bot", 6649475697),
        ("http://t.me/my_channel_1/9", "my_channel_1", 9),
    ],
)
def test_parse_post_url_valid(url, expected_username, expected_id):
    parsed = parse_post_url(url)
    assert parsed.channel_username == expected_username
    assert parsed.post_id == expected_id


@pytest.mark.parametrize(
    "url",
    [
        "https://t.me/Hfearnmoneyv4bot?start=6649475697",
        "not a url",
        "https://t.me/channelname",
        "https://example.com/channelname/123",
    ],
)
def test_parse_post_url_invalid(url):
    with pytest.raises(PostLinkError):
        parse_post_url(url)


def test_score_activity_good_channel():
    subs = 5000
    views = [420, 512, 388, 470, 455, 399, 601, 433]
    score, notes = score_activity(subs, views, sum(views) / len(views))
    assert score == "GOOD"


def test_score_activity_flags_identical_views_as_risky():
    subs = 5000
    views = [500, 500, 500, 500, 500, 500]
    score, notes = score_activity(subs, views, 500)
    assert score in ("SUSPICIOUS", "HIGH_RISK")
    assert "variance" in notes


def test_score_activity_flags_view_count_exceeding_subscribers():
    subs = 1000
    views = [980, 990, 970, 985, 960, 995]
    score, notes = score_activity(subs, views, sum(views) / len(views))
    assert score in ("SUSPICIOUS", "HIGH_RISK")


def test_score_activity_no_data_is_suspicious():
    score, notes = score_activity(1000, [], 0)
    assert score == "SUSPICIOUS"
