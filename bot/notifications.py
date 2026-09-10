"""
User-facing notification templates.

All "did we already tell the user this" bookkeeping goes through
Application.has_notified / mark_notified so a stage is never announced
twice, per spec section 15 ("Do not spam users").
"""
from __future__ import annotations

import logging

from telegram.error import TelegramError
from telegram.ext import ContextTypes

from .models import Application

logger = logging.getLogger(__name__)


TEMPLATES = {
    "received": (
        "✅ <b>Application Received</b>\n\n"
        "We're verifying your channel now. This usually takes just a few minutes."
    ),
    "post_check_passed": (
        "✅ <b>Post Verification Passed</b>\n\n"
        "Your channel meets the subscriber and average-view requirements, "
        "and the referral link was found. We're now tracking your post's "
        "views for the next {hours} hours."
    ),
    "referral_missing": (
        "❌ <b>Referral Link Not Found</b>\n\n"
        "We couldn't find our official Mini App referral link "
        "({prefix}...) in that post. Please publish a post containing it "
        "and submit the new link."
    ),
    "subscriber_fail": (
        "❌ <b>Subscriber Requirement Not Met</b>\n\n"
        "Your channel has {count} subscribers. A minimum of {minimum} is required."
    ),
    "average_views_fail": (
        "❌ <b>Average Views Requirement Not Met</b>\n\n"
        "Your channel's average is {avg:.0f} views per post. A minimum of {minimum} is required."
    ),
    "views_goal_reached": (
        "✅ <b>Views Milestone Reached!</b>\n\n"
        "Your referral post has reached {views}/{required} views. "
        "Final verification is complete."
    ),
    "verification_passed": (
        "✅ <b>Verification Passed</b>\n\n"
        "All automatic checks have passed. Your application has been sent "
        "to our admin team for final review."
    ),
    "admin_approved": (
        "🎉 <b>Application Approved!</b>\n\n"
        "Your channel has been approved for free advertising. "
        "You can now receive advertisements through our system."
    ),
    "admin_rejected": (
        "❌ <b>Application Rejected</b>\n\n"
        "Your free advertising application was not approved.\n\n"
        "Reason: {reason}"
    ),
    "expired": (
        "⌛️ <b>Verification Expired</b>\n\n"
        "Your referral post reached {views}/{required} views within "
        "{hours} hours, so the requirement wasn't met in time. Feel free "
        "to publish a new post and apply again."
    ),
    "post_unavailable": (
        "❌ <b>Verification Failed</b>\n\n"
        "The submitted post became unavailable (deleted or inaccessible) "
        "before verification could finish."
    ),
}


async def notify(
    context: ContextTypes.DEFAULT_TYPE,
    application: Application,
    stage: str,
    *,
    force: bool = False,
    **kwargs,
) -> None:
    """Send a templated notification at most once per (application, stage)."""
    if not force and application.has_notified(stage):
        return

    text = TEMPLATES[stage].format(**kwargs)
    try:
        await context.bot.send_message(
            chat_id=application.user_id, text=text, parse_mode="HTML"
        )
    except TelegramError as exc:
        logger.warning("Failed to notify user %s (stage=%s): %s", application.user_id, stage, exc)
    application.mark_notified(stage)
