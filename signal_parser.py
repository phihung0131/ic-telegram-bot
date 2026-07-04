import re

NEW_SIGNAL_RE = re.compile(
    r"GÓC NHÌN CÁ NHÂN.*?#XAUUSD.*?"
    r"Vùng (?P<zone_type>hỗ trợ|kháng cự) quan trọng:\s*(?P<entry>[\d.]+).*?"
    r"Ngưỡng rủi ro:\s*(?P<sl>[\d.]+)",
    re.DOTALL,
)

# "hủy", "huỷ" (2 cách gõ dấu hỏi/nặng đều có thể gặp), "điều chỉnh", "đóng"
CANCEL_KEYWORDS_RE = re.compile(r"h[uủ][yỷ]|điều chỉnh|đóng", re.IGNORECASE)

# "hủy setup 4148" / "huỷ setup 4148" - không kèm reply
SETUP_CANCEL_RE = re.compile(r"h[uủ][yỷ]\s*setup\s*(\d{3,5})", re.IGNORECASE)

FILLED_RE = re.compile(r"Khớp\s*([\d.]+)", re.IGNORECASE)


def is_new_signal(text: str) -> bool:
    return "GÓC NHÌN CÁ NHÂN" in text and "#XAUUSD" in text


def parse_new_signal(text: str):
    m = NEW_SIGNAL_RE.search(text)
    if not m:
        return None
    zone_type = m.group("zone_type")
    entry = float(m.group("entry"))
    sl = float(m.group("sl"))
    side = "BUY" if zone_type == "hỗ trợ" else "SELL"
    return {"side": side, "entry": entry, "sl": sl}


def is_cancel_reply(text: str) -> bool:
    return bool(CANCEL_KEYWORDS_RE.search(text))


def is_filled_info(text: str) -> bool:
    """Tin 'Khớp 4464.0' - chỉ mang tính thông tin, không cần hành động."""
    return bool(FILLED_RE.search(text))


def parse_setup_cancel(text: str):
    """Trả về số giá (float) nếu là tin 'hủy setup XXXX' không kèm reply, ngược lại None."""
    m = SETUP_CANCEL_RE.search(text)
    if not m:
        return None
    return float(m.group(1))
