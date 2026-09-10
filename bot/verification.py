"""
Core eligibility logic: subscriber/view thresholds, referral-link
detection, and the heuristic anti-fraud activity score.

This module is intentionally free of Telegram-bot / database concerns so
it can be unit-tested in isolation.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass

from .config import settings
from .telethon_client import (
    ChannelNotAccessibleError,
    ChannelSnapshot,
    PostSnapshot,
    inspector,
    parse_post_url,
)


@dataclass
class EligibilityResult:
    channel: ChannelSnapshot
    post: PostSnapshot
    average_views: float
    activity_score: str
    activity_notes: str
    subscriber_ok: bool
    average_views_ok: bool
    referral_link_ok: bool

    @property
    def passed_automatic_checks(self) -> bool:
        return self.subscriber_ok and self.average_views_ok and self.referral_link_ok


async def run_full_check(post_url: str) -> EligibilityResult:
    """
    Runs every automatic check the spec requires (subscribers, average
    views, referral link, activity score) for a freshly-submitted post
    link. Raises PostLinkError / ChannelNotAccessibleError on bad input.
    """
    parsed = parse_post_url(post_url)

    channel = await inspector.get_channel_snapshot(parsed.channel_username)
    post = await inspector.get_post_snapshot(parsed.channel_username, parsed.post_id)

    recent_views = await inspector.get_recent_view_counts(
        parsed.channel_username, settings.average_views_sample_size
    )
    average_views = statistics.mean(recent_views) if recent_views else 0.0

    activity_score, activity_notes = score_activity(
        subscriber_count=channel.subscriber_count,
        recent_views=recent_views,
        average_views=average_views,
    )

    return EligibilityResult(
        channel=channel,
        post=post,
        average_views=average_views,
        activity_score=activity_score,
        activity_notes=activity_notes,
        subscriber_ok=channel.subscriber_count >= settings.min_subscribers,
        average_views_ok=average_views >= settings.min_average_views,
        referral_link_ok=post.referral_link_found,
    )


def score_activity(
    subscriber_count: int, recent_views: list[int], average_views: float
) -> tuple[str, str]:
    """
    Heuristic-only fraud signal. This NEVER claims to prove views are
    genuine -- it only flags patterns worth a human's attention. Any
    "SUSPICIOUS" or "HIGH_RISK" result routes to manual admin review
    instead of being auto-approved.
    """
    notes: list[str] = []
    risk_points = 0

    if not recent_views or subscriber_count <= 0:
        return "SUSPICIOUS", "Insufficient public data to score activity."

    # 1. Subscriber-to-view ratio: a channel where every post is viewed by
    #    a huge share (or a tiny sliver) of its subscriber base is unusual.
    view_ratio = average_views / subscriber_count
    if view_ratio > 0.95:
        risk_points += 2
        notes.append(f"Average views are {view_ratio:.0%} of subscriber count (unusually high).")
    elif view_ratio < 0.01:
        risk_points += 1
        notes.append(f"Average views are only {view_ratio:.1%} of subscriber count (unusually low).")

    # 2. Consistency: real audiences produce some natural variance between
    #    posts. Views that are nearly identical post-to-post are a
    #    classic bot/view-farm signature.
    if len(recent_views) >= 3:
        stdev = statistics.pstdev(recent_views)
        mean = statistics.mean(recent_views)
        coeff_variation = (stdev / mean) if mean else 0
        if coeff_variation < 0.05:
            risk_points += 2
            notes.append("Recent post views are nearly identical (low natural variance).")

    # 3. Sudden spikes: one viral post skewing the whole sample.
    if len(recent_views) >= 4:
        sorted_views = sorted(recent_views, reverse=True)
        top, rest = sorted_views[0], sorted_views[1:]
        rest_mean = statistics.mean(rest) if rest else 0
        if rest_mean > 0 and top > rest_mean * 5:
            risk_points += 1
            notes.append("One post is a major outlier compared to the rest of the sample.")

    # 4. Posting activity: too few recent posts to trust the sample.
    if len(recent_views) < 5:
        risk_points += 1
        notes.append("Very few recent posts available to sample -- treat average with caution.")

    if risk_points == 0:
        return "GOOD", "No anomalies detected in the sampled public data."
    if risk_points <= 2:
        return "SUSPICIOUS", " ".join(notes)
    return "HIGH_RISK", " ".join(notes)


__all__ = ["EligibilityResult", "run_full_check", "score_activity", "ChannelNotAccessibleError"]
