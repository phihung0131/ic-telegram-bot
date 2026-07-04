"""
Lưu trạng thái các tín hiệu/lệnh đang theo dõi, key = telegram message_id của
tin "GÓC NHÌN CÁ NHÂN" gốc.

status: PENDING (đã đặt lệnh chờ, chưa khớp) / OPEN (đã khớp, đang có vị thế)
        / CLOSED / CANCELLED
"""
import json
import os
from logging_setup import logger

STORE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "open_positions.json")


def load():
    try:
        with open(STORE_FILE, "r", encoding="utf-8") as f:
            data = {int(k): v for k, v in json.load(f).items()}
            logger.info(f"Loaded {len(data)} tín hiệu đang theo dõi từ {STORE_FILE}")
            return data
    except FileNotFoundError:
        logger.info(f"{STORE_FILE} chưa tồn tại, khởi tạo rỗng")
        return {}
    except json.JSONDecodeError as e:
        logger.warning(f"{STORE_FILE} lỗi JSON ({e}), khởi tạo rỗng")
        return {}


def save(store: dict):
    try:
        with open(STORE_FILE, "w", encoding="utf-8") as f:
            json.dump(store, f, indent=2, ensure_ascii=False)
    except Exception:
        logger.exception(f"Lỗi khi ghi {STORE_FILE}")


store = load()


def find_by_clordid_prefix(clordid: str):
    """ClOrdID luôn có dạng '<msg_id>-ENTRY' / '-SL' / '-TP' / '-CLOSE'."""
    try:
        msg_id_str, tag = clordid.rsplit("-", 1)
        msg_id = int(msg_id_str)
    except ValueError:
        return None, None
    return store.get(msg_id), msg_id


def find_open_matching_price(target_price: float, tolerance: float):
    """Dùng cho rule 'hủy setup XXXX' - tìm lệnh PENDING/OPEN có entry gần target nhất.
    Khi nhiều lệnh có cùng khoảng cách giá (vd nhiều tín hiệu test trùng entry), ưu tiên
    lệnh có msg_id LỚN HƠN (tín hiệu MỚI NHẤT) thay vì lệnh cũ nhất gặp đầu tiên - tránh
    khớp nhầm vào tín hiệu cũ/đã không còn tồn tại thật trên broker."""
    best_id, best_pos, best_diff = None, None, None
    for msg_id, pos in store.items():
        if pos["status"] not in ("PENDING", "OPEN"):
            continue
        diff = abs(pos["entry"] - target_price)
        if diff > tolerance:
            continue
        if best_diff is None or diff < best_diff or (diff == best_diff and msg_id > best_id):
            best_id, best_pos, best_diff = msg_id, pos, diff
    return best_id, best_pos