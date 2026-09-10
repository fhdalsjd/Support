"""Inline keyboards used by the user and admin interfaces."""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Apply for Promotion", callback_data="user_apply")],
        [InlineKeyboardButton("📋 My Application", callback_data="user_application"), InlineKeyboardButton("ℹ️ Requirements", callback_data="user_requirements")],
        [InlineKeyboardButton("💬 Contact Support", callback_data="user_support")],
    ])

MAIN_MENU = main_menu_keyboard()
APPLY_ENTRY = InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Start Application", callback_data="apply_start")]])
REQUIREMENTS_CONFIRM = InlineKeyboardMarkup([[InlineKeyboardButton("✅ I Have Read the Requirements", callback_data="requirements_confirm")]])

def user_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="user_home")]])

def user_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("✖️ Cancel", callback_data="user_cancel")]])

def admin_control_center_keyboard(pending_count: int = 0) -> InlineKeyboardMarkup:
    pending_label = f"📥 Pending Review ({pending_count})" if pending_count else "📥 Pending Review"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(pending_label, callback_data="admin_pending")],
        [InlineKeyboardButton("📣 Broadcast", callback_data="admin_broadcast"), InlineKeyboardButton("📊 System Status", callback_data="admin_status")],
        [InlineKeyboardButton("⚙️ Settings", callback_data="admin_settings")],
    ])

def admin_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Control Center", callback_data="admin_center")]])

def admin_broadcast_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Send Broadcast", callback_data="admin_broadcast_confirm")],
        [InlineKeyboardButton("✖️ Cancel", callback_data="admin_broadcast_cancel")],
    ])

def admin_support_reply_keyboard(user_id: int, blocked: bool = False) -> InlineKeyboardMarkup:
    moderation = "🔓 Unblock User" if blocked else "🚫 Block User"
    action = "admin_unblock" if blocked else "admin_block"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Reply to User", callback_data=f"admin_support_reply:{user_id}")],
        [InlineKeyboardButton(moderation, callback_data=f"{action}:{user_id}")],
        [InlineKeyboardButton("↩️ Control Center", callback_data="admin_center")],
    ])

def admin_block_confirm_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🚫 Confirm Block", callback_data=f"admin_block_confirm:{user_id}"),
        InlineKeyboardButton("✖️ Cancel", callback_data="admin_center"),
    ]])

def admin_review_keyboard(application_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Approve", callback_data=f"admin_approve:{application_id}"), InlineKeyboardButton("❌ Reject", callback_data=f"admin_reject:{application_id}")],
        [InlineKeyboardButton("🔎 View Details", callback_data=f"admin_details:{application_id}")],
        [InlineKeyboardButton("↩️ Control Center", callback_data="admin_center")],
    ])

def admin_pending_list_keyboard(application_ids: list[int]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(f"📄 Application #{app_id}", callback_data=f"admin_details:{app_id}")] for app_id in application_ids]
    rows.append([InlineKeyboardButton("↩️ Control Center", callback_data="admin_center")])
    return InlineKeyboardMarkup(rows)
