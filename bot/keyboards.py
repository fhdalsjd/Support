from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
MAIN_MENU=ReplyKeyboardMarkup([["📢 Free Advertisement"],["📋 My Application","ℹ️ Requirements"],["📞 Support"]],resize_keyboard=True)
APPLY_ENTRY=InlineKeyboardMarkup([[InlineKeyboardButton("📢 Apply for Free Advertisement",callback_data="apply_start")]])
def admin_review_keyboard(application_id:int)->InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("✅ APPROVE",callback_data=f"admin_approve:{application_id}"),InlineKeyboardButton("❌ REJECT",callback_data=f"admin_reject:{application_id}")],[InlineKeyboardButton("🔍 VIEW DETAILS",callback_data=f"admin_details:{application_id}")]])
def admin_pending_list_keyboard(application_ids:list[int])->InlineKeyboardMarkup:
    rows=[[InlineKeyboardButton(f"Application #{x}",callback_data=f"admin_details:{x}")] for x in application_ids]
    return InlineKeyboardMarkup(rows) if rows else None
