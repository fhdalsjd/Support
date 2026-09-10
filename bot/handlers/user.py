"""User-facing handlers: main menu, the apply conversation, and status lookup."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

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
    get_active_application_for_channel,
    get_active_application_for_post,
    get_or_create_user,
    get_user_active_application,
    is_channel_already_approved,
    session_scope,
)
from ..keyboards import APPLY_ENTRY, MAIN_MENU
from ..models import ActivityScore, Application, ApplicationStatus
from ..notifications import notify
from ..telethon_client import ChannelNotAccessibleError, PostLinkError
from ..verification import run_full_check

logger = logging.getLogger(__name__)

WAITING_POST_LINK = 1
WAITING_SUPPORT_MESSAGE = 2

REQUIREMENTS_TEXT = (
    "ℹ️ <b>Eligibility Requirements</b>\n\n"
    "To qualify for free advertising, your channel needs to meet all of "
    "the following:\n\n"
    f"• {settings.min_subscribers:,}+ subscribers\n"
    f"• {settings.min_average_views}+ average views per post\n"
    "• A post featuring our official Mini App referral link\n"
    f"• That same post must reach {settings.min_referral_views}+ views "
    f"within {settings.verification_hours} hours\n\n"
    "Once you're ready, send us your channel post link "
    "(e.g. https://t.me/channelname/123)."
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with session_scope() as session:
        get_or_create_user(
            session,
            update.effective_user.id,
            update.effective_user.username,
            update.effective_user.first_name,
        )
    await update.message.reply_text(
        "🎁 <b>Free Advertisement</b>\n\n"
        "Get your channel featured for free by promoting our Mini App. "
        "Once your channel passes verification and is approved, you're all set.",
        parse_mode="HTML",
        reply_markup=MAIN_MENU,
    )
    await update.message.reply_text(
        "Tap below whenever you're ready to apply.", reply_markup=APPLY_ENTRY
    )


async def show_requirements(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(REQUIREMENTS_TEXT, parse_mode="HTML")


async def support_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "📞 <b>Support</b>\n\n"
        "Please let us know what you need help with, and a member of our "
        "team will get back to you here shortly.",
        parse_mode="HTML",
    )
    return WAITING_SUPPORT_MESSAGE


async def receive_support_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    message_text = update.message.text.strip()

    if not settings.admin_ids:
        await update.message.reply_text(
            "Support is temporarily unavailable. Please try again later."
        )
        return ConversationHandler.END

    handle = f"@{user.username}" if user.username else "(no username set)"
    forward_text = (
        "📩 <b>New Support Message</b>\n\n"
        f"From: {handle}\n"
        f"Telegram ID: <code>{user.id}</code>\n\n"
        f"{message_text}"
    )
    for admin_id in settings.admin_ids:
        try:
            await context.bot.send_message(
                admin_id,
                forward_text,
                parse_mode="HTML",
            )
        except Exception:
            logger.exception("Failed to forward support message to admin %s", admin_id)

    await update.message.reply_text(
        "✅ Thank you — your message has been forwarded to our support team. "
        "We'll get back to you here shortly."
    )
    return ConversationHandler.END


def _format_time_remaining(deadline: datetime) -> str:
    remaining = deadline - datetime.now(timezone.utc)
    if remaining.total_seconds() <= 0:
        return "0h 0m"
    hours, rem = divmod(int(remaining.total_seconds()), 3600)
    minutes = rem // 60
    return f"{hours}h {minutes}m"


STATUS_LABELS = {
    ApplicationStatus.PENDING_POST_CHECK: "Checking your post…",
    ApplicationStatus.TRACKING_VIEWS: "Tracking views",
    ApplicationStatus.VERIFICATION_PASSED: "Verification passed",
    ApplicationStatus.ADMIN_REVIEW: "Awaiting admin review",
    ApplicationStatus.APPROVED: "Approved ✅",
    ApplicationStatus.REJECTED: "Rejected ❌",
    ApplicationStatus.EXPIRED: "Expired ⌛️",
    ApplicationStatus.FAILED: "Failed ❌",
}


def _status_text(application: Application) -> str:
    lines = [
        "📋 <b>Your Application</b>",
        "",
        f"Channel: @{application.channel_username}",
        f"Status: <b>{STATUS_LABELS.get(application.status, application.status.value)}</b>",
    ]
    if application.status == ApplicationStatus.TRACKING_VIEWS and application.verification_deadline:
        deadline = application.verification_deadline
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        lines.append(f"Current views: {application.current_views}/{settings.min_referral_views}")
        lines.append(f"Time remaining: {_format_time_remaining(deadline)}")
    if application.status == ApplicationStatus.REJECTED and application.rejection_reason:
        lines.append(f"Reason: {application.rejection_reason}")
    return "\n".join(lines)


async def my_application(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with session_scope() as session:
        application = get_user_active_application(session, update.effective_user.id)
        if application is None:
            await update.message.reply_text(
                "You don't currently have an active application. "
                "Tap 📢 Free Advertisement whenever you're ready to apply."
            )
            return
        await update.message.reply_text(_status_text(application), parse_mode="HTML")


async def apply_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await query.answer()
        send = query.message.reply_text
    else:
        send = update.message.reply_text

    with session_scope() as session:
        existing = get_user_active_application(session, update.effective_user.id)
        if existing is not None:
            await send(
                "It looks like you already have an application in progress:\n\n"
                + _status_text(existing),
                parse_mode="HTML",
            )
            return ConversationHandler.END

    await send(REQUIREMENTS_TEXT, parse_mode="HTML")
    return WAITING_POST_LINK


async def receive_post_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    post_url = update.message.text.strip()

    with session_scope() as session:
        db_user = get_or_create_user(session, user.id, user.username, user.first_name)

        if db_user.last_applied_at:
            elapsed = (datetime.utcnow() - db_user.last_applied_at).total_seconds()
            if elapsed < settings.apply_rate_limit_seconds:
                wait = int(settings.apply_rate_limit_seconds - elapsed)
                await update.message.reply_text(f"Please wait {wait} seconds before submitting again.")
                return WAITING_POST_LINK

        user_active = get_user_active_application(session, user.id)
        if user_active is not None:
            if user_active.post_url == post_url:
                await update.message.reply_text(
                    "This post is already under verification. Resubmitting it "
                    "does not reset the tracking timer.\n\n" + _status_text(user_active),
                    parse_mode="HTML",
                )
                return ConversationHandler.END
            await update.message.reply_text(
                "You currently have an active application in progress. "
                "Please wait for it to finish before submitting a different post.\n\n"
                + _status_text(user_active),
                parse_mode="HTML",
            )
            return ConversationHandler.END

        other_active = get_active_application_for_post(session, post_url)
        if other_active is not None:
            await update.message.reply_text(
                "❌ This post is already being verified as part of another application."
            )
            return ConversationHandler.END

    try:
        result = await run_full_check(post_url)
    except PostLinkError as exc:
        await update.message.reply_text(f"❌ {exc}")
        return WAITING_POST_LINK
    except ChannelNotAccessibleError as exc:
        await update.message.reply_text(f"❌ {exc}")
        return WAITING_POST_LINK
    except Exception:
        logger.exception("Unexpected error verifying %s", post_url)
        await update.message.reply_text(
            "❌ Something went wrong while checking that post. Please try again in a few minutes."
        )
        return WAITING_POST_LINK

    with session_scope() as session:
        if is_channel_already_approved(session, result.channel.channel_id):
            await update.message.reply_text(
                "ℹ️ This channel is already approved for free advertising — no need to apply again."
            )
            return ConversationHandler.END

        channel_active = get_active_application_for_channel(session, result.channel.channel_id)
        if channel_active is not None and channel_active.post_url != post_url:
            await update.message.reply_text(
                "❌ This channel already has another active application in progress."
            )
            return ConversationHandler.END

        if not result.subscriber_ok:
            await update.message.reply_text(
                "❌ <b>Subscriber Requirement Not Met</b>\n\n"
                f"Your channel has {result.channel.subscriber_count:,} subscribers. "
                f"A minimum of {settings.min_subscribers:,} is required.",
                parse_mode="HTML",
            )
            return ConversationHandler.END

        if not result.average_views_ok:
            await update.message.reply_text(
                "❌ <b>Average Views Requirement Not Met</b>\n\n"
                f"Your channel's average is {result.average_views:.0f} views per post. "
                f"A minimum of {settings.min_average_views} is required.",
                parse_mode="HTML",
            )
            return ConversationHandler.END

        if not result.referral_link_ok:
            await update.message.reply_text(
                "❌ <b>Referral Link Not Found</b>\n\n"
                "We couldn't find our official Mini App referral link in that post. "
                f"Please publish a post containing a link in the form "
                f"{settings.referral_url_prefix}&lt;id&gt; and submit it here.",
                parse_mode="HTML",
            )
            return ConversationHandler.END

        deadline = datetime.utcnow() + timedelta(hours=settings.verification_hours)
        application = Application(
            user_id=user.id,
            channel_id=result.channel.channel_id,
            channel_username=result.channel.username,
            channel_title=result.channel.title,
            post_id=result.post.post_id,
            post_url=post_url,
            initial_views=result.post.views,
            current_views=result.post.views,
            subscriber_count=result.channel.subscriber_count,
            average_views=result.average_views,
            activity_score=ActivityScore(result.activity_score),
            activity_notes=result.activity_notes,
            referral_link_found=True,
            status=ApplicationStatus.TRACKING_VIEWS,
            verification_deadline=deadline,
        )
        session.add(application)
        db_user = get_or_create_user(session, user.id, user.username, user.first_name)
        db_user.last_applied_at = datetime.utcnow()
        session.flush()

        await notify(context, application, "received")
        await notify(
            context,
            application,
            "post_check_passed",
            hours=settings.verification_hours,
        )

    await update.message.reply_text(
        "✅ <b>Eligible So Far!</b>\n\n"
        f"Subscribers: {result.channel.subscriber_count:,}\n"
        f"Average views: {result.average_views:.0f}\n"
        f"Referral link: Found ✅\n\n"
        f"We're now tracking this post — it needs {settings.min_referral_views}+ views "
        f"within {settings.verification_hours} hours to complete verification. "
        "You can check progress anytime under 📋 My Application.",
        parse_mode="HTML",
    )
    return ConversationHandler.END


async def cancel_apply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Cancelled — you can apply again anytime from the main menu.")
    return ConversationHandler.END


def build_apply_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CommandHandler("apply", apply_entry),
            MessageHandler(filters.Regex("^📢 Free Advertisement$"), apply_entry),
            CallbackQueryHandler(apply_entry, pattern="^apply_start$"),
        ],
        states={
            WAITING_POST_LINK: [
                CommandHandler("cancel", cancel_apply),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_post_link),
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel_apply)],
        name="apply_conversation",
    )


def build_support_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CommandHandler("support", support_entry),
            MessageHandler(filters.Regex("^📞 Support$"), support_entry),
        ],
        states={
            WAITING_SUPPORT_MESSAGE: [
                CommandHandler("cancel", cancel_apply),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_support_message),
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel_apply)],
        name="support_conversation",
    )


def register(application) -> None:
    application.add_handler(CommandHandler("start", start))
    application.add_handler(build_apply_conversation())
    application.add_handler(build_support_conversation())
    application.add_handler(MessageHandler(filters.Regex("^📋 My Application$"), my_application))
    application.add_handler(CommandHandler("myapplication", my_application))
    application.add_handler(MessageHandler(filters.Regex("^ℹ️ Requirements$"), show_requirements))
