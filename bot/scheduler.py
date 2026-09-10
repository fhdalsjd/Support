from __future__ import annotations
import logging
from dataclasses import dataclass
from datetime import datetime,timezone
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram.ext import Application as TelegramApplication
from .config import settings
from .database import get_applications_in_tracking,session_scope
from .models import Application,ApplicationStatus
from .notifications import notify
from .telethon_client import ChannelNotAccessibleError,inspector
logger=logging.getLogger(__name__)
@dataclass
class _BotContext: bot:object
async def poll_tracked_applications(telegram_app:TelegramApplication)->None:
    context=_BotContext(telegram_app.bot)
    with session_scope() as session: tracked=get_applications_in_tracking(session)
    for aid in [a.id for a in tracked]:
        try: await _poll_one(context,aid)
        except Exception: logger.exception("Error polling application %s",aid)
async def _poll_one(context,application_id):
    with session_scope() as session:
        app=session.get(Application,application_id)
        if app is None or app.status!=ApplicationStatus.TRACKING_VIEWS:return
        try: post=await inspector.get_post_snapshot(app.channel_username,app.post_id)
        except ChannelNotAccessibleError: app.status=ApplicationStatus.FAILED; await notify(context,app,"post_unavailable"); return
        app.current_views=post.views; deadline=app.verification_deadline
        if deadline and deadline.tzinfo is None: deadline=deadline.replace(tzinfo=timezone.utc)
        now=datetime.now(timezone.utc)
        if app.current_views>=settings.min_referral_views:
            app.status=ApplicationStatus.ADMIN_REVIEW; await notify(context,app,"views_goal_reached",views=app.current_views); await notify(context,app,"verification_passed"); return
        if deadline and now>=deadline:
            app.status=ApplicationStatus.EXPIRED; await notify(context,app,"expired",views=app.current_views,required=settings.min_referral_views,hours=settings.verification_hours); return
def start_scheduler(telegram_app):
    scheduler=AsyncIOScheduler(); scheduler.add_job(poll_tracked_applications,"interval",minutes=settings.tracking_poll_minutes,args=[telegram_app],id="tracking_poll",max_instances=1,coalesce=True); scheduler.start(); logger.info("Tracking scheduler started (every %s minutes).",settings.tracking_poll_minutes); return scheduler
