import os
import asyncio
import requests
from datetime import datetime
from dotenv import load_dotenv
from telethon import TelegramClient, events

load_dotenv()

from logging_setup import logger
import positions_store as store
import signal_parser as sp
from fix_engine import FixSession
from fix_trading import TradingEngine

# ====================== CONFIG ======================
BOT_NOTIFY_TOKEN = os.getenv("BOT_NOTIFY_TOKEN")
NOTIFY_CHAT_ID = os.getenv("NOTIFY_CHAT_ID")
TELEGRAM_API_ID = int(os.getenv("TELEGRAM_API_ID"))
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH")
TARGET_GROUP_ID = int(os.getenv("TARGET_GROUP_ID"))
HEALTHCHECK_CHANNEL_ID = int(os.getenv("HEALTHCHECK_CHANNEL_ID")) if os.getenv("HEALTHCHECK_CHANNEL_ID") else None

CT_SANDBOX_MODE = os.getenv("CT_SANDBOX_MODE", "True").lower() == "true"
_SUF = "_DEMO" if CT_SANDBOX_MODE else "_REAL"

CT_HOST = os.getenv(f"CT_HOST{_SUF}")
CT_QUOTE_PORT = int(os.getenv(f"CT_QUOTE_PORT{_SUF}"))
CT_TRADE_PORT = int(os.getenv(f"CT_TRADE_PORT{_SUF}"))
CT_SENDER_COMPID = os.getenv(f"CT_SENDER_COMPID{_SUF}")
CT_TARGET_COMPID = os.getenv(f"CT_TARGET_COMPID{_SUF}", "cServer")
CT_PASSWORD = os.getenv(f"CT_PASSWORD{_SUF}")
CT_ACCOUNT = os.getenv(f"CT_ACCOUNT{_SUF}")
CT_USE_SSL = os.getenv("CT_USE_SSL", "True").lower() == "true"
CT_HEARTBEAT_SEC = int(os.getenv("CT_HEARTBEAT_SEC", 30))
CT_RECONNECT_DELAY_SEC = int(os.getenv("CT_RECONNECT_DELAY_SEC", 5))

CT_SYMBOL = os.getenv("CT_SYMBOL", "XAUUSD")
CT_SYMBOL_ID = os.getenv("CT_SYMBOL_ID") or None
# TP = entry ± TP_OFFSET (đơn vị: giá trực tiếp, KHÔNG phải pips) - vd TP_OFFSET=10 nghĩa là
# TP cách entry đúng 10.0 (giống hệt cách bạn đọc số trên chart), dễ hiểu và ít nhầm hơn
# kiểu tính pips*pip_size cũ.
TP_OFFSET = float(os.getenv("TP_OFFSET", 10))
FIXED_VOLUME = float(os.getenv("FIXED_VOLUME", 0.01))
SETUP_MATCH_TOLERANCE = float(os.getenv("SETUP_MATCH_TOLERANCE", 5))

# ---- TRAILING PROFIT (dời SL/TP theo lời + đóng ngay nếu lời tụt sâu) ----
# Đơn vị: giá trực tiếp, giống TP_OFFSET (KHÔNG phải pips/tiền thật).
CT_TRAILING_ENABLED = os.getenv("CT_TRAILING_ENABLED", "True").lower() == "true"
CT_TRAIL_STEP = float(os.getenv("CT_TRAIL_STEP", 5))
CT_TRAIL_DRAWDOWN = float(os.getenv("CT_TRAIL_DRAWDOWN", 3))
CT_TRAIL_CHECK_INTERVAL_SEC = int(os.getenv("CT_TRAIL_CHECK_INTERVAL_SEC", 3))

BOT_START_TIME = datetime.now()
last_signal_message_at = None
processed_message_ids = set()


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def send_telegram(text: str):
    """CHỈ gọi hàm này cho các sự kiện liên quan giao dịch (vào lệnh/hủy/đóng/SL-TP hit/lỗi)."""
    try:
        url = f"https://api.telegram.org/bot{BOT_NOTIFY_TOKEN}/sendMessage"
        resp = requests.post(url, json={
            "chat_id": NOTIFY_CHAT_ID, "text": text,
            "parse_mode": "HTML", "disable_web_page_preview": True,
        }, timeout=10)
        if not resp.ok:
            logger.warning(f"Telegram notify lỗi ({resp.status_code}): {resp.text}")
    except Exception:
        logger.exception("Telegram notify exception")


# ====================== FIX SESSIONS ======================
def on_trade_message(session_name, msg_type, fields):
    if msg_type == "8":  # ExecutionReport
        trading_engine.on_execution_report(fields)
    elif msg_type == "9":  # OrderCancelReject
        logger.warning(f"OrderCancelReject: {fields}")
        send_telegram(
            f"❌ <b>HỦY/SỬA LỆNH THẤT BẠI</b>\n⏰ <code>{now()}</code>\n"
            f"💥 Lý do: {fields.get('58', 'không rõ')}\n"
            f"🆔 ClOrdID: <code>{fields.get('11', '')}</code>"
        )
    elif msg_type == "3":  # Reject (session-level, ví dụ thiếu field bắt buộc)
        logger.error(f"[TRADE] Session Reject: {fields}")
        send_telegram(
            f"❌ <b>LỆNH BỊ SERVER TỪ CHỐI (SESSION REJECT)</b>\n⏰ <code>{now()}</code>\n"
            f"💥 Lý do: {fields.get('58', 'không rõ')}\n"
            f"🏷️ Tag thiếu/lỗi: <code>{fields.get('371', '?')}</code>  "
            f"RefMsgType: <code>{fields.get('372', '?')}</code>"
        )
    elif msg_type == "j":  # BusinessMessageReject - lệnh bị từ chối ở mức business (vd sai Symbol, sai field...)
        logger.error(f"[TRADE] Business Message Reject: {fields}")
        send_telegram(
            f"❌ <b>LỆNH BỊ TỪ CHỐI (BUSINESS REJECT)</b>\n⏰ <code>{now()}</code>\n"
            f"💥 Lý do: {fields.get('58', 'không rõ')}\n"
            f"🆔 RefID: <code>{fields.get('379', '?')}</code>  "
            f"RefMsgType: <code>{fields.get('372', '?')}</code>  "
            f"Reason code: <code>{fields.get('380', '?')}</code>"
        )
        trading_engine.on_business_reject(fields)
    elif msg_type == "0":
        # Heartbeat - bình thường, không cần log/notify gì thêm
        pass
    else:
        # Bất kỳ message type nào chưa được xử lý rõ ràng -> vẫn log để không mất dấu vết,
        # tránh lặp lại tình trạng "im lặng thất bại" như Business Reject (35=j) trước đây.
        logger.warning(f"[TRADE] Nhận message type chưa xử lý: 35={msg_type} | fields={fields}")


def on_logon(session_name):
    logger.info(f"Session {session_name} sẵn sàng")
    if session_name == "QUOTE":
        # Subscribe giá real-time ngay khi QUOTE session Logon xong (kể cả sau
        # reconnect) - cần cho trailing profit tính lời nổi liên tục.
        trading_engine.subscribe_market_data()


def on_disconnect(session_name):
    logger.error(f"Session {session_name} bị mất kết nối - cần cơ chế reconnect (xem README)")
    send_telegram(f"⚠️ <b>MẤT KẾT NỐI FIX ({session_name})</b>\n⏰ <code>{now()}</code>\nBot sẽ không vào/đóng lệnh được cho tới khi kết nối lại.")


quote_session = FixSession(
    name="QUOTE", host=CT_HOST, port=CT_QUOTE_PORT,
    sender_comp_id=CT_SENDER_COMPID, target_comp_id=CT_TARGET_COMPID, sender_sub_id="QUOTE",
    password=CT_PASSWORD, account=CT_ACCOUNT, use_ssl=CT_USE_SSL,
    heartbeat_interval=CT_HEARTBEAT_SEC, on_logon=on_logon, on_disconnect=on_disconnect,
    reconnect_delay_sec=CT_RECONNECT_DELAY_SEC,
    on_market_data=lambda raw: trading_engine.on_market_data(raw),
)

trade_session = FixSession(
    name="TRADE", host=CT_HOST, port=CT_TRADE_PORT,
    sender_comp_id=CT_SENDER_COMPID, target_comp_id=CT_TARGET_COMPID, sender_sub_id="TRADE",
    password=CT_PASSWORD, account=CT_ACCOUNT, use_ssl=CT_USE_SSL,
    heartbeat_interval=CT_HEARTBEAT_SEC, on_message=on_trade_message,
    on_logon=on_logon, on_disconnect=on_disconnect,
    reconnect_delay_sec=CT_RECONNECT_DELAY_SEC,
)

trading_engine = TradingEngine(
    quote_session=quote_session, trade_session=trade_session,
    symbol=CT_SYMBOL, symbol_id=CT_SYMBOL_ID, volume=FIXED_VOLUME, notify_fn=send_telegram,
    trailing_enabled=CT_TRAILING_ENABLED, trail_step=CT_TRAIL_STEP,
    trail_drawdown=CT_TRAIL_DRAWDOWN, trail_check_interval_sec=CT_TRAIL_CHECK_INTERVAL_SEC,
)


# ====================== XỬ LÝ TÍN HIỆU THEO 4 RULE ======================
def handle_new_signal(msg_id: int, text: str):
    parsed = sp.parse_new_signal(text)
    if not parsed:
        logger.warning(f"[{msg_id}] Có 'GÓC NHÌN CÁ NHÂN #XAUUSD' nhưng parse thất bại, bỏ qua")
        return
    side, entry, sl = parsed["side"], parsed["entry"], parsed["sl"]
    tp = entry + TP_OFFSET if side == "BUY" else entry - TP_OFFSET
    tp = round(tp, 2)
    trading_engine.open_entry_order(msg_id, side, entry, sl, tp)


def handle_reply_cancel(reply_to_id: int, text: str):
    pos = store.store.get(reply_to_id)
    if not pos:
        logger.debug(f"Reply hủy/đóng/điều chỉnh tới msg_id={reply_to_id} nhưng không tracking, bỏ qua")
        return
    if pos["status"] == "PENDING":
        trading_engine.cancel_pending_entry(reply_to_id, pos)
    elif pos["status"] == "OPEN":
        trading_engine.close_open_position(reply_to_id, pos, reason=f"Admin yêu cầu: {text[:100]}")
    else:
        logger.debug(f"msg_id={reply_to_id} đã ở trạng thái {pos['status']}, không cần xử lý thêm")


def handle_setup_cancel(target_price: float):
    msg_id, pos = store.find_open_matching_price(target_price, SETUP_MATCH_TOLERANCE)
    if not pos:
        logger.info(f"'hủy setup {target_price}' - không tìm thấy lệnh nào khớp giá gần đó, bỏ qua")
        return
    if pos["status"] == "PENDING":
        trading_engine.cancel_pending_entry(msg_id, pos)
    else:
        trading_engine.close_open_position(msg_id, pos, reason=f"Hủy setup theo giá {target_price}")


# ====================== TELEGRAM LISTENER ======================
client = TelegramClient("session_ct_bot", TELEGRAM_API_ID, TELEGRAM_API_HASH)


@client.on(events.NewMessage(chats=TARGET_GROUP_ID))
async def handler(event):
    msg_id = event.message.id
    if msg_id in processed_message_ids:
        return
    processed_message_ids.add(msg_id)
    if len(processed_message_ids) > 500:
        processed_message_ids.clear()

    global last_signal_message_at
    last_signal_message_at = datetime.now()

    text = event.raw_text or ""
    logger.info(f"Nhận tin msg_id={msg_id}: {text[:120].replace(chr(10), ' ')}")

    loop = asyncio.get_event_loop()

    async def _run_safe(fn, *args):
        """Chạy hàm xử lý trong executor, bắt MỌI exception để không bị Telethon nuốt mất
        (nếu không có try/except này, lỗi socket/kết nối khi gửi FIX sẽ crash âm thầm,
        không log rõ ràng, không có Telegram cảnh báo)."""
        try:
            await loop.run_in_executor(None, fn, *args)
        except Exception:
            logger.exception(f"msg_id={msg_id}: Lỗi không xử lý được khi chạy {fn.__name__}")
            send_telegram(
                f"🚨 <b>BOT LỖI KHI XỬ LÝ TÍN HIỆU</b>\n⏰ <code>{now()}</code>\n"
                f"🆔 msg_id: <code>{msg_id}</code>\n"
                f"⚙️ Hàm: <code>{fn.__name__}</code>\n"
                f"⚠️ Kiểm tra log ngay - lệnh có thể chưa được gửi/hủy/đóng đúng"
            )

    # Rule 3: reply chứa hủy/điều chỉnh/đóng tới 1 tin GÓC NHÌN CÁ NHÂN
    if event.message.reply_to:
        replied_id = event.message.reply_to.reply_to_msg_id
        if sp.is_cancel_reply(text):
            await _run_safe(handle_reply_cancel, replied_id, text)
            return
        # Rule 1 (info, bỏ qua): tin "Khớp xxxx" reply tới tín hiệu -> không cần hành động
        if sp.is_filled_info(text):
            logger.debug(f"msg_id={msg_id}: tin KHỚP LỆNH thông tin, bỏ qua")
            return
        # reply khác không khớp rule nào -> bỏ qua
        return

    # Rule 4: "hủy setup XXXX" không kèm reply
    setup_price = sp.parse_setup_cancel(text)
    if setup_price is not None:
        await _run_safe(handle_setup_cancel, setup_price)
        return

    # Rule 2: tín hiệu mới
    if sp.is_new_signal(text):
        await _run_safe(handle_new_signal, msg_id, text)
        return

    # Các tin khác (chào hỏi, tin tức, tổng kết ngày...) -> không xử lý, không notify


# ====================== HEALTHCHECK (tuỳ chọn) ======================
if HEALTHCHECK_CHANNEL_ID:
    @client.on(events.NewMessage(chats=HEALTHCHECK_CHANNEL_ID))
    async def healthcheck_handler(event):
        if event.raw_text.strip() != "/ic":
            return
        uptime = datetime.now() - BOT_START_TIME
        idle = f"{int((datetime.now() - last_signal_message_at).total_seconds() // 60)} phút trước" if last_signal_message_at else "chưa có tin nào"
        price_info = (
            f"Bid {trading_engine.latest_bid} / Ask {trading_engine.latest_ask}"
            if trading_engine.latest_bid is not None else "chưa nhận được giá"
        )
        await event.reply(
            f"✅ <b>BOT ĐANG SỐNG</b>\n"
            f"🌐 Mode: <b>{'DEMO 🧪' if CT_SANDBOX_MODE else 'REAL 💰'}</b>\n"
            f"⏳ Uptime: {str(uptime).split('.')[0]}\n"
            f"📡 QUOTE: {'connected' if quote_session.logged_on else 'DISCONNECTED ⚠️'} | "
            f"TRADE: {'connected' if trade_session.logged_on else 'DISCONNECTED ⚠️'}\n"
            f"💹 Giá {CT_SYMBOL}: {price_info}\n"
            f"📈 Trailing: {'BẬT' if CT_TRAILING_ENABLED else 'TẮT'}\n"
            f"📨 Tin gần nhất: {idle}\n"
            f"📂 Đang theo dõi: {len(store.store)} tín hiệu",
            parse_mode="html",
        )


async def main():
    mode = "DEMO 🧪" if CT_SANDBOX_MODE else "REAL 💰"
    logger.info(f"Bot khởi động - Mode: {mode} - Host: {CT_HOST}")

    quote_session.connect()
    trade_session.connect()
    await asyncio.sleep(3)  # chờ Logon 2 session trước khi nhận tín hiệu (on_logon sẽ tự subscribe market data)

    trading_engine.start_trailing_monitor()

    trailing_info = (
        f"📈 Trailing: mỗi +{CT_TRAIL_STEP} lời dời SL/TP, đóng ngay nếu tụt {CT_TRAIL_DRAWDOWN} từ đỉnh"
        if CT_TRAILING_ENABLED else "📈 Trailing: TẮT"
    )
    send_telegram(
        f"🤖 <b>BOT XAUUSD KHỞI ĐỘNG</b>\n⏰ <code>{now()}</code>\n"
        f"🌐 Mode: <b>{mode}</b>\n"
        f"📡 QUOTE: {'OK' if quote_session.logged_on else 'LỖI'} | TRADE: {'OK' if trade_session.logged_on else 'LỖI'}\n"
        f"💵 Volume/lệnh: {FIXED_VOLUME} | TP cố định: entry ± {TP_OFFSET}\n"
        f"{trailing_info}"
    )

    await client.start()
    logger.info("Telegram client kết nối thành công, bắt đầu lắng nghe tín hiệu")
    await client.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot dừng bởi KeyboardInterrupt")
    except Exception:
        logger.exception("Bot crash ở top-level")
        raise