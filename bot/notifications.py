"""Professional, human-readable user notification templates."""
from __future__ import annotations

import logging

from telegram.error import TelegramError
from telegram.ext import ContextTypes

from .keyboards import main_menu_keyboard
from .models import Application

logger = logging.getLogger(__name__)


TEMPLATES = {
    "received": (
        "✅ <b>Application received</b>\n\n"
        "Thank you. We have received your application and started the verification process. "
        "We’ll keep you updated as soon as there is progress."
    ),
    "post_check_passed": (
        "🔎 <b>Verification started</b>\n\n"
        "Your channel has passed the initial eligibility checks, including subscribers, "
        "average views, and the referral link.\n\n"
        "We’re now monitoring your referral post for the next <b>{hours} hours</b>."
    ),
    "referral_missing": (
        "⚠️ <b>Referral link not found</b>\n\n"
        "We couldn’t find the official Mini App referral link in the submitted post.\n\n"
        "Please publish a post containing a link beginning with <code>{prefix}</code>, "
        "then submit the new post link here."
    ),
    "subscriber_fail": (
        "⚠️ <b>Subscriber requirement not met</b>\n\n"
        "Your channel currently has <b>{count:,}</b> subscribers. "
        "At least <b>{minimum:,}</b> are required.\n\n"
        "Once your channel meets this requirement, you’re welcome to apply again."
    ),
    "average_views_fail": (
        "⚠️ <b>Average-view requirement not met</b>\n\n"
        "Your channel currently averages <b>{avg:.0f}</b> views per post. "
        "At least <b>{minimum:,}</b> average views are required.\n\n"
        "You can apply again once the requirement is met."
    ),
    "views_goal_reached": (
        "🎯 <b>View milestone reached</b>\n\n"
        "Your referral post has reached <b>{views:,}/{required:,}</b> views.\n\n"
        "The automatic verification stage is complete and your application is now being prepared for final review."
    ),
    "verification_passed": (
        "✅ <b>Verification complete</b>\n\n"
        "All automatic checks have passed successfully. Your application has been forwarded to the admin team for final approval.\n\n"
        "No further action is required from you for now."
    ),
    "admin_approved": (
        "🎉 <b>Application approved</b>\n\n"
        "Good news — your channel has been approved for free advertising.\n\n"
        "Your channel is now active in our approved network and can receive advertisements through the system."
    ),
    "admin_rejected": (
        "ℹ️ <b>Application update</b>\n\n"
        "After review, we’re unable to approve your free advertising application at this time.\n\n"
        "<b>Reason:</b> {reason}\n\n"
        "You may address the issue and submit a new application when you’re ready."
    ),
    "expired": (
        "⌛ <b>Verification window ended</b>\n\n"
        "Your referral post reached <b>{views:,}/{required:,}</b> views within the "
        "<b>{hours}-hour</b> verification window.\n\n"
        "The required milestone was not reached in time, so this application has expired. "
        "You’re welcome to publish a new qualifying post and apply again."
    ),
    "post_unavailable": (
        "⚠️ <b>Verification stopped</b>\n\n"
        "The submitted post is no longer available or accessible, so we couldn’t complete verification.\n\n"
        "Please make sure the post remains available and submit a new qualifying post to apply again."
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
    """Send a templated notification at most once per application stage."""
    if not force and application.has_notified(stage):
        return

    text = TEMPLATES[stage].format(**kwargs)
    try:
        await context.bot.send_message(
            chat_id=application.user_id,
            text=text,
            parse_mode="HTML",
            reply_markup=main_menu_keyboard(),
        )
    except TelegramError as exc:
        logger.warning(
            "Failed to notify user %s (stage=%s): %s",
            application.user_id,
            stage,
            exc,
        )
    application.mark_notified(stage)
