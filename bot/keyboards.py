"""Reusable Telegram keyboards."""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup

# Keep the original user-facing keyboard exactly as in the previous bot.
MAIN_MENU = ReplyKeyboardMarkup(
    [
        ["📢 Free Advertisement"],
        ["📋 My Application", "ℹ️ Requirements"],
        ["📞 Support"],
    ],
    resize_keyboard=True,
)


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    """Compatibility helper for existing notification code."""
    return MAIN_MENU

APPLY_ENTRY = InlineKeyboardMarkup(
    [[InlineKeyboardButton("📢 Apply for Free Advertisement", callback_data="apply_start")]]
)


def admin_review_keyboard(application_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ APPROVE", callback_data=f"admin_approve:{application_id}"),
                InlineKeyboardButton("❌ REJECT", callback_data=f"admin_reject:{application_id}"),
            ],
            [InlineKeyboardButton("🔍 VIEW DETAILS", callback_data=f"admin_details:{application_id}")],
        ]
    )


def admin_pending_list_keyboard(application_ids: list[int]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"Application #{app_id}", callback_data=f"admin_details:{app_id}")]
        for app_id in application_ids
    ]
    return InlineKeyboardMarkup(rows) if rows else None


def support_admin_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✍️ Reply", callback_data=f"support_reply:{user_id}"),
                InlineKeyboardButton("🚫 Block User", callback_data=f"support_block:{user_id}"),
            ]
        ]
    )


def block_confirm_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Confirm block", callback_data=f"support_block_confirm:{user_id}"),
                InlineKeyboardButton("Cancel", callback_data=f"support_block_cancel:{user_id}"),
            ]
        ]
    )


# Compatibility keyboards used by the current admin handlers. These additions do
# not alter the user-facing ReplyKeyboard above.
def admin_control_center_keyboard(pending_count: int = 0) -> InlineKeyboardMarkup:
    pending_label = f"📥 Pending Review ({pending_count})" if pending_count else "📥 Pending Review"
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(pending_label, callback_data="admin_pending")],
            [
                InlineKeyboardButton("📣 Broadcast", callback_data="admin_broadcast"),
                InlineKeyboardButton("📊 System Status", callback_data="admin_status"),
            ],
            [InlineKeyboardButton("⚙️ Settings", callback_data="admin_settings")],
        ]
    )


def admin_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🛡️ Official Channel Auto-Decline", callback_data="admin_official_channels")],
            [InlineKeyboardButton("↩️ Control Center", callback_data="admin_center")],
        ]
    )


def admin_broadcast_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Send Broadcast", callback_data="admin_broadcast_confirm")],
            [InlineKeyboardButton("✖️ Cancel", callback_data="admin_broadcast_cancel")],
        ]
    )


def admin_support_reply_keyboard(user_id: int, blocked: bool = False) -> InlineKeyboardMarkup:
    moderation = "🔓 Unblock User" if blocked else "🚫 Block User"
    action = "admin_unblock" if blocked else "admin_block"
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("💬 Reply to User", callback_data=f"admin_support_reply:{user_id}")],
            [InlineKeyboardButton(moderation, callback_data=f"{action}:{user_id}")],
            [InlineKeyboardButton("↩️ Control Center", callback_data="admin_center")],
        ]
    )


def admin_block_confirm_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("🚫 Confirm Block", callback_data=f"admin_block_confirm:{user_id}"),
            InlineKeyboardButton("✖️ Cancel", callback_data="admin_center"),
        ]]
    )


def official_channels_keyboard(channels: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"➖ @{username}", callback_data=f"official_channel_remove:{channel_id}")]
        for channel_id, username in channels
    ]
    rows.append([InlineKeyboardButton("➕ Add Official Channel", callback_data="official_channel_add")])
    rows.append([InlineKeyboardButton("↩️ Back to Settings", callback_data="admin_settings")])
    return InlineKeyboardMarkup(rows)


def official_channel_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("✖️ Cancel", callback_data="official_channel_cancel")]]
    )
