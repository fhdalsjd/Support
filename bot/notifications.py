from __future__ import annotations
import logging
from telegram.error import TelegramError
from telegram.ext import ContextTypes
from .models import Application
logger=logging.getLogger(__name__)
TEMPLATES={"received":"✅ <b>Application received</b>\n\nWe're verifying your channel now. This can take a few minutes.","post_check_passed":"✅ <b>Post verification passed</b>\n\nYour channel meets the subscriber and average-view requirements, and the referral link was found. Now tracking your post's views for the next {hours} hours.","referral_missing":"❌ <b>Referral link not found</b>\n\nWe couldn't find the official Mini App referral link ({prefix}...) in that post. Please publish a post containing it and submit the new link.","subscriber_fail":"❌ <b>Subscriber requirement not met</b>\n\nYour channel has {count} subscribers. A minimum of {minimum} is required.","average_views_fail":"❌ <b>Average views requirement not met</b>\n\nYour channel's average is {avg:.0f} views/post. A minimum of {minimum} is required.","views_goal_reached":"✅ <b>100-views milestone reached!</b>\n\nYour referral post has reached {views} views. Final verification is complete.","verification_passed":"✅ <b>Verification passed</b>\n\nAll automatic checks passed. Your application has been sent to our admin team for final review.","admin_approved":"🎉 <b>Application Approved!</b>\n\nYour channel has been approved for free advertising.\nYou can now receive advertisements through our advertising system.","admin_rejected":"❌ <b>Application Rejected</b>\n\nYour free advertising application was not approved.\n\nReason: {reason}","expired":"⌛️ <b>Verification expired</b>\n\nYour referral post reached {views}/{required} views within {hours} hours, so the requirement was not met. Feel free to publish a new post and apply again.","post_unavailable":"❌ <b>Verification failed</b>\n\nThe submitted post became unavailable (deleted or inaccessible) before verification finished."}
async def notify(context:ContextTypes.DEFAULT_TYPE,application:Application,stage:str,*,force:bool=False,**kwargs)->None:
    if not force and application.has_notified(stage): return
    text=TEMPLATES[stage].format(**kwargs)
    try: await context.bot.send_message(chat_id=application.user_id,text=text,parse_mode="HTML")
    except TelegramError as exc: logger.warning("Failed to notify user %s (stage=%s): %s",application.user_id,stage,exc)
    application.mark_notified(stage)
