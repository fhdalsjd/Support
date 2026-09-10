"""Reusable Telegram keyboards for users and administrators."""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup

MAIN_MENU = ReplyKeyboardMarkup(
    [
        ["📢 Free Advertisement"],
        ["📋 My Application", "ℹ️ Requirements"],
        ["📞 Support"],
    ],
    resize_keyboard=True,
)

APPLY_ENTRY = InlineKeyboardMarkup(
    [[InlineKeyboardButton("📢 Start Application", callback_data="apply_start")]]
)


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
        [[InlineKeyboardButton("↩️ Back to Control Center", callback_data="admin_center")]]
    )


def admin_broadcast_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Send Broadcast", callback_data="admin_broadcast_confirm"),
                InlineKeyboardButton("✖️ Cancel", callback_data="admin_broadcast_cancel"),
            ]
        ]
    )


def admin_review_keyboard(application_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"admin_approve:{application_id}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"admin_reject:{application_id}"),
            ],
            [InlineKeyboardButton("🔎 View Details", callback_data=f"admin_details:{application_id}")],
            [InlineKeyboardButton("↩️ Control Center", callback_data="admin_center")],
        ]
    )


def admin_pending_list_keyboard(application_ids: list[int]) -> InlineKeyboardMarkup | None:
    rows = [
        [InlineKeyboardButton(f"Application #{app_id}", callback_data=f"admin_details:{app_id}")]
        for app_id in application_ids
    ]
    rows.append([InlineKeyboardButton("↩️ Control Center", callback_data="admin_center")])
    return InlineKeyboardMarkup(rows)
