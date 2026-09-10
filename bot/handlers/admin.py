"""Professional admin control center, review, support, broadcast, and moderation."""
from __future__ import annotations
import html
import logging
from datetime import datetime, timezone
from functools import wraps
from telegram import Update
from telegram.error import TelegramError
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes, ConversationHandler, MessageHandler, filters
from ..config import settings
from ..database import add_approved_channel, block_user, get_application, get_applications_awaiting_admin, get_user, session_scope, unblock_user
from ..keyboards import admin_back_keyboard, admin_block_confirm_keyboard, admin_broadcast_confirm_keyboard, admin_control_center_keyboard, admin_pending_list_keyboard, admin_review_keyboard, admin_support_reply_keyboard
from ..models import Application, ApplicationStatus, User
from ..notifications import notify
logger = logging.getLogger(__name__)
WAITING_REJECT_REASON = 10
WAITING_BROADCAST_MESSAGE = 20
WAITING_SUPPORT_REPLY = 30


def admin_only(handler):
    @wraps(handler)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id if update.effective_user else None
        if uid not in settings.admin_ids:
            if update.callback_query:
                await update.callback_query.answer("This area is restricted to administrators.", show_alert=True)
            elif update.message:
                await update.message.reply_text("This area is restricted to administrators.")
            return ConversationHandler.END
        return await handler(update, context)
    return wrapped


def _format_time_remaining(deadline: datetime | None) -> str:
    if deadline is None: return "Not available"
    if deadline.tzinfo is None: deadline = deadline.replace(tzinfo=timezone.utc)
    remaining = deadline - datetime.now(timezone.utc)
    if remaining.total_seconds() <= 0: return "0h 0m"
    hours, rem = divmod(int(remaining.total_seconds()), 3600)
    return f"{hours}h {rem // 60}m"


def _application_card(app: Application) -> str:
    score = app.activity_score.value if hasattr(app.activity_score, "value") else app.activity_score
    return ("📄 <b>Application Review</b> #{id}\n\n<b>Channel</b>\n@{username} — {title}\n\n"
            "<b>Audience</b>\n{subs:,} subscribers\n{avg:.0f} average views per post\n\n"
            "<b>Referral Post</b>\n{post}\n\n<b>Verification</b>\n"
            "Current views: <b>{cur:,}</b> / {req:,}\nTime remaining: {remaining}\n"
            "Activity score: <b>{score}</b>\nReferral link: <b>Found</b>\n\n"
            "<b>Applicant</b>\n<code>{user}</code>").format(
        id=app.id, username=html.escape(app.channel_username or "unknown"), title=html.escape(app.channel_title or "Untitled channel"),
        subs=app.subscriber_count, avg=app.average_views, post=html.escape(app.post_url or "Not available"),
        cur=app.current_views, req=settings.min_referral_views, remaining=_format_time_remaining(app.verification_deadline),
        score=html.escape(str(score)), user=app.user_id)


async def _send_control_center(target, context):
    with session_scope() as session:
        pending = len(get_applications_awaiting_admin(session)); users = session.query(User).count(); blocked = session.query(User).filter(User.is_blocked.is_(True)).count()
    await target.reply_text(
        "🛡️ <b>Admin Control Center</b>\n\n"
        "Welcome back. Choose an action below.\n\n"
        f"👥 Registered users: <b>{users:,}</b>\n📥 Awaiting review: <b>{pending:,}</b>\n🚫 Blocked users: <b>{blocked:,}</b>",
        parse_mode="HTML", reply_markup=admin_control_center_keyboard(pending))

@admin_only
async def admin_panel(update, context): await _send_control_center(update.message, context)

@admin_only
async def admin_center(update, context):
    q=update.callback_query; await q.answer(); await _send_control_center(q.message, context)

@admin_only
async def admin_pending(update, context):
    q=update.callback_query; await q.answer()
    with session_scope() as s: pending=get_applications_awaiting_admin(s); ids=[a.id for a in pending]
    if not pending:
        await q.edit_message_text("📥 <b>Pending Review</b>\n\nYour review queue is clear.",parse_mode="HTML",reply_markup=admin_back_keyboard()); return
    await q.edit_message_text(f"📥 <b>Pending Review</b>\n\n<b>{len(pending)}</b> application(s) are ready for a decision.",parse_mode="HTML",reply_markup=admin_pending_list_keyboard(ids))

@admin_only
async def admin_status(update, context):
    q=update.callback_query; await q.answer()
    with session_scope() as s:
        users=s.query(User).count(); blocked=s.query(User).filter(User.is_blocked.is_(True)).count(); total=s.query(Application).count()
        tracking=s.query(Application).filter(Application.status==ApplicationStatus.TRACKING_VIEWS).count(); review=s.query(Application).filter(Application.status==ApplicationStatus.ADMIN_REVIEW).count()
        approved=s.query(Application).filter(Application.status==ApplicationStatus.APPROVED).count(); rejected=s.query(Application).filter(Application.status==ApplicationStatus.REJECTED).count(); expired=s.query(Application).filter(Application.status==ApplicationStatus.EXPIRED).count(); failed=s.query(Application).filter(Application.status==ApplicationStatus.FAILED).count()
    text=(f"📊 <b>System Status</b>\n\n👥 Users: <b>{users:,}</b>\n🚫 Blocked: <b>{blocked:,}</b>\n📋 Applications: <b>{total:,}</b>\n\n🔄 Tracking: <b>{tracking:,}</b>\n📥 Final review: <b>{review:,}</b>\n✅ Approved: <b>{approved:,}</b>\n❌ Rejected: <b>{rejected:,}</b>\n⌛ Expired: <b>{expired:,}</b>\n⚠️ Failed: <b>{failed:,}</b>")
    await q.edit_message_text(text,parse_mode="HTML",reply_markup=admin_back_keyboard())

@admin_only
async def admin_settings(update, context):
    q=update.callback_query; await q.answer()
    text=("⚙️ <b>Bot Settings</b>\n\n<b>Eligibility</b>\n"
          f"• Minimum subscribers: <b>{settings.min_subscribers:,}</b>\n• Minimum average views: <b>{settings.min_average_views:,}</b>\n• Referral milestone: <b>{settings.min_referral_views:,}</b> views\n\n"
          "<b>Verification</b>\n"
          f"• Verification window: <b>{settings.verification_hours} hours</b>\n• Check interval: <b>{settings.tracking_poll_minutes} minutes</b>\n• View sample: <b>{settings.average_views_sample_size} posts</b>\n\n"
          "<b>Application</b>\n"
          f"• Re-application cooldown: <b>{settings.apply_rate_limit_seconds} seconds</b>\n• Official bot: <b>@{html.escape(settings.official_bot_username)}</b>")
    await q.edit_message_text(text,parse_mode="HTML",reply_markup=admin_back_keyboard())

@admin_only
async def admin_view_details(update, context):
    q=update.callback_query; await q.answer(); aid=int(q.data.split(":")[1])
    with session_scope() as s: app=get_application(s,aid)
    if not app: await q.edit_message_text("⚠️ <b>Application unavailable</b>",parse_mode="HTML",reply_markup=admin_back_keyboard()); return
    await q.edit_message_text(_application_card(app),parse_mode="HTML",reply_markup=admin_review_keyboard(aid))

async def push_admin_review(context, app):
    text="🚨 <b>New Application Ready for Review</b>\n\n"+_application_card(app)
    for aid in settings.admin_ids:
        try: await context.bot.send_message(aid,text=text,parse_mode="HTML",reply_markup=admin_review_keyboard(app.id))
        except TelegramError: logger.exception("Failed to notify admin %s",aid)

@admin_only
async def admin_approve(update, context):
    q=update.callback_query; await q.answer(); aid=int(q.data.split(":")[1])
    with session_scope() as s:
        app=get_application(s,aid)
        if not app: await q.edit_message_text("⚠️ Application unavailable.",reply_markup=admin_back_keyboard()); return
        if app.status!=ApplicationStatus.ADMIN_REVIEW: await q.answer("This application is no longer awaiting review.",show_alert=True); return
        app.status=ApplicationStatus.APPROVED; app.admin_decision="APPROVED"; app.approved_at=datetime.utcnow(); add_approved_channel(s,app,update.effective_user.id); s.flush(); await notify(context,app,"admin_approved")
    await q.edit_message_text(f"✅ <b>Application #{aid} approved</b>\n\nThe applicant has been notified.",parse_mode="HTML",reply_markup=admin_back_keyboard())

@admin_only
async def admin_reject_start(update, context):
    q=update.callback_query; await q.answer(); aid=int(q.data.split(":")[1])
    with session_scope() as s: app=get_application(s,aid)
    if not app or app.status!=ApplicationStatus.ADMIN_REVIEW: await q.answer("This application is no longer awaiting review.",show_alert=True); return ConversationHandler.END
    context.user_data["reject_application_id"]=aid
    await q.message.reply_text(f"✍️ <b>Reject Application #{aid}</b>\n\nSend a concise reason for the applicant, or use /skip.\nUse /cancel to stop.",parse_mode="HTML")
    return WAITING_REJECT_REASON

@admin_only
async def admin_reject_reason(update,context): return await _finalize_reject(update,context,update.message.text.strip())
@admin_only
async def admin_reject_skip(update,context): return await _finalize_reject(update,context,None)
async def _finalize_reject(update,context,reason):
    aid=context.user_data.pop("reject_application_id",None)
    if aid is None: await update.message.reply_text("No rejection is currently active."); return ConversationHandler.END
    with session_scope() as s:
        app=get_application(s,aid)
        if not app or app.status!=ApplicationStatus.ADMIN_REVIEW: await update.message.reply_text("This application is no longer awaiting review."); return ConversationHandler.END
        app.status=ApplicationStatus.REJECTED; app.admin_decision="REJECTED"; app.rejection_reason=reason; s.flush(); await notify(context,app,"admin_rejected",reason=reason or "No reason was provided.")
    await update.message.reply_text(f"❌ <b>Application #{aid} rejected</b>\n\nThe applicant has been notified.",parse_mode="HTML",reply_markup=admin_control_center_keyboard()); return ConversationHandler.END

@admin_only
async def admin_broadcast_start(update,context):
    q=update.callback_query; await q.answer(); await q.message.reply_text("📣 <b>Broadcast Center</b>\n\nSend the announcement. You will receive a preview before delivery.\n\nUse /cancel to stop.",parse_mode="HTML"); return WAITING_BROADCAST_MESSAGE
@admin_only
async def admin_broadcast_message(update,context):
    text=update.message.text.strip()
    if not text: await update.message.reply_text("Please send a non-empty announcement."); return WAITING_BROADCAST_MESSAGE
    context.user_data["broadcast_text"]=text
    with session_scope() as s: recipients=s.query(User).filter(~User.telegram_id.in_(settings.admin_ids)).count() if settings.admin_ids else s.query(User).count()
    await update.message.reply_text(f"📣 <b>Broadcast Preview</b>\n\nRecipients: <b>{recipients:,}</b>\n\n<b>Message</b>\n{html.escape(text)}\n\nConfirm below.",parse_mode="HTML",reply_markup=admin_broadcast_confirm_keyboard()); return WAITING_BROADCAST_MESSAGE
@admin_only
async def admin_broadcast_confirm(update,context):
    q=update.callback_query; await q.answer(); text=context.user_data.pop("broadcast_text",None)
    if not text: await q.edit_message_text("⚠️ <b>Broadcast draft expired</b>",parse_mode="HTML",reply_markup=admin_back_keyboard()); return ConversationHandler.END
    with session_scope() as s: recipients=[u.telegram_id for u in s.query(User).all() if u.telegram_id not in settings.admin_ids and not u.is_blocked]
    sent=failed=0
    for uid in recipients:
        try: await context.bot.send_message(uid,text=text); sent+=1
        except TelegramError: failed+=1
    await q.edit_message_text(f"📣 <b>Broadcast Complete</b>\n\nDelivered: <b>{sent:,}</b>\nNot delivered: <b>{failed:,}</b>\nBlocked users were excluded.",parse_mode="HTML",reply_markup=admin_back_keyboard()); return ConversationHandler.END
@admin_only
async def admin_broadcast_cancel(update,context):
    q=update.callback_query; await q.answer(); context.user_data.pop("broadcast_text",None); await q.edit_message_text("Broadcast cancelled. No messages were sent.",reply_markup=admin_back_keyboard()); return ConversationHandler.END

@admin_only
async def admin_support_reply_start(update,context):
    q=update.callback_query; await q.answer(); uid=int(q.data.split(":")[1])
    if uid in settings.admin_ids: await q.answer("Administrators cannot be moderated.",show_alert=True); return ConversationHandler.END
    with session_scope() as s: user=get_user(s,uid)
    if not user: await q.answer("User record not found.",show_alert=True); return ConversationHandler.END
    if user.is_blocked: await q.answer("This user is blocked.",show_alert=True); return ConversationHandler.END
    context.user_data["support_reply_user_id"]=uid
    await q.message.reply_text(f"💬 <b>Support Reply</b>\n\nReplying to user <code>{uid}</code>.\nSend your response, or /cancel.",parse_mode="HTML"); return WAITING_SUPPORT_REPLY
@admin_only
async def admin_support_reply_send(update,context):
    uid=context.user_data.pop("support_reply_user_id",None); text=update.message.text.strip()
    if uid is None: await update.message.reply_text("No support reply is currently active."); return ConversationHandler.END
    with session_scope() as s: user=get_user(s,uid); blocked=bool(user and user.is_blocked)
    if blocked: await update.message.reply_text("🚫 This user is blocked. Reply was not sent.",reply_markup=admin_control_center_keyboard()); return ConversationHandler.END
    if not text: context.user_data["support_reply_user_id"]=uid; await update.message.reply_text("Please send a non-empty reply."); return WAITING_SUPPORT_REPLY
    try: await context.bot.send_message(uid,text=f"💬 <b>Support Team</b>\n\n{html.escape(text)}\n\nIf you need further assistance, contact us again from the main menu.",parse_mode="HTML")
    except TelegramError: await update.message.reply_text("⚠️ <b>Reply not delivered</b>\n\nTelegram could not deliver this message.",parse_mode="HTML",reply_markup=admin_control_center_keyboard()); return ConversationHandler.END
    await update.message.reply_text(f"✅ <b>Reply sent</b>\n\nDelivered to user <code>{uid}</code>.",parse_mode="HTML",reply_markup=admin_control_center_keyboard()); return ConversationHandler.END

@admin_only
async def admin_block_start(update,context):
    q=update.callback_query; await q.answer(); uid=int(q.data.split(":")[1])
    if uid in settings.admin_ids: await q.answer("Administrators cannot be blocked.",show_alert=True); return
    with session_scope() as s: user=get_user(s,uid)
    if not user: await q.answer("User record not found.",show_alert=True); return
    if user.is_blocked: await q.answer("This user is already blocked.",show_alert=True); return
    await q.edit_message_reply_markup(reply_markup=admin_block_confirm_keyboard(uid))
    await q.message.reply_text("⚠️ <b>Block User</b>\n\nThis will prevent the user from applying, contacting support, and receiving broadcasts.\n\nConfirm only if moderation is appropriate.",parse_mode="HTML")

@admin_only
async def admin_block_confirm(update,context):
    q=update.callback_query; await q.answer(); uid=int(q.data.split(":")[1])
    if uid in settings.admin_ids: await q.answer("Administrators cannot be blocked.",show_alert=True); return
    with session_scope() as s:
        user=block_user(s,uid,update.effective_user.id,"Blocked by administrator")
        if not user: await q.edit_message_text("⚠️ User record not found.",reply_markup=admin_back_keyboard()); return
    try: await context.bot.send_message(uid,"🚫 <b>Access restricted</b>\n\nYour access to this bot has been restricted by the moderation team. If you believe this was a mistake, please contact the service administrator.",parse_mode="HTML")
    except TelegramError: pass
    await q.edit_message_text(f"🚫 <b>User {uid} blocked</b>\n\nThe user can no longer use the bot or receive broadcasts.",parse_mode="HTML",reply_markup=admin_back_keyboard())

@admin_only
async def admin_unblock(update,context):
    q=update.callback_query; await q.answer(); uid=int(q.data.split(":")[1])
    if uid in settings.admin_ids: await q.answer("Administrators are always protected.",show_alert=True); return
    with session_scope() as s: user=unblock_user(s,uid)
    if not user: await q.edit_message_text("⚠️ User record not found.",reply_markup=admin_back_keyboard()); return
    try: await context.bot.send_message(uid,"🔓 <b>Access restored</b>\n\nYour access to the bot has been restored. You may use the service normally again.",parse_mode="HTML")
    except TelegramError: pass
    await q.edit_message_text(f"🔓 <b>User {uid} unblocked</b>\n\nAccess has been restored.",parse_mode="HTML",reply_markup=admin_back_keyboard())

async def cancel_admin_action(update,context):
    for key in ("reject_application_id","broadcast_text","support_reply_user_id"): context.user_data.pop(key,None)
    await update.message.reply_text("Action cancelled. Nothing was changed or sent.",reply_markup=admin_control_center_keyboard()); return ConversationHandler.END

def build_reject_conversation():
    return ConversationHandler(entry_points=[CallbackQueryHandler(admin_reject_start,pattern=r"^admin_reject:\d+$")],states={WAITING_REJECT_REASON:[CommandHandler("skip",admin_reject_skip),CommandHandler("cancel",cancel_admin_action),MessageHandler(filters.TEXT & ~filters.COMMAND,admin_reject_reason)]},fallbacks=[CommandHandler("skip",admin_reject_skip),CommandHandler("cancel",cancel_admin_action)],name="admin_reject_conversation")
def build_broadcast_conversation():
    return ConversationHandler(entry_points=[CallbackQueryHandler(admin_broadcast_start,pattern=r"^admin_broadcast$")],states={WAITING_BROADCAST_MESSAGE:[CallbackQueryHandler(admin_broadcast_confirm,pattern=r"^admin_broadcast_confirm$"),CallbackQueryHandler(admin_broadcast_cancel,pattern=r"^admin_broadcast_cancel$"),CommandHandler("cancel",cancel_admin_action),MessageHandler(filters.TEXT & ~filters.COMMAND,admin_broadcast_message)]},fallbacks=[CommandHandler("cancel",cancel_admin_action)],name="admin_broadcast_conversation")
def build_support_reply_conversation():
    return ConversationHandler(entry_points=[CallbackQueryHandler(admin_support_reply_start,pattern=r"^admin_support_reply:\d+$")],states={WAITING_SUPPORT_REPLY:[CommandHandler("cancel",cancel_admin_action),MessageHandler(filters.TEXT & ~filters.COMMAND,admin_support_reply_send)]},fallbacks=[CommandHandler("cancel",cancel_admin_action)],name="admin_support_reply_conversation")

def register(application):
    application.add_handler(CommandHandler("admin",admin_panel)); application.add_handler(CommandHandler("pending",admin_panel))
    application.add_handler(build_broadcast_conversation()); application.add_handler(build_reject_conversation()); application.add_handler(build_support_reply_conversation())
    application.add_handler(CallbackQueryHandler(admin_center,pattern=r"^admin_center$")); application.add_handler(CallbackQueryHandler(admin_pending,pattern=r"^admin_pending$")); application.add_handler(CallbackQueryHandler(admin_status,pattern=r"^admin_status$")); application.add_handler(CallbackQueryHandler(admin_settings,pattern=r"^admin_settings$")); application.add_handler(CallbackQueryHandler(admin_view_details,pattern=r"^admin_details:\d+$")); application.add_handler(CallbackQueryHandler(admin_approve,pattern=r"^admin_approve:\d+$")); application.add_handler(CallbackQueryHandler(admin_block_start,pattern=r"^admin_block:\d+$")); application.add_handler(CallbackQueryHandler(admin_block_confirm,pattern=r"^admin_block_confirm:\d+$")); application.add_handler(CallbackQueryHandler(admin_unblock,pattern=r"^admin_unblock:\d+$"))
