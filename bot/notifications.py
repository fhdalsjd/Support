"""Professional user-facing notification templates."""
from __future__ import annotations

import logging

from telegram.error import TelegramError
from telegram.ext import ContextTypes

from .keyboards import main_menu_keyboard
from .models import Application

logger = logging.getLogger(__name__)

TEMPLATES = {
    "received": "✅ <b>Application received</b>\n\nThank you. We have received your application and started the verification process. We’ll keep you updated as soon as there is progress.",
    "post_check_passed": "🔎 <b>Verification started</b>\n\nYour channel passed the initial eligibility checks. We’re now monitoring your referral post for the next <b>{hours} hours</b>.",
    "referral_missing": "⚠️ <b>Referral link not found</b>\n\nWe couldn’t find the official Mini App referral link in the submitted post.\n\nPlease publish a post containing a link beginning with <code>{prefix}</code>, then submit the new post link here.",
    "subscriber_fail": "⚠️ <b>Subscriber requirement not met</b>\n\nYour channel currently has <b>{count:,}</b> subscribers. At least <b>{minimum:,}</b> are required.",
    "average_views_fail": "⚠️ <b>Average-view requirement not met</b>\n\nYour channel currently averages <b>{avg:.0f}</b> views per post. At least <b>{minimum:,}</b> average views are required.",
    "views_goal_reached": "🎯 <b>View milestone reached</b>\n\nYour referral post has reached <b>{views:,}/{required:,}</b> views.\n\nThe automatic verification stage is complete and your application is now being prepared for final review.",
    "verification_passed": "✅ <b>Verification complete</b>\n\nAll automatic checks have passed successfully. Your application has been forwarded to the admin team for final approval.",
    "admin_approved": "🎉 <b>Application approved</b>\n\nGood news — your channel has been approved for free advertising.",
    "admin_rejected": "ℹ️ <b>Application update</b>\n\nWe’re unable to approve your free advertising application at this time.\n\n<b>Reason:</b> {reason}",
    "expired": "⌛ <b>Verification window ended</b>\n\nYour referral post reached <b>{views:,}/{required:,}</b> views within the <b>{hours}-hour</b> verification window. The required milestone was not reached in time.",
    "post_unavailable": "⚠️ <b>Verification stopped</b>\n\nThe submitted post is no longer available or accessible, so we couldn’t complete verification.",
}

async def notify(context: ContextTypes.DEFAULT_TYPE, application: Application, stage: str, *, force: bool = False, **kwargs) -> None:
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
        logger.warning("Failed to notify user %s (stage=%s): %s", application.user_id, stage, exc)
    application.mark_notified(stage)
