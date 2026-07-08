"""
Lớp nghiệp vụ trading trên nền FIX engine (fix_engine.py).

Luồng bracket order (vì FIX chuẩn không có OCO/bracket built-in, phải tự quản):
  1. GÓC NHÌN CÁ NHÂN mới -> NewOrderSingle (ClOrdID = "<msg_id>-ENTRY"), OrdType=Limit
     tại giá entry.
  2. ExecutionReport ExecType=Trade (khớp) cho "<msg_id>-ENTRY"
     -> tự động gửi 2 lệnh:
        - Stop  (ClOrdID="<msg_id>-SL") tại giá SL, chiều ngược lại, đóng vị thế
        - Limit (ClOrdID="<msg_id>-TP") tại giá TP, chiều ngược lại, đóng vị thế
  3. Khi 1 trong 2 lệnh trên khớp -> hủy lệnh còn lại (OCO thủ công) + đánh dấu CLOSED.
  4. Khi có yêu cầu hủy/đóng thủ công (reply "hủy"/"điều chỉnh"/"đóng", hoặc "hủy setup XXXX"):
       - Nếu đang PENDING (chưa khớp)  -> OrderCancelRequest lệnh entry.
       - Nếu đang OPEN (đã khớp)       -> gửi market order ngược chiều để đóng vị thế,
                                           đồng thời hủy 2 lệnh SL/TP đang treo.

VERIFY TRƯỚC KHI DÙNG THẬT:
  - Định danh Symbol (tag 55): dùng string "XAUUSD" theo mặc định. Nếu broker
    yêu cầu Symbol ID dạng số, set CT_SYMBOL_ID trong .env, code sẽ ưu tiên dùng số đó.
  - OrdType Stop (tag 40 = "3") có thể cần thêm tag 99 (StopPx) - đã có trong code.
  - Với tài khoản dạng "hedging" (cho phép nhiều vị thế cùng symbol cùng lúc),
    việc đóng bằng lệnh market ngược chiều có thể cần thêm PositionID/PosMaintRptID
    cụ thể thay vì chỉ Symbol+Side - IC Markets cTrader thường dùng netting nên
    thường sẽ ổn, nhưng cần test kỹ trên demo.
"""

import itertools
import math
import threading
import time
from datetime import datetime, timezone

from logging_setup import logger
from fix_engine import FixMessage
import positions_store as store

_seq = itertools.count(1)


def _next_clordid_suffix():
    return f"{int(time.time() * 1000)}-{next(_seq)}"


def _transact_time():
    """Tag 60 (TransactTime) - bắt buộc trong NewOrderSingle/OrderCancelRequest."""
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H:%M:%S.%f")[:-3]


class TradingEngine:
    def __init__(self, quote_session, trade_session, symbol, symbol_id, volume, notify_fn,
                 trailing_enabled=True, trail_step=5.0, trail_drawdown=3.0,
                 trail_check_interval_sec=3):
        self.quote = quote_session
        self.trade = trade_session
        self.symbol = symbol
        self.symbol_id = symbol_id  # None nếu broker chấp nhận string symbol
        self.volume = volume
        self.notify = notify_fn  # hàm gửi Telegram (chỉ gọi cho sự kiện liên quan giao dịch)

        # ---- Trailing profit (xem docstring _check_trailing_for_position bên dưới) ----
        self.trailing_enabled = trailing_enabled
        self.trail_step = trail_step            # mỗi lần lời vượt thêm 1 mốc TRAIL_STEP -> dời SL/TP thêm TRAIL_STEP
        self.trail_drawdown = trail_drawdown    # tụt bao nhiêu so với ĐỈNH lời đã đạt thì đóng lệnh ngay
        self.trail_check_interval_sec = trail_check_interval_sec
        self.latest_bid = None
        self.latest_ask = None
        self._md_req_seq = itertools.count(1)
        self._trailing_thread_started = False

    def _tag55(self):
        return self.symbol_id if self.symbol_id else self.symbol

    # ------------------------------------------------------------------
    # ĐẶT LỆNH ENTRY (pending limit tại vùng giá tín hiệu)
    # ------------------------------------------------------------------
    def open_entry_order(self, msg_id: int, side: str, entry: float, sl: float, tp: float):
        clordid = f"{msg_id}-ENTRY"
        fix_side = "1" if side == "BUY" else "2"  # 1=Buy, 2=Sell

        msg = FixMessage()
        msg.append(11, clordid)          # ClOrdID
        msg.append(55, self._tag55())    # Symbol
        msg.append(54, fix_side)         # Side
        msg.append(38, self.volume)      # OrderQty
        msg.append(40, "2")              # OrdType = Limit
        msg.append(44, entry)            # Price
        msg.append(59, "1")              # TimeInForce = GTC
        msg.append(60, _transact_time()) # TransactTime (bắt buộc)

        logger.info(f"[{msg_id}] Gửi NewOrderSingle ENTRY: {side} {self.symbol} @ {entry} qty={self.volume}")

        try:
            self.trade.send_app_message(msg, "D")
        except Exception as e:
            logger.exception(f"[{msg_id}] Gửi lệnh ENTRY THẤT BẠI (lỗi socket/kết nối)")
            self.notify(
                f"🚨 <b>KHÔNG GỬI ĐƯỢC LỆNH VÀO</b>\n⏰ <code>{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}</code>\n"
                f"🪙 XAUUSD {side} @ {entry}\n"
                f"💥 Lỗi: <code>{e}</code>\n"
                f"⚠️ TRADE session có thể đã mất kết nối - cần kiểm tra/restart bot ngay"
            )
            return  # không lưu vào store vì lệnh chưa chắc đã tới broker

        store.store[msg_id] = {
            "status": "PENDING",
            "side": side,
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "order_ids": {"ENTRY": None, "SL": None, "TP": None},  # broker OrderID (tag 37), điền khi có ack
            # PositionID (tag 721) do broker cấp khi ENTRY khớp - BẮT BUỘC với tài khoản
            # Hedging: phải đính kèm tag này vào mọi lệnh SL/TP/đóng sau đó, nếu không
            # broker sẽ hiểu là mở vị thế MỚI thay vì thao tác trên vị thế đã có (xem
            # README/ghi chú ở đầu file này).
            "position_id": None,
        }
        store.save(store.store)

        # Notify NGAY khi gửi lệnh, không đợi khớp - để biết chắc bot đã hành động
        self.notify(
            f"📤 <b>ĐÃ GỬI LỆNH CHỜ (PENDING)</b>\n"
            f"🪙 XAUUSD {side} @ <code>{entry}</code>\n"
            f"🛑 SL: <code>{sl}</code>  🎯 TP: <code>{tp}</code>\n"
            f"📦 Volume: {self.volume}\n"
            f"⏳ Đang chờ khớp lệnh..."
        )

    # ------------------------------------------------------------------
    # KHI ENTRY KHỚP -> đặt SL + TP
    # ------------------------------------------------------------------
    def _place_bracket_orders(self, msg_id: int, pos: dict):
        side = pos["side"]
        close_side = "2" if side == "BUY" else "1"  # ngược chiều để đóng vị thế

        sl_msg = FixMessage()
        sl_msg.append(11, f"{msg_id}-SL")
        sl_msg.append(55, self._tag55())
        sl_msg.append(54, close_side)
        sl_msg.append(38, self.volume)
        sl_msg.append(40, "3")            # OrdType = Stop
        sl_msg.append(99, pos["sl"])      # StopPx
        sl_msg.append(59, "1")
        sl_msg.append(60, _transact_time())
        if pos.get("position_id"):
            sl_msg.append(721, pos["position_id"])  # PositionID - bắt buộc với tài khoản Hedging
        logger.info(f"[{msg_id}] Gửi Stop-Loss order tại {pos['sl']} (PositionID={pos.get('position_id')})")
        self.trade.send_app_message(sl_msg, "D")

        tp_msg = FixMessage()
        tp_msg.append(11, f"{msg_id}-TP")
        tp_msg.append(55, self._tag55())
        tp_msg.append(54, close_side)
        tp_msg.append(38, self.volume)
        tp_msg.append(40, "2")            # OrdType = Limit
        tp_msg.append(44, pos["tp"])      # Price
        tp_msg.append(59, "1")
        tp_msg.append(60, _transact_time())
        if pos.get("position_id"):
            tp_msg.append(721, pos["position_id"])  # PositionID - bắt buộc với tài khoản Hedging
        logger.info(f"[{msg_id}] Gửi Take-Profit order tại {pos['tp']} (PositionID={pos.get('position_id')})")
        self.trade.send_app_message(tp_msg, "D")

        pos["status"] = "OPEN"
        pos.setdefault("trail_level", 0)     # số mốc TRAIL_STEP đã trail rồi (0 = chưa dời lần nào)
        pos.setdefault("peak_profit", 0.0)   # lời (đơn vị giá) cao nhất từng đạt được, dùng để check tụt/drawdown
        store.save(store.store)

    # ------------------------------------------------------------------
    # HỦY LỆNH ENTRY CHƯA KHỚP
    # ------------------------------------------------------------------
    def cancel_pending_entry(self, msg_id: int, pos: dict):
        entry_order_id = pos["order_ids"].get("ENTRY")
        msg = FixMessage()
        msg.append(41, f"{msg_id}-ENTRY")           # OrigClOrdID
        msg.append(11, f"{msg_id}-ENTRY-CXL-{_next_clordid_suffix()}")  # ClOrdID mới
        if entry_order_id:
            msg.append(37, entry_order_id)          # OrderID (nếu đã có)
        # Broker này reject OrderCancelRequest nếu có bất kỳ field nào ngoài
        # OrigClOrdID/ClOrdID/OrderID (đã lần lượt bị reject tag 54, 55, 60) -
        # nên chỉ giữ lại 3 field tối thiểu này.
        logger.info(f"[{msg_id}] Gửi OrderCancelRequest cho lệnh entry chưa khớp")
        try:
            self.trade.send_app_message(msg, "F")
        except Exception as e:
            logger.exception(f"[{msg_id}] Gửi OrderCancelRequest THẤT BẠI (lỗi socket/kết nối)")
            self.notify(
                f"🚨 <b>KHÔNG GỬI ĐƯỢC LỆNH HỦY</b>\n🪙 XAUUSD {pos['side']} @ {pos['entry']}\n"
                f"💥 Lỗi: <code>{e}</code>\n⚠️ Lệnh CÓ THỂ vẫn còn treo trên sàn - kiểm tra thủ công ngay"
            )
            return

        # KHÔNG đánh dấu CANCELLED ngay ở đây - phải đợi broker xác nhận thật (ExecType=4)
        # trong on_execution_report. Lý do: nếu đúng lúc gửi hủy thì lệnh lại vừa khớp
        # (race condition), đánh dấu CANCELLED ngay sẽ khiến bot bỏ sót 1 vị thế đang mở
        # thật trên sàn mà không có SL/TP bảo vệ. Để trạng thái PENDING cho tới khi có
        # xác nhận rõ ràng (Cancelled hoặc Trade) từ broker.
        self.notify(
            f"⏳ <b>ĐÃ GỬI YÊU CẦU HỦY</b>\n🪙 XAUUSD {pos['side']} @ {pos['entry']}\n"
            f"📋 Đang chờ broker xác nhận..."
        )

    # ------------------------------------------------------------------
    # ĐÓNG VỊ THẾ ĐANG MỞ (đã khớp) + hủy SL/TP đang treo
    # ------------------------------------------------------------------
    def close_open_position(self, msg_id: int, pos: dict, reason: str):
        close_fix_side = "2" if pos["side"] == "BUY" else "1"

        for tag in ("SL", "TP"):
            order_id = pos["order_ids"].get(tag)
            cxl = FixMessage()
            cxl.append(41, f"{msg_id}-{tag}")
            cxl.append(11, f"{msg_id}-{tag}-CXL-{_next_clordid_suffix()}")
            if order_id:
                cxl.append(37, order_id)
            # CHỈ giữ OrigClOrdID/ClOrdID/OrderID trên Cancel - broker (IC Markets/cTrader)
            # REJECT thẳng nếu có tag 721 (PositionID) ở đây, dù nó bắt buộc trên
            # NewOrderSingle. Đã xác nhận qua Session Reject thực tế: "Tag not defined
            # for this message type, field=721" (RefMsgType=F). PositionID chỉ hợp lệ
            # khi TẠO lệnh (msg type D), không hợp lệ khi HỦY lệnh (msg type F).
            try:
                self.trade.send_app_message(cxl, "F")
                logger.info(f"[{msg_id}] Đã gửi hủy {tag} trước khi đóng vị thế (OrderID={order_id})")
            except Exception:
                logger.exception(f"[{msg_id}] Gửi hủy {tag} thất bại khi đóng vị thế (bỏ qua, vẫn thử đóng vị thế chính)")

        close_msg = FixMessage()
        close_msg.append(11, f"{msg_id}-CLOSE-{_next_clordid_suffix()}")
        close_msg.append(55, self._tag55())
        close_msg.append(54, close_fix_side)
        close_msg.append(38, self.volume)
        close_msg.append(40, "1")  # Market
        close_msg.append(59, "1")
        close_msg.append(60, _transact_time())
        if pos.get("position_id"):
            close_msg.append(721, pos["position_id"])  # PositionID - bắt buộc với tài khoản Hedging
        logger.info(f"[{msg_id}] Đóng vị thế bằng market order ngược chiều ({reason}) PositionID={pos.get('position_id')}")
        try:
            self.trade.send_app_message(close_msg, "D")
        except Exception as e:
            logger.exception(f"[{msg_id}] Gửi lệnh ĐÓNG VỊ THẾ THẤT BẠI (lỗi socket/kết nối)")
            self.notify(
                f"🚨 <b>KHÔNG GỬI ĐƯỢC LỆNH ĐÓNG</b>\n🪙 XAUUSD {pos['side']} @ {pos['entry']}\n"
                f"💥 Lỗi: <code>{e}</code>\n⚠️ VỊ THẾ VẪN CÒN MỞ trên sàn - cần đóng thủ công ngay"
            )
            return

        pos["status"] = "CLOSED"
        store.save(store.store)
        self.notify(
            f"🔒 <b>ĐÓNG LỆNH</b>\n🪙 XAUUSD {pos['side']} @ {pos['entry']}\n📋 Lý do: {reason}"
        )

    # ------------------------------------------------------------------
    # XỬ LÝ EXECUTION REPORT TỪ TRADE SESSION
    # ------------------------------------------------------------------
    def on_execution_report(self, fields: dict):
        clordid = fields.get("11", "")
        exec_type = fields.get("150")  # ExecType: 0=New,4=Cancelled,8=Rejected,F=Trade...
        ord_status = fields.get("39")
        order_id = fields.get("37")
        # IC Markets/cTrader thường trả giá khớp ở tag 6 (AvgPx) thay vì tag 31 (LastPx
        # chuẩn FIX) - ưu tiên 31 nếu có, fallback sang 6, để tránh hiện "None" trong log/notify.
        last_px = fields.get("31") or fields.get("6")

        logger.info(
            f"ExecutionReport nhận được: ClOrdID={clordid} ExecType={exec_type} "
            f"OrdStatus={ord_status} OrderID={order_id} LastPx={last_px}"
        )

        pos, msg_id = store.find_by_clordid_prefix(clordid)
        if pos is None:
            logger.warning(f"ExecutionReport không khớp với tín hiệu nào đang theo dõi. Full fields: {fields}")
            return

        tag = clordid.split("-")[1] if "-" in clordid else ""

        if exec_type == "0" and order_id:  # New - lưu lại OrderID broker cấp
            pos["order_ids"][tag] = order_id
            store.save(store.store)
            return

        if exec_type == "8":  # Rejected
            logger.error(f"[{msg_id}] Lệnh {tag} bị REJECT: {fields.get('58', '')}")
            self.notify(f"❌ <b>LỆNH BỊ TỪ CHỐI</b>\n🪙 XAUUSD {tag}\n💥 {fields.get('58', 'không rõ lý do')}")
            return

        if exec_type == "5":  # Replaced - broker xác nhận đã sửa giá SL/TP (trailing)
            if order_id:
                pos["order_ids"][tag] = order_id  # OrderID có thể đổi sau khi Replace, cập nhật lại
                store.save(store.store)
            logger.info(f"[{msg_id}] Broker xác nhận đã dời {tag} (trailing) thành công, OrderID mới={order_id}")
            return

        if exec_type == "F":  # Trade - khớp (toàn phần hoặc một phần)
            if tag == "ENTRY":
                fill_price = last_px or pos["entry"]
                position_id = fields.get("721")
                if position_id:
                    pos["position_id"] = position_id
                    logger.info(f"[{msg_id}] ENTRY khớp tại {fill_price}, PositionID={position_id}")
                else:
                    # Không có tag 721 trong ExecutionReport - broker này có thể không trả
                    # PositionID ở message Trade, hoặc tài khoản là Netting (không cần).
                    # Nếu là tài khoản Hedging thật, SL/TP/đóng lệnh sau đó sẽ KHÔNG đóng
                    # đúng vị thế mà mở vị thế mới - cần kiểm tra log ExecutionReport đầy đủ.
                    logger.warning(
                        f"[{msg_id}] ENTRY khớp tại {fill_price} nhưng KHÔNG có tag 721 (PositionID) "
                        f"trong ExecutionReport - nếu tài khoản là Hedging, SL/TP có thể mở vị thế mới "
                        f"thay vì đóng đúng vị thế này. Full fields: {fields}"
                    )
                store.save(store.store)
                self.notify(
                    f"✅ <b>VÀO LỆNH THÀNH CÔNG</b>\n"
                    f"🪙 XAUUSD {pos['side']}\n"
                    f"💰 Giá khớp: <code>{fill_price}</code>\n"
                    f"🛑 SL: <code>{pos['sl']}</code>  🎯 TP: <code>{pos['tp']}</code>"
                )
                self._place_bracket_orders(msg_id, pos)
            elif tag in ("SL", "TP"):
                logger.info(f"[{msg_id}] {tag} khớp tại {last_px} -> đóng vị thế, hủy lệnh còn lại")
                other = "TP" if tag == "SL" else "SL"
                other_order_id = pos["order_ids"].get(other)
                if not other_order_id:
                    logger.warning(
                        f"[{msg_id}] Chưa có OrderID cho lệnh {other} (chưa nhận New ack?) "
                        f"- vẫn thử gửi Cancel chỉ bằng ClOrdID, có thể bị broker reject"
                    )
                cxl = FixMessage()
                cxl.append(41, f"{msg_id}-{other}")
                cxl.append(11, f"{msg_id}-{other}-CXL-{_next_clordid_suffix()}")
                if other_order_id:
                    cxl.append(37, other_order_id)
                # CHỈ giữ OrigClOrdID/ClOrdID/OrderID - broker REJECT thẳng nếu có tag 721
                # (PositionID) trên Cancel (đã xác nhận qua Session Reject thực tế: "Tag not
                # defined for this message type, field=721", RefMsgType=F). PositionID chỉ
                # hợp lệ khi TẠO lệnh (msg type D), không hợp lệ khi HỦY (msg type F).
                try:
                    self.trade.send_app_message(cxl, "F")
                    logger.info(f"[{msg_id}] Đã gửi OrderCancelRequest hủy {other} (OrderID={other_order_id})")
                except Exception:
                    logger.exception(f"[{msg_id}] Gửi hủy {other} (OCO) thất bại sau khi {tag} khớp")
                    self.notify(
                        f"🚨 <b>KHÔNG HỦY ĐƯỢC LỆNH {other}</b>\n🪙 XAUUSD {pos['side']}\n"
                        f"⚠️ {tag} đã khớp nhưng lệnh {other} có thể vẫn còn treo trên sàn - kiểm tra thủ công"
                    )

                pos["status"] = "CLOSED"
                store.save(store.store)
                label = "🎯 TAKE PROFIT" if tag == "TP" else "🛑 STOP LOSS"
                self.notify(
                    f"{label} <b>HIT</b>\n🪙 XAUUSD {pos['side']}\n💰 Giá đóng: <code>{last_px}</code>"
                )
            return

        if exec_type == "4":  # Cancelled - broker XÁC NHẬN THẬT lệnh đã hủy
            if tag == "ENTRY" and pos["status"] == "PENDING":
                logger.info(f"[{msg_id}] Broker xác nhận đã hủy lệnh ENTRY")
                pos["status"] = "CANCELLED"
                store.save(store.store)
                self.notify(f"🚫 <b>ĐÃ HỦY LỆNH CHỜ</b>\n🪙 XAUUSD {pos['side']} @ {pos['entry']}\n📋 Broker đã xác nhận hủy")
            else:
                logger.info(f"[{msg_id}] Lệnh {tag} đã được broker xác nhận hủy")
            return

        # ExecType khác chưa được xử lý rõ ràng ở trên (vd broker trả về mã lạ,
        # partial fill kiểu khác...) - LOG LẠI để không mất dấu vết, tránh lặp lại
        # tình trạng "im lặng bỏ qua" từng gây khó chẩn đoán các lần trước.
        logger.warning(f"[{msg_id}] ExecutionReport ExecType={exec_type} chưa được xử lý cho lệnh {tag}. Full fields: {fields}")

    # ------------------------------------------------------------------
    # XỬ LÝ BUSINESS MESSAGE REJECT (35=j) - đặc biệt ORDER_NOT_FOUND khi hủy lệnh
    # ------------------------------------------------------------------
    def on_business_reject(self, fields: dict):
        """Khi gửi OrderCancelRequest cho 1 lệnh KHÔNG còn tồn tại trên broker (đã bị
        hủy/khớp/đóng thủ công ngoài bot từ trước), broker trả về ORDER_NOT_FOUND.
        Nếu không xử lý, store nội bộ sẽ mãi mãi coi lệnh đó là PENDING/OPEN, gây khớp
        nhầm cho các lần 'hủy setup XXXX' sau này. Đồng bộ lại trạng thái CANCELLED để
        dừng gây nhiễu."""
        reason_text = fields.get("58", "")
        ref_id = fields.get("379", "")
        if "ORDER_NOT_FOUND" not in reason_text:
            return
        parts = ref_id.split("-", 2)
        if len(parts) < 2:
            return
        try:
            msg_id = int(parts[0])
        except ValueError:
            return
        tag = parts[1]
        pos = store.store.get(msg_id)
        # CHỈ xử lý khi đang PENDING và liên quan tới ENTRY - KHÔNG BAO GIỜ tự động hạ
        # trạng thái OPEN xuống CANCELLED chỉ vì 1 cancel request cũ/lạc bị ORDER_NOT_FOUND,
        # vì vị thế OPEN có thể đang có SL/TP sống thật, đánh rớt theo dõi sẽ rất nguy hiểm.
        if not pos or pos["status"] != "PENDING" or tag != "ENTRY":
            return
        logger.warning(
            f"[{msg_id}] Lệnh không tồn tại trên broker (đã bị hủy/khớp/đóng thủ công "
            f"ngoài bot) - đồng bộ lại trạng thái CANCELLED để tránh khớp nhầm về sau"
        )
        pos["status"] = "CANCELLED"
        store.save(store.store)
        self.notify(
            f"⚠️ <b>ĐỒNG BỘ TRẠNG THÁI</b>\n🪙 XAUUSD {pos['side']} @ {pos['entry']}\n"
            f"📋 Lệnh không còn tồn tại trên sàn (có thể đã bị thao tác thủ công ngoài bot) "
            f"- bot đã cập nhật lại để tránh nhầm lẫn"
        )

    # ------------------------------------------------------------------
    # MARKET DATA (giá real-time trên QUOTE session) - cần cho trailing profit
    # ------------------------------------------------------------------
    def subscribe_market_data(self):
        """Gửi MarketDataRequest (35=V) subscribe Bid/Offer cho symbol, gọi 1 lần
        ngay sau khi QUOTE session Logon xong (xem bot.py::on_logon).
        VERIFY: cấu trúc group NoRelatedSym / MarketDepth có thể khác nhau tùy
        broker - nếu bị Reject hoặc không thấy message 35=W trả về, đối chiếu tài
        liệu FIX API riêng mà IC Markets gửi cho tài khoản của bạn."""
        if not self.trailing_enabled:
            return
        msg = FixMessage()
        msg.append(262, f"MD-{next(self._md_req_seq)}-{int(time.time())}")  # MDReqID
        msg.append(263, "1")   # SubscriptionRequestType = Snapshot + Updates
        msg.append(264, "1")   # MarketDepth = Top of book
        msg.append(267, "2")   # NoMDEntryTypes
        msg.append(269, "0")   # MDEntryType = Bid
        msg.append(269, "1")   # MDEntryType = Offer
        msg.append(146, "1")   # NoRelatedSym
        msg.append(55, self._tag55())
        logger.info(f"Gửi MarketDataRequest subscribe {self.symbol} (phục vụ trailing profit)")
        try:
            self.quote.send_app_message(msg, "V")
        except Exception:
            logger.exception(
                "Gửi MarketDataRequest thất bại - trailing sẽ KHÔNG hoạt động cho tới khi subscribe lại "
                "(vd sau khi QUOTE session reconnect)"
            )

    def on_market_data(self, raw: str):
        """Callback gắn vào QUOTE session (on_market_data), nhận message 35=W
        (Snapshot) hoặc 35=X (Incremental), cập nhật latest_bid/latest_ask.
        Dùng parse_pairs (giữ tag lặp) vì message MarketData có NHIỀU group entry
        cùng tag 269 (MDEntryType)/270 (MDEntryPx) - 1 cho Bid, 1 cho Offer."""
        cur_type = None
        for tag, value in FixMessage.parse_pairs(raw):
            if tag == "269":
                cur_type = value  # "0"=Bid, "1"=Offer, "2"=Trade (bỏ qua loại khác)
            elif tag == "270" and cur_type in ("0", "1"):
                try:
                    price = float(value)
                except ValueError:
                    continue
                if cur_type == "0":
                    self.latest_bid = price
                else:
                    self.latest_ask = price

    # ------------------------------------------------------------------
    # TRAILING PROFIT: dời SL/TP theo lời + đóng ngay nếu lời tụt sâu từ đỉnh
    # ------------------------------------------------------------------
    def _amend_order_price(self, msg_id: int, pos: dict, tag: str, new_price: float) -> bool:
        """Gửi OrderCancelReplaceRequest (35=G) để SỬA GIÁ lệnh SL/TP đang treo
        trên broker (không hủy-đặt-lại, mà sửa tại chỗ để giữ nguyên vị trí trong
        queue nếu broker hỗ trợ). Theo đúng convention đã dùng cho Cancel ở các hàm
        khác trong file này (cancel_pending_entry/close_open_position), OrigClOrdID
        luôn dùng ClOrdID GỐC "<msg_id>-<tag>", không dùng chain ClOrdID trung gian.

        VERIFY TRÊN DEMO TRƯỚC KHI BẬT TRAILING Ở REAL: một số broker yêu cầu đầy đủ
        Symbol(55)/Side(54)/OrderQty(38)/OrdType(40) kèm Price(44)/StopPx(99) trên
        Replace (khác Cancel thường chỉ cần OrigClOrdID/ClOrdID/OrderID) - đã điền đủ
        các field phổ biến nhất bên dưới, nhưng nếu bị Business Reject (35=j) thì đối
        chiếu tài liệu FIX API riêng của broker để biết field nào thiếu/thừa."""
        order_id = pos["order_ids"].get(tag)
        close_side = "2" if pos["side"] == "BUY" else "1"

        msg = FixMessage()
        msg.append(41, f"{msg_id}-{tag}")   # OrigClOrdID (ClOrdID gốc)
        msg.append(11, f"{msg_id}-{tag}-TRAIL-{_next_clordid_suffix()}")  # ClOrdID mới
        if order_id:
            msg.append(37, order_id)        # OrderID hiện tại (nếu đã biết)
        msg.append(55, self._tag55())
        msg.append(54, close_side)
        msg.append(38, self.volume)
        if tag == "SL":
            msg.append(40, "3")             # OrdType = Stop
            msg.append(99, new_price)       # StopPx
        else:  # TP
            msg.append(40, "2")             # OrdType = Limit
            msg.append(44, new_price)       # Price
        msg.append(59, "1")
        msg.append(60, _transact_time())

        logger.info(f"[{msg_id}] Trailing: dời {tag} -> {new_price}")
        try:
            self.trade.send_app_message(msg, "G")
            return True
        except Exception:
            logger.exception(
                f"[{msg_id}] Gửi OrderCancelReplaceRequest ({tag}) thất bại - bước trailing này "
                f"KHÔNG áp dụng được, SL/TP cũ trên broker vẫn còn hiệu lực"
            )
            return False

    def _check_trailing_for_position(self, msg_id: int, pos: dict):
        """
        Trailing profit (đơn vị: giá trực tiếp, giống quy ước TP_OFFSET của bot):
          - Lời hiện tại = khoảng cách giá thuận lợi so với entry, dùng giá CÓ THỂ
            đóng lệnh ngay (BUY: Bid - entry vì đóng BUY = bán tại Bid;
            SELL: entry - Ask vì đóng SELL = mua tại Ask).
          - Mỗi khi lời vượt thêm 1 mốc trail_step mới (5, 10, 15, ... theo trailing
            liên tục) so với mốc đã trail gần nhất -> dời CẢ SL và TP thêm đúng
            trail_step theo hướng có lợi, khóa dần lợi nhuận.
          - Theo dõi ĐỈNH lời cao nhất từng đạt (peak_profit). Nếu lời đã từng vượt
            mốc trail_step đầu tiên rồi sau đó TỤT xuống >= trail_drawdown so với
            đỉnh đó -> đóng lệnh NGAY bằng market order, không đợi SL/TP đang treo
            khớp (vì SL vừa dời có thể còn cách khá xa giá hiện tại / có độ trễ).
        """
        if pos.get("status") != "OPEN":
            return
        side = pos["side"]
        if side == "BUY":
            if self.latest_bid is None:
                return  # chưa nhận được giá nào từ QUOTE session, bỏ qua lượt check này
            profit = self.latest_bid - pos["entry"]
        else:
            if self.latest_ask is None:
                return
            profit = pos["entry"] - self.latest_ask

        peak = pos.get("peak_profit", 0.0)
        if profit > peak:
            peak = profit
            pos["peak_profit"] = peak
            store.save(store.store)

        # --- 1) Trailing liên tục: dời SL/TP thêm 1 (hoặc nhiều) nấc trail_step ---
        trail_level = pos.get("trail_level", 0)
        new_level = math.floor(profit / self.trail_step) if profit >= self.trail_step else 0
        if new_level > trail_level:
            shift = (new_level - trail_level) * self.trail_step
            direction = 1 if side == "BUY" else -1
            new_sl = round(pos["sl"] + direction * shift, 2)
            new_tp = round(pos["tp"] + direction * shift, 2)
            ok_sl = self._amend_order_price(msg_id, pos, "SL", new_sl)
            ok_tp = self._amend_order_price(msg_id, pos, "TP", new_tp)
            if ok_sl:
                pos["sl"] = new_sl
            if ok_tp:
                pos["tp"] = new_tp
            pos["trail_level"] = new_level
            store.save(store.store)
            self.notify(
                f"📈 <b>TRAILING - DỜI SL/TP</b>\n🪙 XAUUSD {side}\n"
                f"💰 Lời hiện tại: <code>{round(profit, 2)}</code> (mốc #{new_level})\n"
                f"🛑 SL mới: <code>{new_sl}</code>  🎯 TP mới: <code>{new_tp}</code>"
            )

        # --- 2) Bảo vệ lợi nhuận: lời tụt sâu so với đỉnh đã đạt -> đóng ngay ---
        if peak >= self.trail_step and (peak - profit) >= self.trail_drawdown:
            logger.info(
                f"[{msg_id}] Lời tụt từ đỉnh {round(peak, 2)} xuống {round(profit, 2)} "
                f"(>= drawdown {self.trail_drawdown}) -> đóng lệnh bảo vệ lợi nhuận"
            )
            self.close_open_position(
                msg_id, pos,
                reason=f"Trailing: lời tụt từ đỉnh {round(peak, 2)} xuống {round(profit, 2)}, đóng bảo vệ lợi nhuận"
            )

    def start_trailing_monitor(self):
        """Khởi động thread nền kiểm tra trailing định kỳ. Gọi 1 lần khi bot khởi
        động (sau khi tạo TradingEngine), tự bỏ qua nếu trailing bị tắt hoặc đã chạy rồi."""
        if not self.trailing_enabled or self._trailing_thread_started:
            return
        self._trailing_thread_started = True
        threading.Thread(target=self._trailing_loop, daemon=True, name="trailing-monitor").start()
        logger.info(
            f"Trailing profit monitor đã bật: mỗi {self.trail_step} điểm lời dời SL/TP thêm "
            f"{self.trail_step}, đóng ngay nếu lời tụt {self.trail_drawdown} điểm từ đỉnh "
            f"(check mỗi {self.trail_check_interval_sec}s)"
        )

    def _trailing_loop(self):
        while True:
            time.sleep(self.trail_check_interval_sec)
            try:
                # list(...) để chụp snapshot danh sách msg_id trước, tránh lỗi "dict
                # changed size during iteration" do thread Telegram có thể thêm lệnh
                # mới vào store.store cùng lúc.
                for msg_id in list(store.store.keys()):
                    pos = store.store.get(msg_id)
                    if pos and pos.get("status") == "OPEN":
                        self._check_trailing_for_position(msg_id, pos)
            except Exception:
                logger.exception("Lỗi trong vòng lặp trailing monitor (bỏ qua, thử lại ở lần check kế tiếp)")