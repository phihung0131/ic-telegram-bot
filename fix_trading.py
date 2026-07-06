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
    def __init__(self, quote_session, trade_session, symbol, symbol_id, volume, notify_fn):
        self.quote = quote_session
        self.trade = trade_session
        self.symbol = symbol
        self.symbol_id = symbol_id  # None nếu broker chấp nhận string symbol
        self.volume = volume
        self.notify = notify_fn  # hàm gửi Telegram (chỉ gọi cho sự kiện liên quan giao dịch)

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