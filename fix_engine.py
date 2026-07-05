"""
FIX 4.4 engine mức thấp, tự build/parse message qua raw TCP socket (SSL).

Không dùng QuickFix để tránh phụ thuộc build C++ phức tạp khi deploy VPS -
đổi lại phải tự quản lý sequence number, heartbeat, test request thủ công.

QUAN TRỌNG: phần Logon (tag 553/554 Username/Password, SenderSubID, ...) là
theo quy ước phổ biến nhất của FIX API IC Markets/cTrader. Hãy đối chiếu với
tài liệu FIX 4.4 chính thức mà IC Markets gửi cho tài khoản của bạn trước
khi chạy - nếu sai tag, Logon sẽ bị server reject (nhận message 35=5 Logout
kèm lý do trong tag 58).
"""

import socket
import ssl
import threading
import time
from datetime import datetime, timezone

from logging_setup import logger

SOH = "\x01"


class FixMessage:
    def __init__(self):
        self.fields = []  # list[(tag:str, value:str)], giữ đúng thứ tự thêm vào

    def append(self, tag, value):
        self.fields.append((str(tag), str(value)))
        return self

    def build(self, sender_comp_id, target_comp_id, sender_sub_id, target_sub_id, seq_num, msg_type):
        header_fields = [("35", msg_type), ("49", sender_comp_id), ("56", target_comp_id)]
        if sender_sub_id:
            header_fields.append(("50", sender_sub_id))
        if target_sub_id:
            header_fields.append(("57", target_sub_id))
        header_fields.append(("34", str(seq_num)))
        header_fields.append(("52", datetime.now(timezone.utc).strftime("%Y%m%d-%H:%M:%S.%f")[:-3]))

        body_fields = header_fields + self.fields
        body = SOH.join(f"{t}={v}" for t, v in body_fields) + SOH
        head = f"8=FIX.4.4{SOH}9={len(body)}{SOH}"
        no_checksum = head + body
        checksum = sum(no_checksum.encode("ascii")) % 256
        return no_checksum + f"10={checksum:03d}{SOH}"

    @staticmethod
    def parse(raw: str) -> dict:
        out = {}
        for part in raw.split(SOH):
            if not part or "=" not in part:
                continue
            tag, _, value = part.partition("=")
            out[tag] = value
        return out


class FixSession:
    def __init__(self, name, host, port, sender_comp_id, target_comp_id, sender_sub_id,
                 password, account, use_ssl=True, heartbeat_interval=30, on_message=None,
                 on_logon=None, on_disconnect=None, target_sub_id=None, reconnect_delay_sec=30):
        self.name = name
        self.host = host
        self.port = port
        self.sender_comp_id = sender_comp_id
        self.target_comp_id = target_comp_id
        self.sender_sub_id = sender_sub_id
        # cTrader FIX API yêu cầu TargetSubID = cùng giá trị QUOTE/TRADE như SenderSubID
        self.target_sub_id = target_sub_id if target_sub_id is not None else sender_sub_id
        self.password = password
        self.account = account
        self.use_ssl = use_ssl
        self.heartbeat_interval = heartbeat_interval
        self.on_message = on_message
        self.on_logon = on_logon
        self.on_disconnect = on_disconnect
        # Số giây chờ giữa các lần tự động kết nối lại khi mất kết nối ngoài ý muốn
        # (không áp dụng khi tự chủ động gọi close(), vd lúc bot tắt bình thường)
        self.reconnect_delay_sec = reconnect_delay_sec

        self.sock = None
        self.seq_num = 1
        self.running = False
        self.logged_on = False
        self._recv_buf = ""
        self._lock = threading.Lock()
        self._intentional_close = False  # True khi close() được gọi chủ động -> không tự reconnect

    # ---------------------------------------------------------------
    def connect(self):
        # Dọn socket cũ (nếu có, vd từ lần reconnect trước) tránh rò rỉ file descriptor
        if self.sock is not None:
            try:
                self.sock.close()
            except Exception:
                pass
        self._intentional_close = False
        self._recv_buf = ""
        self.seq_num = 1  # server luôn nhận ResetSeqNumFlag=Y ở Logon nên reset về 1 là an toàn
        self.logged_on = False

        raw_sock = socket.create_connection((self.host, self.port), timeout=15)
        if self.use_ssl:
            ctx = ssl.create_default_context()
            self.sock = ctx.wrap_socket(raw_sock, server_hostname=self.host)
        else:
            self.sock = raw_sock
        self.sock.settimeout(None)  # bỏ timeout kết nối, để recv() block vô thời hạn giữa các heartbeat
        logger.info(f"[{self.name}] TCP{'+SSL' if self.use_ssl else ''} connected {self.host}:{self.port}")

        self.running = True
        threading.Thread(target=self._recv_loop, daemon=True, name=f"fix-recv-{self.name}").start()
        threading.Thread(target=self._heartbeat_loop, daemon=True, name=f"fix-hb-{self.name}").start()

        self._send_logon()

    def _send_logon(self):
        msg = FixMessage()
        msg.append(98, 0)             # EncryptMethod = None
        msg.append(108, self.heartbeat_interval)  # HeartBtInt
        msg.append(141, "Y")          # ResetSeqNumFlag
        msg.append(553, self.account) # Username - VERIFY: có broker dùng account number, có broker dùng login riêng
        msg.append(554, self.password)
        self._send(msg, "A")

    def _send(self, msg: FixMessage, msg_type: str):
        with self._lock:
            wire = msg.build(self.sender_comp_id, self.target_comp_id, self.sender_sub_id,
                              self.target_sub_id, self.seq_num, msg_type)
            self.sock.sendall(wire.encode("ascii"))
            logger.debug(f"[{self.name}] >> {wire.replace(SOH, '|')}")
            self.seq_num += 1

    def send_app_message(self, msg: FixMessage, msg_type: str):
        if not self.logged_on:
            logger.warning(f"[{self.name}] Chưa Logon xong nhưng vẫn gửi {msg_type} - có thể bị reject")
        self._send(msg, msg_type)

    # ---------------------------------------------------------------
    def _heartbeat_loop(self):
        while self.running:
            time.sleep(self.heartbeat_interval)
            if not self.running:
                break
            try:
                self._send(FixMessage(), "0")
            except Exception:
                logger.exception(f"[{self.name}] Lỗi gửi heartbeat")
                self.running = False

    def _recv_loop(self):
        while self.running:
            try:
                chunk = self.sock.recv(4096)
                if not chunk:
                    logger.warning(f"[{self.name}] Server đóng kết nối")
                    break
                self._recv_buf += chunk.decode("ascii", errors="replace")
                self._drain_buffer()
            except (socket.timeout, TimeoutError):
                # Không có dữ liệu trong khoảng thời gian ngắn - không phải lỗi, thử lại
                continue
            except Exception:
                logger.exception(f"[{self.name}] Lỗi vòng lặp nhận dữ liệu")
                break
        self.running = False
        self.logged_on = False
        if self.on_disconnect:
            try:
                self.on_disconnect(self.name)
            except Exception:
                logger.exception(f"[{self.name}] Lỗi trong on_disconnect callback")

        if not self._intentional_close:
            self._schedule_reconnect()

    def _schedule_reconnect(self):
        """Retry vô hạn, cách nhau reconnect_delay_sec giây, cho tới khi connect() thành công.
        Chạy trong thread riêng để không chặn/không cần recv loop cũ."""
        def _attempt():
            time.sleep(self.reconnect_delay_sec)
            logger.info(f"[{self.name}] Đang thử kết nối lại sau {self.reconnect_delay_sec}s...")
            try:
                self.connect()
                logger.info(f"[{self.name}] Kết nối lại thành công (đang chờ Logon ack)")
            except Exception:
                logger.exception(f"[{self.name}] Kết nối lại thất bại, sẽ thử tiếp")
                self._schedule_reconnect()

        threading.Thread(target=_attempt, daemon=True, name=f"fix-reconnect-{self.name}").start()

    def _drain_buffer(self):
        while True:
            start = self._recv_buf.find("8=FIX")
            if start == -1:
                self._recv_buf = ""
                return
            checksum_idx = self._recv_buf.find(SOH + "10=", start)
            if checksum_idx == -1:
                return
            end = self._recv_buf.find(SOH, checksum_idx + 1)
            if end == -1:
                return
            raw_msg = self._recv_buf[start:end + 1]
            self._recv_buf = self._recv_buf[end + 1:]
            fields = FixMessage.parse(raw_msg)
            logger.debug(f"[{self.name}] << {raw_msg.replace(SOH, '|')}")
            self._handle_incoming(fields)

    def _handle_incoming(self, fields: dict):
        msg_type = fields.get("35")

        if msg_type == "A":  # Logon ack
            self.logged_on = True
            logger.info(f"[{self.name}] Logon thành công")
            if self.on_logon:
                self.on_logon(self.name)
            return

        if msg_type == "1":  # TestRequest -> phải trả Heartbeat kèm TestReqID (112)
            hb = FixMessage()
            if "112" in fields:
                hb.append(112, fields["112"])
            self._send(hb, "0")
            return

        if msg_type == "5":  # Logout
            logger.warning(f"[{self.name}] Nhận Logout từ server: {fields.get('58', '(không rõ lý do)')}")
            self.running = False
            return

        if msg_type == "3":  # Reject
            logger.error(f"[{self.name}] Server REJECT message: {fields}")

        if self.on_message:
            try:
                self.on_message(self.name, msg_type, fields)
            except Exception:
                logger.exception(f"[{self.name}] Lỗi trong on_message callback")

    def close(self):
        self._intentional_close = True
        self.running = False
        try:
            self._send(FixMessage(), "5")
            self.sock.close()
        except Exception:
            pass