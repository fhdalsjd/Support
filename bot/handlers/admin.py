"""Admin-only handlers: pending queue, application detail, approve/reject."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from ..config import settings
from ..database import (
    add_approved_channel,
    get_application,
    get_applications_awaiting_admin,
    session_scope,
)
from ..keyboards import admin_pending_list_keyboard, admin_review_keyboard
from ..models import Application, ApplicationStatus
from ..notifications import notify

logger = logging.getLogger(__name__)

WAITING_REJECT_REASON = 10


def admin_only(handler):
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id if update.effective_user else None
        if user_id not in settings.admin_ids:
            if update.callback_query:
                await update.callback_query.answer("Not authorized.", show_alert=True)
            elif update.message:
                await update.message.reply_text("Not authorized.")
            return ConversationHandler.END if handler.__name__ != "admin_panel" else None
        return await handler(update, context)

    wrapped.__name__ = handler.__name__
    return wrapped


def _format_time_remaining(deadline: datetime | None) -> str:
    if deadline is None:
        return "n/a"
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    remaining = deadline - datetime.now(timezone.utc)
    if remaining.total_seconds() <= 0:
        return "0h 0m"
    hours, rem = divmod(int(remaining.total_seconds()), 3600)
    minutes = rem // 60
    return f"{hours}h {minutes}m"


def _application_card(app: Application) -> str:
    eligibility = "PASSED ✅" if app.status in (
        ApplicationStatus.VERIFICATION_PASSED,
        ApplicationStatus.ADMIN_REVIEW,
        ApplicationStatus.APPROVED,
    ) else "PENDING"

    return (
        "🆕 <b>Free Ad Application</b> #{id}\n\n"
        "Channel:\n<b>@{username}</b> ({title})\n\n"
        "Subscribers:\n<b>{subs:,}</b>\n\n"
        "Average views:\n<b>{avg:.0f}</b>\n\n"
        "Referral post:\n{post_url}\n\n"
        "Current views:\n<b>{cur}</b>\n\n"
        "Required views:\n<b>{req}</b>\n\n"
        "Time remaining:\n{remaining}\n\n"
        "Activity score:\n<b>{score}</b>\n{notes}\n\n"
        "Referral link:\nFOUND ✅\n\n"
        "Eligibility:\n{eligibility}\n\n"
        "Applicant:\n<code>{applicant}</code>"
    ).format(
        id=app.id,
        username=app.channel_username,
        title=app.channel_title,
        subs=app.subscriber_count,
        avg=app.average_views,
        post_url=app.post_url,
        cur=app.current_views,
        req=settings.min_referral_views,
        remaining=_format_time_remaining(app.verification_deadline),
        score=app.activity_score.value if hasattr(app.activity_score, "value") else app.activity_score,
        notes=f"<i>{app.activity_notes}</i>" if app.activity_notes else "",
        eligibility=eligibility,
        applicant=app.user_id,
    )


@admin_only
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with session_scope() as session:
        pending = get_applications_awaiting_admin(session)
        if not pending:
            await update.message.reply_text("No applications are awaiting review right now.")
            return
        await update.message.reply_text(
            f"{len(pending)} application(s) awaiting review:",
            reply_markup=admin_pending_list_keyboard([a.id for a in pending]),
        )


@admin_only
async def admin_view_details(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    application_id = int(query.data.split(":")[1])

    with session_scope() as session:
        app = get_application(session, application_id)
        if app is None:
            await query.edit_message_text("Application not found (it may have been removed).")
            return
        text = _application_card(app)

    await query.message.reply_text(
        text, parse_mode="HTML", reply_markup=admin_review_keyboard(application_id)
    )


@admin_only
async def admin_approve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    application_id = int(query.data.split(":")[1])

    with session_scope() as session:
        app = get_application(session, application_id)
        if app is None:
            await query.edit_message_text("Application not found.")
            return
        if app.status != ApplicationStatus.ADMIN_REVIEW:
            await query.answer("This application is no longer awaiting review.", show_alert=True)
            return

        app.status = ApplicationStatus.APPROVED
        app.admin_decision = "APPROVED"
        app.approved_at = datetime.utcnow()
        add_approved_channel(session, app, approved_by=update.effective_user.id)
        session.flush()
        await notify(context, app, "admin_approved")

    await query.edit_message_text(f"✅ Application #{application_id} approved.")


@admin_only
async def admin_reject_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    application_id = int(query.data.split(":")[1])

    with session_scope() as session:
        app = get_application(session, application_id)
        if app is None or app.status != ApplicationStatus.ADMIN_REVIEW:
            await query.answer("This application is no longer awaiting review.", show_alert=True)
            return ConversationHandler.END

    context.user_data["reject_application_id"] = application_id
    await query.message.reply_text(
        "Send an optional rejection reason, or /skip to reject without one."
    )
    return WAITING_REJECT_REASON


async def _finalize_reject(update: Update, context: ContextTypes.DEFAULT_TYPE, reason: str | None) -> int:
    application_id = context.user_data.pop("reject_application_id", None)
    if application_id is None:
        await update.message.reply_text("No rejection in progress.")
        return ConversationHandler.END

    with session_scope() as session:
        app = get_application(session, application_id)
        if app is None:
            await update.message.reply_text("Application not found.")
            return ConversationHandler.END
        if app.status != ApplicationStatus.ADMIN_REVIEW:
            await update.message.reply_text("This application is no longer awaiting review.")
            return ConversationHandler.END

        app.status = ApplicationStatus.REJECTED
        app.admin_decision = "REJECTED"
        app.rejection_reason = reason
        session.flush()
        await notify(context, app, "admin_rejected", reason=reason or "No reason given.")

    await update.message.reply_text(f"❌ Application #{application_id} rejected.")
    return ConversationHandler.END


@admin_only
async def admin_reject_reason(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _finalize_reject(update, context, update.message.text.strip())


@admin_only
async def admin_reject_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _finalize_reject(update, context, None)


def build_reject_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_reject_start, pattern=r"^admin_reject:\d+$")],
        states={
            WAITING_REJECT_REASON: [
                CommandHandler("skip", admin_reject_skip),
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_reject_reason),
            ]
        },
        fallbacks=[CommandHandler("skip", admin_reject_skip)],
        name="admin_reject_conversation",
    )


def register(application) -> None:
    application.add_handler(CommandHandler("admin", admin_panel))
    application.add_handler(CommandHandler("pending", admin_panel))
    application.add_handler(CallbackQueryHandler(admin_view_details, pattern=r"^admin_details:\d+$"))
    application.add_handler(CallbackQueryHandler(admin_approve, pattern=r"^admin_approve:\d+$"))
    application.add_handler(build_reject_conversation())
