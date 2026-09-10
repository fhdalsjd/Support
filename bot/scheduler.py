"""
Background polling job for the 24-hour referral-post view requirement.

Runs on a fixed interval (TRACKING_POLL_MINUTES) and, for every
application currently in TRACKING_VIEWS:

  * re-fetches the SAME post (never a different one -- section 8/9)
  * updates current_views
  * if it has become unavailable -> FAILED, notify, stop tracking
  * if the view goal is reached -> VERIFICATION_PASSED -> ADMIN_REVIEW,
    notify, stop tracking
  * if the deadline has passed without reaching the goal -> EXPIRED,
    notify, stop tracking
  * otherwise leaves it in TRACKING_VIEWS for the next poll

The timer itself is never reset by anything in this module -- it is
fixed at submission time (Application.verification_deadline) and only
read here.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram.ext import Application as TelegramApplication

from .config import settings
from .database import get_applications_in_tracking, session_scope
from .models import ApplicationStatus
from .notifications import notify
from .telethon_client import ChannelNotAccessibleError, inspector

logger = logging.getLogger(__name__)


@dataclass
class _BotContext:
    """Minimal stand-in for ContextTypes.DEFAULT_TYPE -- notify() only needs .bot."""

    bot: object


async def poll_tracked_applications(telegram_app: TelegramApplication) -> None:
    context = _BotContext(bot=telegram_app.bot)

    with session_scope() as session:
        tracked = get_applications_in_tracking(session)

    for app_id in [a.id for a in tracked]:
        try:
            await _poll_one(context, app_id)
        except Exception:
            logger.exception("Error polling application %s", app_id)


async def _poll_one(context: _BotContext, application_id: int) -> None:
    # Each application is processed in its own transaction so one failure
    # can't roll back progress on the others.
    from .models import Application  # local import to avoid a cycle at module load

    with session_scope() as session:
        app = session.get(Application, application_id)
        if app is None or app.status != ApplicationStatus.TRACKING_VIEWS:
            return

        try:
            post = await inspector.get_post_snapshot(app.channel_username, app.post_id)
        except ChannelNotAccessibleError:
            app.status = ApplicationStatus.FAILED
            await notify(context, app, "post_unavailable")
            return

        app.current_views = post.views

        deadline = app.verification_deadline
        if deadline and deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)

        if app.current_views >= settings.min_referral_views:
            app.status = ApplicationStatus.ADMIN_REVIEW
            await notify(
                context, app, "views_goal_reached",
                views=app.current_views, required=settings.min_referral_views,
            )
            await notify(context, app, "verification_passed")

            from .handlers.admin import push_admin_review  # local import: avoids a cycle at module load

            await push_admin_review(context, app)
            return

        if deadline and now >= deadline:
            app.status = ApplicationStatus.EXPIRED
            await notify(
                context,
                app,
                "expired",
                views=app.current_views,
                required=settings.min_referral_views,
                hours=settings.verification_hours,
            )
            return
        # else: still within the window, leave in TRACKING_VIEWS for the next poll.


def start_scheduler(telegram_app: TelegramApplication) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        poll_tracked_applications,
        "interval",
        minutes=settings.tracking_poll_minutes,
        args=[telegram_app],
        id="tracking_poll",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    logger.info("Tracking scheduler started (every %s minutes).", settings.tracking_poll_minutes)
    return scheduler
