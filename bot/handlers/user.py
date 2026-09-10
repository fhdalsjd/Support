"""Professional user-facing bot flow with inline navigation and clear responses."""
from __future__ import annotations

import html
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
from ..keyboards import (
    APPLY_ENTRY,
    MAIN_MENU,
    user_back_keyboard,
    user_cancel_keyboard,
)
from ..models import ActivityScore, Application, ApplicationStatus
from ..notifications import notify
from ..telethon_client import ChannelNotAccessibleError, PostLinkError
from ..verification import run_full_check

logger = logging.getLogger(__name__)

WAITING_POST_LINK = 1
WAITING_SUPPORT_MESSAGE = 2

REQUIREMENTS_TEXT = (
    "ℹ️ <b>Eligibility Requirements</b>\n\n"
    "Before submitting an application, please make sure your channel meets "
    "all of the requirements below:\n\n"
    f"• <b>{settings.min_subscribers:,}+</b> subscribers\n"
    f"• <b>{settings.min_average_views:,}+</b> average views per post\n"
    "• A public post containing our official Mini App referral link\n"
    f"• The referral post must reach <b>{settings.min_referral_views:,}+</b> views "
    f"within <b>{settings.verification_hours} hours</b>\n\n"
    "If everything is ready, submit the link to the qualifying channel post."
)

WELCOME_TEXT = (
    "👋 <b>Welcome</b>\n\n"
    "You’re in the right place to apply for free channel advertising.\n\n"
    "Our verification system checks your channel eligibility and monitors the "
    "submitted referral post automatically. If all requirements are met, your "
    "application is sent to our team for final approval.\n\n"
    "Choose an option below to continue."
)


def _menu_text() -> str:
    return (
        "🏠 <b>Main Menu</b>\n\n"
        "Choose what you’d like to do. Your application status and support are "
        "available here at any time."
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
        WELCOME_TEXT,
        parse_mode="HTML",
        reply_markup=MAIN_MENU,
    )


async def home_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(_menu_text(), parse_mode="HTML", reply_markup=MAIN_MENU)


async def show_requirements(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        await query.edit_message_text(
            REQUIREMENTS_TEXT,
            parse_mode="HTML",
            reply_markup=user_back_keyboard(),
        )
    else:
        await update.message.reply_text(
            REQUIREMENTS_TEXT,
            parse_mode="HTML",
            reply_markup=user_back_keyboard(),
        )


async def support_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (
        "💬 <b>Contact Support</b>\n\n"
        "Need help with an application or have a question? Send your message "
        "below and our support team will review it.\n\n"
        "Please include enough detail for us to understand the issue quickly."
    )
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        await query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=user_cancel_keyboard(),
        )
    else:
        await update.message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=user_cancel_keyboard(),
        )
    return WAITING_SUPPORT_MESSAGE


async def receive_support_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    message_text = update.message.text.strip()

    if not message_text:
        await update.message.reply_text(
            "Please send a message with a little more detail so our team can help you."
        )
        return WAITING_SUPPORT_MESSAGE

    if not settings.admin_ids:
        await update.message.reply_text(
            "⚠️ <b>Support temporarily unavailable</b>\n\n"
            "Our support channel is not available right now. Please try again later.",
            parse_mode="HTML",
            reply_markup=MAIN_MENU,
        )
        return ConversationHandler.END

    handle = f"@{user.username}" if user.username else "No username"
    forward_text = (
        "📩 <b>New Support Request</b>\n\n"
        f"<b>User:</b> {html.escape(handle)}\n"
        f"<b>Telegram ID:</b> <code>{user.id}</code>\n\n"
        "<b>Message</b>\n"
        f"{html.escape(message_text)}"
    )

    delivered = 0
    for admin_id in settings.admin_ids:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=forward_text,
                parse_mode="HTML",
                reply_markup=__import__("bot.keyboards", fromlist=["admin_support_reply_keyboard"]).admin_support_reply_keyboard(user.id),
            )
            delivered += 1
        except Exception:
            logger.exception("Failed to forward support message to admin %s", admin_id)

    if delivered:
        await update.message.reply_text(
            "✅ <b>Message sent</b>\n\n"
            "Your request has been delivered to our support team. A team member "
            "will reply here as soon as possible.",
            parse_mode="HTML",
            reply_markup=MAIN_MENU,
        )
    else:
        await update.message.reply_text(
            "⚠️ <b>Message not delivered</b>\n\n"
            "We could not reach the support team right now. Please try again later.",
            parse_mode="HTML",
            reply_markup=MAIN_MENU,
        )
    return ConversationHandler.END


async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        await query.edit_message_text(
            "No problem — the action has been cancelled.\n\nChoose an option below.",
            reply_markup=MAIN_MENU,
        )
    else:
        await update.message.reply_text(
            "No problem — the action has been cancelled.",
            reply_markup=MAIN_MENU,
        )
    return ConversationHandler.END


def _format_time_remaining(deadline: datetime) -> str:
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    remaining = deadline - datetime.now(timezone.utc)
    if remaining.total_seconds() <= 0:
        return "0h 0m"
    hours, rem = divmod(int(remaining.total_seconds()), 3600)
    minutes = rem // 60
    return f"{hours}h {minutes}m"


STATUS_LABELS = {
    ApplicationStatus.PENDING_POST_CHECK: "Initial checks in progress",
    ApplicationStatus.TRACKING_VIEWS: "Verification in progress",
    ApplicationStatus.VERIFICATION_PASSED: "Verification complete",
    ApplicationStatus.ADMIN_REVIEW: "Awaiting final review",
    ApplicationStatus.APPROVED: "Approved",
    ApplicationStatus.REJECTED: "Not approved",
    ApplicationStatus.EXPIRED: "Verification window expired",
    ApplicationStatus.FAILED: "Verification unsuccessful",
}


def _status_text(application: Application) -> str:
    channel = html.escape(application.channel_username or "Unknown")
    status = STATUS_LABELS.get(application.status, application.status.value)
    lines = [
        "📋 <b>Application Status</b>",
        "",
        f"<b>Channel:</b> @{channel}",
        f"<b>Status:</b> {status}",
    ]
    if application.status == ApplicationStatus.TRACKING_VIEWS and application.verification_deadline:
        deadline = application.verification_deadline
        lines.extend(
            [
                "",
                f"<b>Referral views:</b> {application.current_views:,} / {settings.min_referral_views:,}",
                f"<b>Time remaining:</b> {_format_time_remaining(deadline)}",
                "",
                "Your application is being monitored automatically. No further action is required right now.",
            ]
        )
    elif application.status == ApplicationStatus.ADMIN_REVIEW:
        lines.extend(["", "Your automatic verification has passed. Our team is completing the final review."])
    elif application.status == ApplicationStatus.APPROVED:
        lines.extend(["", "🎉 Your channel has been approved and is now part of the approved advertising network."])
    elif application.status == ApplicationStatus.REJECTED:
        if application.rejection_reason:
            lines.extend(["", f"<b>Review note:</b> {html.escape(application.rejection_reason)}"])
        lines.extend(["", "You may address the issue and submit a new qualifying application when ready."])
    elif application.status == ApplicationStatus.EXPIRED:
        lines.extend(["", "The required view milestone was not reached during the verification window."])
    elif application.status == ApplicationStatus.FAILED:
        lines.extend(["", "The verification could not be completed. Please review the requirements and try again with a qualifying post."])
    return "\n".join(lines)


async def my_application(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with session_scope() as session:
        application = get_user_active_application(session, update.effective_user.id)
        if application is None:
            text = (
                "📋 <b>My Application</b>\n\n"
                "You don’t have an active application at the moment.\n\n"
                "When your channel is ready, start a new application from the main menu."
            )
        else:
            text = _status_text(application)

    if update.callback_query:
        query = update.callback_query
        await query.answer()
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=user_back_keyboard())
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=user_back_keyboard())


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
                "📋 <b>Application already in progress</b>\n\n"
                "You already have an active application. A new submission cannot be started until the current one is complete.\n\n"
                + _status_text(existing),
                parse_mode="HTML",
                reply_markup=user_back_keyboard(),
            )
            return ConversationHandler.END

    await send(
        REQUIREMENTS_TEXT + "\n\n🔗 <b>Next step</b>\nSend the link to the qualifying channel post.",
        parse_mode="HTML",
        reply_markup=user_cancel_keyboard(),
    )
    return WAITING_POST_LINK


async def receive_post_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    post_url = update.message.text.strip()

    with session_scope() as session:
        db_user = get_or_create_user(session, user.id, user.username, user.first_name)

        if db_user.last_applied_at:
            elapsed = (datetime.utcnow() - db_user.last_applied_at).total_seconds()
            if elapsed < settings.apply_rate_limit_seconds:
                wait = max(1, int(settings.apply_rate_limit_seconds - elapsed))
                await update.message.reply_text(
                    "⏳ <b>Please wait before submitting again</b>\n\n"
                    f"You can try again in approximately <b>{wait} seconds</b>.",
                    parse_mode="HTML",
                    reply_markup=user_cancel_keyboard(),
                )
                return WAITING_POST_LINK

        user_active = get_user_active_application(session, user.id)
        if user_active is not None:
            if user_active.post_url == post_url:
                await update.message.reply_text(
                    "📋 <b>This post is already being verified</b>\n\n"
                    "Submitting it again will not restart the verification timer.\n\n"
                    + _status_text(user_active),
                    parse_mode="HTML",
                    reply_markup=user_back_keyboard(),
                )
                return ConversationHandler.END
            await update.message.reply_text(
                "📋 <b>You already have an active application</b>\n\n"
                "Please wait for the current verification to finish before submitting another post.\n\n"
                + _status_text(user_active),
                parse_mode="HTML",
                reply_markup=user_back_keyboard(),
            )
            return ConversationHandler.END

        other_active = get_active_application_for_post(session, post_url)
        if other_active is not None:
            await update.message.reply_text(
                "⚠️ <b>Post already under verification</b>\n\n"
                "This post is already linked to another active application and cannot be submitted again.",
                parse_mode="HTML",
                reply_markup=MAIN_MENU,
            )
            return ConversationHandler.END

    try:
        result = await run_full_check(post_url)
    except PostLinkError as exc:
        await update.message.reply_text(
            f"⚠️ <b>Invalid post link</b>\n\n{html.escape(str(exc))}\n\n"
            "Please send a valid public Telegram channel post link.",
            parse_mode="HTML",
            reply_markup=user_cancel_keyboard(),
        )
        return WAITING_POST_LINK
    except ChannelNotAccessibleError as exc:
        await update.message.reply_text(
            f"⚠️ <b>Channel could not be checked</b>\n\n{html.escape(str(exc))}\n\n"
            "Please make sure the channel and post are publicly accessible, then try again.",
            parse_mode="HTML",
            reply_markup=user_cancel_keyboard(),
        )
        return WAITING_POST_LINK
    except Exception:
        logger.exception("Unexpected error verifying %s", post_url)
        await update.message.reply_text(
            "⚠️ <b>Verification temporarily unavailable</b>\n\n"
            "We could not complete the check right now. Please try again in a few minutes.",
            parse_mode="HTML",
            reply_markup=user_cancel_keyboard(),
        )
        return WAITING_POST_LINK

    with session_scope() as session:
        if is_channel_already_approved(session, result.channel.channel_id):
            await update.message.reply_text(
                "ℹ️ <b>Channel already approved</b>\n\n"
                "This channel is already part of the approved advertising network, so a new application is not required.",
                parse_mode="HTML",
                reply_markup=MAIN_MENU,
            )
            return ConversationHandler.END

        channel_active = get_active_application_for_channel(session, result.channel.channel_id)
        if channel_active is not None and channel_active.post_url != post_url:
            await update.message.reply_text(
                "⚠️ <b>Channel already has an active application</b>\n\n"
                "Please wait for the existing application to finish before submitting another post.",
                parse_mode="HTML",
                reply_markup=MAIN_MENU,
            )
            return ConversationHandler.END

        if not result.subscriber_ok:
            await update.message.reply_text(
                "❌ <b>Subscriber requirement not met</b>\n\n"
                f"Current subscribers: <b>{result.channel.subscriber_count:,}</b>\n"
                f"Required: <b>{settings.min_subscribers:,}</b>\n\n"
                "Once your channel meets the requirement, you can submit a new application.",
                parse_mode="HTML",
                reply_markup=MAIN_MENU,
            )
            return ConversationHandler.END

        if not result.average_views_ok:
            await update.message.reply_text(
                "❌ <b>Average views requirement not met</b>\n\n"
                f"Current average: <b>{result.average_views:.0f}</b> views per post\n"
                f"Required average: <b>{settings.min_average_views:,}</b>\n\n"
                "You can apply again once the requirement is met.",
                parse_mode="HTML",
                reply_markup=MAIN_MENU,
            )
            return ConversationHandler.END

        if not result.referral_link_ok:
            await update.message.reply_text(
                "❌ <b>Referral link not found</b>\n\n"
                "We could not find the official Mini App referral link in the submitted post.\n\n"
                f"Please publish the correct referral link beginning with <code>{html.escape(settings.referral_url_prefix)}</code> and submit that post instead.",
                parse_mode="HTML",
                reply_markup=MAIN_MENU,
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
        "🎯 <b>Application accepted for verification</b>\n\n"
        f"Subscribers: <b>{result.channel.subscriber_count:,}</b>\n"
        f"Average views: <b>{result.average_views:.0f}</b>\n"
        "Referral link: <b>Verified</b> ✅\n\n"
        f"We are now monitoring the post for <b>{settings.verification_hours} hours</b>. "
        f"It needs to reach <b>{settings.min_referral_views:,} views</b> to complete automatic verification.\n\n"
        "You can check your progress anytime from <b>My Application</b>.",
        parse_mode="HTML",
        reply_markup=MAIN_MENU,
    )
    return ConversationHandler.END


def build_apply_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CommandHandler("apply", apply_entry),
            CallbackQueryHandler(apply_entry, pattern=r"^(?:apply_start|user_apply)$"),
        ],
        states={
            WAITING_POST_LINK: [
                CallbackQueryHandler(cancel_conversation, pattern=r"^user_cancel$"),
                CommandHandler("cancel", cancel_conversation),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_post_link),
            ]
        },
        fallbacks=[
            CallbackQueryHandler(cancel_conversation, pattern=r"^user_cancel$"),
            CommandHandler("cancel", cancel_conversation),
        ],
        name="apply_conversation",
    )


def build_support_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CommandHandler("support", support_entry),
            CallbackQueryHandler(support_entry, pattern=r"^user_support$"),
        ],
        states={
            WAITING_SUPPORT_MESSAGE: [
                CallbackQueryHandler(cancel_conversation, pattern=r"^user_cancel$"),
                CommandHandler("cancel", cancel_conversation),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_support_message),
            ]
        },
        fallbacks=[
            CallbackQueryHandler(cancel_conversation, pattern=r"^user_cancel$"),
            CommandHandler("cancel", cancel_conversation),
        ],
        name="support_conversation",
    )


def register(application) -> None:
    application.add_handler(CommandHandler("start", start))
    application.add_handler(build_apply_conversation())
    application.add_handler(build_support_conversation())
    application.add_handler(CallbackQueryHandler(home_callback, pattern=r"^user_home$"))
    application.add_handler(CallbackQueryHandler(my_application, pattern=r"^user_application$"))
    application.add_handler(CallbackQueryHandler(show_requirements, pattern=r"^user_requirements$"))
    application.add_handler(MessageHandler(filters.Regex(r"^📢 Free Advertisement$"), apply_entry))
    application.add_handler(MessageHandler(filters.Regex(r"^📋 My Application$"), my_application))
    application.add_handler(MessageHandler(filters.Regex(r"^ℹ️ Requirements$"), show_requirements))
    application.add_handler(MessageHandler(filters.Regex(r"^📞 Support$"), support_entry))
    application.add_handler(CommandHandler("myapplication", my_application))
