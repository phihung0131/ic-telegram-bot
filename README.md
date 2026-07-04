# XAUUSD Telegram Signal -> IC Markets cTrader FIX API Bot

## Cài đặt
```bash
pip install -r requirements.txt
cp .env.example .env
# điền toàn bộ giá trị thật vào .env (đặc biệt CT_PASSWORD_DEMO, TELEGRAM_API_ID/HASH...)
python3 bot.py
```
Lần đầu chạy, Telethon sẽ hỏi số điện thoại + mã OTP để tạo file session
`session_ct_bot.session` (chỉ cần làm 1 lần).

## 4 rule xử lý tín hiệu (đã cấy vào `bot.py` + `signal_parser.py`)
1. Tin "Khớp..." (reply tới tín hiệu) → chỉ log, không hành động.
2. Tin chứa `GÓC NHÌN CÁ NHÂN: #XAUUSD` → đặt lệnh chờ (pending limit) tại vùng giá,
   SL lấy từ "Ngưỡng rủi ro", TP = entry ± `FIXED_TP_PIPS` (mặc định 100 pip).
3. Reply chứa "hủy"/"huỷ"/"điều chỉnh"/"đóng" tới 1 tin tín hiệu → nếu lệnh
   chưa khớp thì hủy lệnh chờ, nếu đã khớp thì đóng vị thế bằng market order
   ngược chiều (đồng thời hủy SL/TP đang treo).
4. Tin "hủy setup XXXX" (4 số, KHÔNG kèm reply) → quét toàn bộ lệnh đang
   PENDING/OPEN, tìm lệnh có entry gần XXXX nhất trong sai số
   `SETUP_MATCH_TOLERANCE` (mặc định ±5), rồi hủy/đóng lệnh đó. Không thấy
   thì bỏ qua.

Chỉ gửi Telegram notify (`send_telegram`) cho các sự kiện: vào lệnh khớp,
hủy lệnh, đóng lệnh, SL/TP hit, lỗi/reject, mất kết nối FIX, và tin khởi
động bot — đúng như yêu cầu, các tin chat linh tinh khác của admin không
notify.

## BẮT BUỘC PHẢI KIỂM TRA TRƯỚC KHI CHẠY REAL (rất quan trọng)

Phần FIX engine (`fix_engine.py`, `fix_trading.py`) được viết theo quy ước
FIX 4.4 phổ biến nhất, nhưng **mỗi broker có thể khác nhau ở vài chỗ**. Vì
mình không truy cập được tài liệu FIX API riêng theo tài khoản của bạn
(IC Markets thường gửi PDF riêng qua email khi bạn đăng ký FIX API), cần
bạn đối chiếu và chỉnh sửa các điểm sau **trên môi trường DEMO trước**:

| Điểm cần verify | Vị trí trong code | Cách kiểm tra |
|---|---|---|
| Tag Username/Password trong Logon (553/554) | `fix_engine.py::_send_logon` | Nếu Logon bị reject, xem tag 58 trong message Logout server trả về |
| Định danh Symbol (string "XAUUSD" hay ID số) | `.env` biến `CT_SYMBOL_ID` | Gửi `SecurityListRequest` (35=x) và đọc `SecurityList` (35=y) trả về, hoặc hỏi support IC Markets |
| OrdType Stop có cần thêm field nào khác ngoài tag 99 (StopPx) | `fix_trading.py::_place_bracket_orders` | Đặt thử 1 lệnh Stop nhỏ trên demo, xem ExecutionReport/Reject |
| Đóng vị thế bằng market order ngược chiều có work đúng không (netting vs hedging) | `fix_trading.py::close_open_position` | Test trên demo: mở BUY rồi thử đóng, kiểm tra account có về đúng flat không |
| Định dạng giá (mấy chữ số thập phân) | mọi nơi dùng tag 44/99 | XAUUSD trên IC Markets thường 2 chữ số thập phân, nhưng nên in ra để kiểm tra broker có reject vì sai precision không |

## Những gì CHƯA có (cân nhắc bổ sung sau khi test ổn):
- **Reconnect tự động** khi FIX session rớt kết nối (hiện tại chỉ log +
  notify cảnh báo, cần restart thủ công hoặc dùng supervisor/systemd
  `Restart=on-failure` để tự khởi động lại tiến trình).
- **Đồng bộ định kỳ** giữa `open_positions.json` và vị thế/lệnh thật trên
  server (giống `sync_positions_loop` trong bot crypto gốc) — nên bổ sung
  bằng cách định kỳ gửi `OrderStatusRequest`/`RequestForPositions`.
- **Sequence number persistence** qua các lần restart: hiện tại mỗi lần
  connect đều gửi `ResetSeqNumFlag=Y` (tag 141) để đơn giản hóa, một số
  broker yêu cầu duy trì đúng sequence number liên tục thay vì reset mỗi
  lần — nếu server phàn nàn, cần lưu seq_num ra file và bỏ tag 141.
- Giá lấy khi validate (giống bot crypto so sánh SL/TP với giá thị trường
  thực trước khi vào lệnh) — QUOTE session hiện mới chỉ Logon, chưa có
  `MarketDataRequest` để lấy giá real-time. Có thể bổ sung nếu bạn muốn có
  bước validate tương tự trước khi gửi lệnh.

## Khuyến nghị triển khai
1. Chạy trên **DEMO** ít nhất 1–2 tuần, theo dõi song song bằng tay để đối
   chiếu.
2. Chạy bằng `systemd` hoặc `pm2`/`supervisor` để tự restart khi crash.
3. Chỉ tăng `FIXED_VOLUME` lên mức thật khi đã tin tưởng vào độ chính xác
   của cả parser lẫn phần khớp lệnh SL/TP.
