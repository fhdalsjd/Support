import html, logging, re
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from settings import settings
from wallet import wallet
from security import inspect_token, format_token
from trading import trader, TradeError
from store import init_db, positions

logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")
log=logging.getLogger("solbot")

def allowed(uid:int)->bool:return uid in settings.admins

def dashboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("💰 Buy / Trade Token",callback_data="buy_help"),InlineKeyboardButton("📊 Open Positions",callback_data="positions")],[InlineKeyboardButton("👛 Wallet Settings",callback_data="wallet"),InlineKeyboardButton("🎯 Auto-Sniper: OFF",callback_data="sniper")],[InlineKeyboardButton("⚙️ Slippage / Priority",callback_data="settings"),InlineKeyboardButton("🔄 Refresh",callback_data="refresh")]])

async def start(update:Update,context:ContextTypes.DEFAULT_TYPE):
    if not allowed(update.effective_user.id):return
    try: bal=wallet.balance_sol(); address=wallet.short_address()
    except Exception as e:return await update.message.reply_text(f"Wallet error: {e}")
    await update.message.reply_text(f"<b>⚡ Solana Memecoin Trader</b>\n\nBalance: <b>{bal:.6f} SOL</b>\nPublic Key: <code>{address}</code>\n\nPaste any Solana token mint address to inspect it.",parse_mode=ParseMode.HTML,reply_markup=dashboard())

async def inspect_message(update,context):
    if not allowed(update.effective_user.id):return
    text=(update.message.text or "").strip()
    m=re.search(r"[1-9A-HJ-NP-Za-km-z]{32,44}",text)
    if not m:return
    mint=m.group(0)
    try:info=await inspect_token(mint)
    except Exception as e:return await update.message.reply_text(f"❌ {html.escape(str(e))}")
    kb=InlineKeyboardMarkup([[InlineKeyboardButton("🟢 0.1 SOL",callback_data=f"buy|{mint}|0.1"),InlineKeyboardButton("🟢 0.5 SOL",callback_data=f"buy|{mint}|0.5")],[InlineKeyboardButton("🟢 1.0 SOL",callback_data=f"buy|{mint}|1.0"),InlineKeyboardButton("✏️ Custom",callback_data=f"custom|{mint}")],[InlineKeyboardButton("❌ Cancel",callback_data="cancel")]])
    await update.message.reply_text(format_token(info),parse_mode=ParseMode.HTML,reply_markup=kb)

async def custom_amount(update,context):
    if not allowed(update.effective_user.id):return
    mint=context.user_data.get("custom_mint")
    if not mint:return
    try:amt=float(update.message.text.strip())
    except ValueError:return
    if amt<=0 or amt>100:return await update.message.reply_text("Amount must be > 0 and <= 100 SOL.")
    try:tx=await trader.execute(mint,"buy",str(amt))
    except TradeError as e:return await update.message.reply_text(f"❌ {html.escape(str(e))}")
    await update.message.reply_text(f"✅ Buy submitted\nTx: <code>{tx}</code>",parse_mode=ParseMode.HTML);context.user_data.pop("custom_mint",None)

async def show_positions(q):
    ps=positions()
    if not ps:return await q.edit_message_text("📊 No tracked positions.",reply_markup=dashboard())
    body="\n\n".join(f"<b>{html.escape(x['symbol'])}</b> <code>{x['mint'][:6]}…</code>\nQty: {x['qty']:.6f} | Entry: {x['entry_sol']:.6f} SOL" for x in ps)
    await q.edit_message_text("📊 <b>Open Positions</b>\n\n"+body,parse_mode=ParseMode.HTML,reply_markup=dashboard())

async def callback(update:Update,context:ContextTypes.DEFAULT_TYPE):
    q=update.callback_query;await q.answer()
    if not allowed(q.from_user.id):return
    parts=q.data.split("|")
    if q.data=="positions":return await show_positions(q)
    if q.data=="refresh":return await q.edit_message_text(f"<b>Wallet</b>\n{wallet.balance_sol():.6f} SOL\n<code>{wallet.pubkey}</code>",parse_mode=ParseMode.HTML,reply_markup=dashboard())
    if q.data=="wallet":return await q.edit_message_text(f"👛 <b>Wallet</b>\n<code>{wallet.pubkey}</code>\nBalance: {wallet.balance_sol():.6f} SOL",parse_mode=ParseMode.HTML,reply_markup=dashboard())
    if q.data=="settings":return await q.edit_message_text(f"⚙️ Slippage: {settings.slippage_pct:g}%\nPriority fee: {settings.priority_fee_sol} SOL\nJito: {'ON' if settings.jito_enabled else 'OFF'}",reply_markup=dashboard())
    if q.data=="sniper":return await q.answer("Auto-sniper is intentionally OFF in this build until explicit strategy/risk rules are configured.",show_alert=True)
    if q.data in {"buy_help","cancel"}:return await q.edit_message_text("Paste a Solana mint address to inspect and trade it.",reply_markup=dashboard())
    if parts[0]=="custom":context.user_data["custom_mint"]=parts[1];return await q.edit_message_text("Send the SOL amount, e.g. <code>0.25</code>.",parse_mode=ParseMode.HTML)
    if parts[0]=="buy":
        _,mint,amt=parts
        try:tx=await trader.execute(mint,"buy",amt)
        except TradeError as e:return await q.edit_message_text(f"❌ Trade failed: {html.escape(str(e))}")
        await q.edit_message_text(f"✅ Buy submitted\nMint: <code>{mint}</code>\nAmount: {amt} SOL\nTx: <code>{tx}</code>",parse_mode=ParseMode.HTML)

async def error_handler(update,context):log.exception("Unhandled error",exc_info=context.error)

def main():
    if not settings.bot_token:raise RuntimeError("BOT_TOKEN is required")
    if not settings.admin_id:raise RuntimeError("ADMIN_ID is required")
    init_db();app=Application.builder().token(settings.bot_token).build()
    app.add_handler(CommandHandler("start",start))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.TEXT&~filters.COMMAND,custom_amount),group=0)
    app.add_handler(MessageHandler(filters.TEXT&~filters.COMMAND,inspect_message),group=1)
    app.add_error_handler(error_handler);app.run_polling(drop_pending_updates=True)

if __name__=="__main__":main()
