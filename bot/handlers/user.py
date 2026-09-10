"""User-facing handlers: main menu, the apply conversation, and status lookup."""
from __future__ import annotations
import logging
from datetime import datetime,timedelta,timezone
from telegram import Update
from telegram.ext import CallbackQueryHandler,CommandHandler,ContextTypes,ConversationHandler,MessageHandler,filters
from ..config import settings
from ..database import get_active_application_for_channel,get_active_application_for_post,get_or_create_user,get_user_active_application,is_channel_already_approved,session_scope
from ..keyboards import APPLY_ENTRY,MAIN_MENU
from ..models import ActivityScore,Application,ApplicationStatus
from ..notifications import notify
from ..telethon_client import ChannelNotAccessibleError,PostLinkError
from ..verification import run_full_check
logger=logging.getLogger(__name__); WAITING_POST_LINK=1
REQUIREMENTS_TEXT=("ℹ️ <b>Eligibility requirements</b>\n\n" f"• {settings.min_subscribers:,}+ subscribers\n" f"• {settings.min_average_views}+ average views per post\n" "• A post publishing our official Mini App referral link\n" f"• That same post must reach {settings.min_referral_views}+ views within {settings.verification_hours} hours\n\n" "Send your channel post link (e.g. https://t.me/channelname/123) once it's ready.")
async def start(update,context):
    with session_scope() as session: get_or_create_user(session,update.effective_user.id,update.effective_user.username,update.effective_user.first_name)
    await update.message.reply_text("🎁 <b>Free Advertisement</b>\n\nPromote your channel by advertising our Mini App for free -- once your channel is verified and approved.",parse_mode="HTML",reply_markup=MAIN_MENU); await update.message.reply_text("Tap below to get started.",reply_markup=APPLY_ENTRY)
async def show_requirements(update,context): await update.message.reply_text(REQUIREMENTS_TEXT,parse_mode="HTML")
async def show_support(update,context): await update.message.reply_text("📞 <b>Support</b>\n\nHaving trouble with your application? Contact an admin and mention your Telegram ID: "+f"<code>{update.effective_user.id}</code>",parse_mode="HTML")
def _format_time_remaining(deadline):
    remaining=deadline-datetime.now(timezone.utc)
    if remaining.total_seconds()<=0:return "0h 0m"
    h,rem=divmod(int(remaining.total_seconds()),3600); return f"{h}h {rem//60}m"
def _status_text(application):
    lines=["📋 <b>Your application</b>","",f"Channel: @{application.channel_username}",f"Status: <b>{application.status.value}</b>"]
    if application.status==ApplicationStatus.TRACKING_VIEWS and application.verification_deadline:
        d=application.verification_deadline; d=d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d; lines += [f"Current views: {application.current_views}/{settings.min_referral_views}",f"Time remaining: {_format_time_remaining(d)}"]
    if application.status==ApplicationStatus.REJECTED and application.rejection_reason: lines.append(f"Reason: {application.rejection_reason}")
    return "\n".join(lines)
async def my_application(update,context):
    with session_scope() as session:
        application=get_user_active_application(session,update.effective_user.id)
        if application is None: await update.message.reply_text("You don't have an active application right now. Tap 📢 Free Advertisement to apply."); return
        await update.message.reply_text(_status_text(application),parse_mode="HTML")
async def apply_entry(update,context):
    query=update.callback_query
    if query: await query.answer(); send=query.message.reply_text
    else: send=update.message.reply_text
    with session_scope() as session:
        existing=get_user_active_application(session,update.effective_user.id)
        if existing is not None: await send("You already have an application in progress:\n\n"+_status_text(existing),parse_mode="HTML"); return ConversationHandler.END
    await send(REQUIREMENTS_TEXT,parse_mode="HTML"); return WAITING_POST_LINK
async def receive_post_link(update,context):
    user=update.effective_user; post_url=update.message.text.strip()
    with session_scope() as session:
        db_user=get_or_create_user(session,user.id,user.username,user.first_name)
        if db_user.last_applied_at:
            elapsed=(datetime.utcnow()-db_user.last_applied_at).total_seconds()
            if elapsed<settings.apply_rate_limit_seconds: await update.message.reply_text(f"Please wait {int(settings.apply_rate_limit_seconds-elapsed)}s before submitting again."); return WAITING_POST_LINK
        active=get_user_active_application(session,user.id)
        if active is not None:
            if active.post_url==post_url: await update.message.reply_text("This post is already under verification -- the timer is not reset by resubmitting it.\n\n"+_status_text(active),parse_mode="HTML"); return ConversationHandler.END
            await update.message.reply_text("You already have an active application in progress. Please wait for it to finish before submitting a different post.\n\n"+_status_text(active),parse_mode="HTML"); return ConversationHandler.END
        if get_active_application_for_post(session,post_url) is not None: await update.message.reply_text("❌ This post is already being verified as part of another application."); return ConversationHandler.END
    try: result=await run_full_check(post_url)
    except (PostLinkError,ChannelNotAccessibleError) as exc: await update.message.reply_text(f"❌ {exc}"); return WAITING_POST_LINK
    except Exception: logger.exception("Unexpected error verifying %s",post_url); await update.message.reply_text("❌ Something went wrong while checking that post. Please try again shortly."); return WAITING_POST_LINK
    with session_scope() as session:
        if is_channel_already_approved(session,result.channel.channel_id): await update.message.reply_text("ℹ️ This channel is already approved for free advertising -- no need to reapply."); return ConversationHandler.END
        active=get_active_application_for_channel(session,result.channel.channel_id)
        if active is not None and active.post_url!=post_url: await update.message.reply_text("❌ This channel already has another active application in progress."); return ConversationHandler.END
        if not result.subscriber_ok: await update.message.reply_text(f"❌ Subscriber requirement not met.\n\nYour channel has {result.channel.subscriber_count:,} subscribers. A minimum of {settings.min_subscribers:,} is required."); return ConversationHandler.END
        if not result.average_views_ok: await update.message.reply_text(f"❌ Average views requirement not met.\n\nYour channel's average is {result.average_views:.0f} views/post. A minimum of {settings.min_average_views} is required."); return ConversationHandler.END
        if not result.referral_link_ok: await update.message.reply_text("❌ Official Mini App referral link not found in that post.\n\nPlease publish a post containing a link in the form "+f"{settings.referral_url_prefix}<id> and submit it here."); return ConversationHandler.END
        application=Application(user_id=user.id,channel_id=result.channel.channel_id,channel_username=result.channel.username,channel_title=result.channel.title,post_id=result.post.post_id,post_url=post_url,initial_views=result.post.views,current_views=result.post.views,subscriber_count=result.channel.subscriber_count,average_views=result.average_views,activity_score=ActivityScore(result.activity_score),activity_notes=result.activity_notes,referral_link_found=True,status=ApplicationStatus.TRACKING_VIEWS,verification_deadline=datetime.utcnow()+timedelta(hours=settings.verification_hours)); session.add(application); db_user.last_applied_at=datetime.utcnow(); session.flush(); await notify(context,application,"received"); await notify(context,application,"post_check_passed",hours=settings.verification_hours)
    await update.message.reply_text(f"✅ Eligible so far!\n\nSubscribers: {result.channel.subscriber_count:,}\nAverage views: {result.average_views:.0f}\nReferral link: FOUND ✅\n\nNow tracking this post -- it needs {settings.min_referral_views}+ views within {settings.verification_hours} hours. Check 📋 My Application for progress."); return ConversationHandler.END
async def cancel_apply(update,context): await update.message.reply_text("Cancelled. You can apply again anytime from the main menu."); return ConversationHandler.END
def build_apply_conversation():
    return ConversationHandler(entry_points=[CommandHandler("apply",apply_entry),MessageHandler(filters.Regex("^📢 Free Advertisement$"),apply_entry),CallbackQueryHandler(apply_entry,pattern="^apply_start$")],states={WAITING_POST_LINK:[CommandHandler("cancel",cancel_apply),MessageHandler(filters.TEXT & ~filters.COMMAND,receive_post_link)]},fallbacks=[CommandHandler("cancel",cancel_apply)],name="apply_conversation")
def register(application):
    application.add_handler(CommandHandler("start",start)); application.add_handler(build_apply_conversation()); application.add_handler(MessageHandler(filters.Regex("^📋 My Application$"),my_application)); application.add_handler(CommandHandler("myapplication",my_application)); application.add_handler(MessageHandler(filters.Regex("^ℹ️ Requirements$"),show_requirements)); application.add_handler(MessageHandler(filters.Regex("^📞 Support$"),show_support))
