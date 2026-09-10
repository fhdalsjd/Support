"""Professional admin control center, application review, support, and broadcast tools."""
from __future__ import annotations

import html
import logging
from datetime import datetime, timezone
from functools import wraps

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes, ConversationHandler, MessageHandler, filters

from ..config import settings
from ..database import add_approved_channel, get_application, get_applications_awaiting_admin, session_scope
from ..keyboards import (
    admin_back_keyboard,
    admin_broadcast_confirm_keyboard,
    admin_control_center_keyboard,
    admin_pending_list_keyboard,
    admin_review_keyboard,
    admin_support_reply_keyboard,
)
from ..models import Application, ApplicationStatus, User
from ..notifications import notify

logger = logging.getLogger(__name__)

WAITING_REJECT_REASON = 10
WAITING_BROADCAST_MESSAGE = 20
WAITING_SUPPORT_REPLY = 30


def admin_only(handler):
    @wraps(handler)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id if update.effective_user else None
        if user_id not in settings.admin_ids:
            if update.callback_query:
                await update.callback_query.answer("This area is restricted to administrators.", show_alert=True)
            elif update.message:
                await update.message.reply_text("This area is restricted to administrators.")
            return ConversationHandler.END
        return await handler(update, context)

    return wrapped


def _format_time_remaining(deadline: datetime | None) -> str:
    if deadline is None:
        return "Not available"
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    remaining = deadline - datetime.now(timezone.utc)
    if remaining.total_seconds() <= 0:
        return "0h 0m"
    hours, rem = divmod(int(remaining.total_seconds()), 3600)
    return f"{hours}h {rem // 60}m"


def _application_card(app: Application) -> str:
    eligibility = "Passed" if app.status in (
        ApplicationStatus.VERIFICATION_PASSED,
        ApplicationStatus.ADMIN_REVIEW,
        ApplicationStatus.APPROVED,
    ) else "Pending"
    score = app.activity_score.value if hasattr(app.activity_score, "value") else app.activity_score
    username = html.escape(app.channel_username or "unknown")
    title = html.escape(app.channel_title or "Untitled channel")
    post_url = html.escape(app.post_url or "Not available")
    notes = f"\n<i>Activity note: {html.escape(app.activity_notes)}</i>" if app.activity_notes else ""
    return (
        "📄 <b>Application Review</b> #{id}\n\n"
        "<b>Channel</b>\n@{username} — {title}\n\n"
        "<b>Audience</b>\n{subs:,} subscribers\n"
        "{avg:.0f} average views per post\n\n"
        "<b>Referral Post</b>\n{post_url}\n\n"
        "<b>Verification</b>\n"
        "Current views: <b>{cur:,}</b> / {req:,}\n"
        "Time remaining: {remaining}\n"
        "Activity score: <b>{score}</b>{notes}\n"
        "Referral link: <b>Found</b>\n"
        "Eligibility: <b>{eligibility}</b>\n\n"
        "<b>Applicant</b>\n<code>{applicant}</code>"
    ).format(
        id=app.id, username=username, title=title, subs=app.subscriber_count,
        avg=app.average_views, post_url=post_url, cur=app.current_views,
        req=settings.min_referral_views, remaining=_format_time_remaining(app.verification_deadline),
        score=score, notes=notes, eligibility=eligibility, applicant=app.user_id,
    )


async def _send_control_center(target, context: ContextTypes.DEFAULT_TYPE) -> None:
    with session_scope() as session:
        pending_count = len(get_applications_awaiting_admin(session))
        user_count = session.query(User).count()
    text = (
        "🛡️ <b>Admin Control Center</b>\n\n"
        "Welcome back. Everything you need to operate the bot is available here.\n\n"
        f"👥 Registered users: <b>{user_count:,}</b>\n"
        f"📥 Awaiting review: <b>{pending_count:,}</b>\n\n"
        "Select an action below."
    )
    await target.reply_text(text, parse_mode="HTML", reply_markup=admin_control_center_keyboard(pending_count))


@admin_only
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_control_center(update.message, context)


@admin_only
async def admin_center(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    with session_scope() as session:
        pending_count = len(get_applications_awaiting_admin(session))
        user_count = session.query(User).count()
    await query.edit_message_text(
        "🛡️ <b>Admin Control Center</b>\n\n"
        "Your administration dashboard is ready.\n\n"
        f"👥 Registered users: <b>{user_count:,}</b>\n"
        f"📥 Awaiting review: <b>{pending_count:,}</b>\n\n"
        "Select an action below.",
        parse_mode="HTML",
        reply_markup=admin_control_center_keyboard(pending_count),
    )


@admin_only
async def admin_pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    with session_scope() as session:
        pending = get_applications_awaiting_admin(session)
        ids = [a.id for a in pending]
    if not pending:
        await query.edit_message_text(
            "📥 <b>Pending Review</b>\n\nYour review queue is clear. There are no applications waiting for a decision.",
            parse_mode="HTML", reply_markup=admin_back_keyboard(),
        )
        return
    await query.edit_message_text(
        f"📥 <b>Pending Review</b>\n\n{len(pending)} application(s) are ready for your decision.\n\nSelect an application to review:",
        parse_mode="HTML", reply_markup=admin_pending_list_keyboard(ids),
    )


@admin_only
async def admin_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    with session_scope() as session:
        total = session.query(Application).count()
        tracking = session.query(Application).filter(Application.status == ApplicationStatus.TRACKING_VIEWS).count()
        review = session.query(Application).filter(Application.status == ApplicationStatus.ADMIN_REVIEW).count()
        approved = session.query(Application).filter(Application.status == ApplicationStatus.APPROVED).count()
        rejected = session.query(Application).filter(Application.status == ApplicationStatus.REJECTED).count()
        expired = session.query(Application).filter(Application.status == ApplicationStatus.EXPIRED).count()
        failed = session.query(Application).filter(Application.status == ApplicationStatus.FAILED).count()
        users = session.query(User).count()
    text = (
        "📊 <b>System Status</b>\n\n"
        f"👥 Registered users: <b>{users:,}</b>\n"
        f"📋 Total applications: <b>{total:,}</b>\n\n"
        f"🔄 Verification in progress: <b>{tracking:,}</b>\n"
        f"📥 Awaiting final review: <b>{review:,}</b>\n"
        f"✅ Approved: <b>{approved:,}</b>\n"
        f"❌ Rejected: <b>{rejected:,}</b>\n"
        f"⌛ Expired: <b>{expired:,}</b>\n"
        f"⚠️ Failed: <b>{failed:,}</b>"
    )
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=admin_back_keyboard())


@admin_only
async def admin_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    text = (
        "⚙️ <b>Bot Settings</b>\n\n"
        "<b>Eligibility</b>\n"
        f"• Minimum subscribers: <b>{settings.min_subscribers:,}</b>\n"
        f"• Minimum average views: <b>{settings.min_average_views:,}</b>\n"
        f"• Referral milestone: <b>{settings.min_referral_views:,} views</b>\n\n"
        "<b>Verification</b>\n"
        f"• Verification window: <b>{settings.verification_hours} hours</b>\n"
        f"• View check interval: <b>{settings.tracking_poll_minutes} minutes</b>\n"
        f"• Average-view sample: <b>{settings.average_views_sample_size} posts</b>\n\n"
        "<b>Application</b>\n"
        f"• Re-application cooldown: <b>{settings.apply_rate_limit_seconds} seconds</b>\n"
        f"• Official bot: <b>@{html.escape(settings.official_bot_username)}</b>"
    )
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=admin_back_keyboard())


@admin_only
async def admin_view_details(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    application_id = int(query.data.split(":")[1])
    with session_scope() as session:
        app = get_application(session, application_id)
        if app is None:
            await query.edit_message_text(
                "⚠️ <b>Application unavailable</b>\n\nThe application could not be found.",
                parse_mode="HTML", reply_markup=admin_back_keyboard(),
            )
            return
        text = _application_card(app)
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=admin_review_keyboard(application_id))


async def push_admin_review(context, app: Application) -> None:
    """Push a completed verification to every configured administrator."""
    text = "🚨 <b>New Application Ready for Review</b>\n\n" + _application_card(app)
    for admin_id in settings.admin_ids:
        try:
            await context.bot.send_message(
                chat_id=admin_id, text=text, parse_mode="HTML",
                reply_markup=admin_review_keyboard(app.id),
            )
        except TelegramError:
            logger.exception("Failed to notify admin %s about application %s", admin_id, app.id)


@admin_only
async def admin_approve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    application_id = int(query.data.split(":")[1])
    with session_scope() as session:
        app = get_application(session, application_id)
        if app is None:
            await query.edit_message_text(
                "⚠️ <b>Application unavailable</b>\n\nThe application could not be found.",
                parse_mode="HTML", reply_markup=admin_back_keyboard(),
            )
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
    await query.edit_message_text(
        f"✅ <b>Application #{application_id} approved</b>\n\n"
        "The applicant has been notified and the channel is now active in the approved network.",
        parse_mode="HTML", reply_markup=admin_back_keyboard(),
    )


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
        f"✍️ <b>Reject Application #{application_id}</b>\n\n"
        "Please send a concise reason that can be shared with the applicant.\n\n"
        "Use /skip if you do not want to provide a reason, or /cancel to stop.",
        parse_mode="HTML",
    )
    return WAITING_REJECT_REASON


async def _finalize_reject(update: Update, context: ContextTypes.DEFAULT_TYPE, reason: str | None) -> int:
    application_id = context.user_data.pop("reject_application_id", None)
    if application_id is None:
        await update.message.reply_text("There is no rejection currently in progress.")
        return ConversationHandler.END
    with session_scope() as session:
        app = get_application(session, application_id)
        if app is None:
            await update.message.reply_text("⚠️ The application could not be found.")
            return ConversationHandler.END
        if app.status != ApplicationStatus.ADMIN_REVIEW:
            await update.message.reply_text("This application is no longer awaiting review.")
            return ConversationHandler.END
        app.status = ApplicationStatus.REJECTED
        app.admin_decision = "REJECTED"
        app.rejection_reason = reason
        session.flush()
        await notify(context, app, "admin_rejected", reason=reason or "No reason was provided.")
    await update.message.reply_text(
        f"❌ <b>Application #{application_id} rejected</b>\n\nThe applicant has been notified.",
        parse_mode="HTML", reply_markup=admin_control_center_keyboard(),
    )
    return ConversationHandler.END


@admin_only
async def admin_reject_reason(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _finalize_reject(update, context, update.message.text.strip())


@admin_only
async def admin_reject_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _finalize_reject(update, context, None)


@admin_only
async def admin_broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.message.reply_text(
        "📣 <b>Broadcast Center</b>\n\n"
        "Send the announcement you want delivered to registered users.\n\n"
        "You will receive a preview before anything is sent.\n\n"
        "Use /cancel to leave the broadcast center.",
        parse_mode="HTML",
    )
    return WAITING_BROADCAST_MESSAGE


@admin_only
async def admin_broadcast_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not text:
        await update.message.reply_text("Please send a non-empty announcement.")
        return WAITING_BROADCAST_MESSAGE
    context.user_data["broadcast_text"] = text
    with session_scope() as session:
        if settings.admin_ids:
            recipients = session.query(User).filter(~User.telegram_id.in_(settings.admin_ids)).count()
        else:
            recipients = session.query(User).count()
    await update.message.reply_text(
        "📣 <b>Broadcast Preview</b>\n\n"
        f"Recipients: <b>{recipients:,}</b>\n\n"
        "<b>Message</b>\n"
        f"{html.escape(text)}\n\n"
        "If this looks correct, confirm the broadcast below.",
        parse_mode="HTML", reply_markup=admin_broadcast_confirm_keyboard(),
    )
    return WAITING_BROADCAST_MESSAGE


@admin_only
async def admin_broadcast_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    text = context.user_data.pop("broadcast_text", None)
    if not text:
        await query.edit_message_text(
            "⚠️ <b>Broadcast draft expired</b>\n\nPlease start a new broadcast.",
            parse_mode="HTML", reply_markup=admin_back_keyboard(),
        )
        return ConversationHandler.END
    with session_scope() as session:
        recipients = [u.telegram_id for u in session.query(User).all() if u.telegram_id not in settings.admin_ids]
    sent = failed = 0
    for user_id in recipients:
        try:
            await context.bot.send_message(chat_id=user_id, text=text)
            sent += 1
        except TelegramError:
            failed += 1
            logger.warning("Broadcast failed for user %s", user_id)
    await query.edit_message_text(
        "📣 <b>Broadcast Complete</b>\n\n"
        f"Successfully delivered: <b>{sent:,}</b>\n"
        f"Could not deliver: <b>{failed:,}</b>\n\nThe broadcast process has finished.",
        parse_mode="HTML", reply_markup=admin_back_keyboard(),
    )
    return ConversationHandler.END


@admin_only
async def admin_broadcast_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data.pop("broadcast_text", None)
    await query.edit_message_text(
        "📣 <b>Broadcast cancelled</b>\n\nNo messages were sent.",
        parse_mode="HTML", reply_markup=admin_back_keyboard(),
    )
    return ConversationHandler.END


@admin_only
async def admin_support_reply_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = int(query.data.split(":")[1])
    context.user_data["support_reply_user_id"] = user_id
    await query.message.reply_text(
        "💬 <b>Support Reply</b>\n\n"
        f"You are replying to user <code>{user_id}</code>.\n\n"
        "Send the message you want the user to receive.\n"
        "Use /cancel to stop without sending anything.",
        parse_mode="HTML",
    )
    return WAITING_SUPPORT_REPLY


@admin_only
async def admin_support_reply_send(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = context.user_data.pop("support_reply_user_id", None)
    if user_id is None:
        await update.message.reply_text("⚠️ No support reply is currently active.")
        return ConversationHandler.END
    text = update.message.text.strip()
    if not text:
        context.user_data["support_reply_user_id"] = user_id
        await update.message.reply_text("Please send a non-empty reply.")
        return WAITING_SUPPORT_REPLY
    reply_text = (
        "💬 <b>Support Team</b>\n\n"
        f"{html.escape(text)}\n\n"
        "If you need any further assistance, you can contact us again from the main menu."
    )
    try:
        await context.bot.send_message(chat_id=user_id, text=reply_text, parse_mode="HTML")
    except TelegramError:
        logger.exception("Failed to send support reply to user %s", user_id)
        await update.message.reply_text(
            "⚠️ <b>Reply not delivered</b>\n\n"
            "Telegram could not deliver the message. The user may have blocked the bot or stopped the conversation.",
            parse_mode="HTML", reply_markup=admin_control_center_keyboard(),
        )
        return ConversationHandler.END
    await update.message.reply_text(
        f"✅ <b>Reply sent</b>\n\nYour response was delivered to user <code>{user_id}</code>.",
        parse_mode="HTML", reply_markup=admin_control_center_keyboard(),
    )
    return ConversationHandler.END


async def cancel_admin_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("reject_application_id", None)
    context.user_data.pop("broadcast_text", None)
    context.user_data.pop("support_reply_user_id", None)
    await update.message.reply_text(
        "Action cancelled. Nothing was changed or sent.",
        reply_markup=admin_control_center_keyboard(),
    )
    return ConversationHandler.END


def build_reject_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_reject_start, pattern=r"^admin_reject:\d+$")],
        states={WAITING_REJECT_REASON: [
            CommandHandler("skip", admin_reject_skip),
            CommandHandler("cancel", cancel_admin_action),
            MessageHandler(filters.TEXT & ~filters.COMMAND, admin_reject_reason),
        ]},
        fallbacks=[CommandHandler("skip", admin_reject_skip), CommandHandler("cancel", cancel_admin_action)],
        name="admin_reject_conversation",
    )


def build_broadcast_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_broadcast_start, pattern=r"^admin_broadcast$")],
        states={WAITING_BROADCAST_MESSAGE: [
            CallbackQueryHandler(admin_broadcast_confirm, pattern=r"^admin_broadcast_confirm$"),
            CallbackQueryHandler(admin_broadcast_cancel, pattern=r"^admin_broadcast_cancel$"),
            CommandHandler("cancel", cancel_admin_action),
            MessageHandler(filters.TEXT & ~filters.COMMAND, admin_broadcast_message),
        ]},
        fallbacks=[CommandHandler("cancel", cancel_admin_action)],
        name="admin_broadcast_conversation",
    )


def build_support_reply_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_support_reply_start, pattern=r"^admin_support_reply:\d+$")],
        states={WAITING_SUPPORT_REPLY: [
            CommandHandler("cancel", cancel_admin_action),
            MessageHandler(filters.TEXT & ~filters.COMMAND, admin_support_reply_send),
        ]},
        fallbacks=[CommandHandler("cancel", cancel_admin_action)],
        name="admin_support_reply_conversation",
    )


def register(application) -> None:
    application.add_handler(CommandHandler("admin", admin_panel))
    application.add_handler(CommandHandler("pending", admin_panel))
    application.add_handler(build_broadcast_conversation())
    application.add_handler(build_reject_conversation())
    application.add_handler(build_support_reply_conversation())
    application.add_handler(CallbackQueryHandler(admin_center, pattern=r"^admin_center$"))
    application.add_handler(CallbackQueryHandler(admin_pending, pattern=r"^admin_pending$"))
    application.add_handler(CallbackQueryHandler(admin_status, pattern=r"^admin_status$"))
    application.add_handler(CallbackQueryHandler(admin_settings, pattern=r"^admin_settings$"))
    application.add_handler(CallbackQueryHandler(admin_view_details, pattern=r"^admin_details:\d+$"))
    application.add_handler(CallbackQueryHandler(admin_approve, pattern=r"^admin_approve:\d+$"))
