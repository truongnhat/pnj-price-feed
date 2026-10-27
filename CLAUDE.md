# pnj-price-feed — quy tắc cho mọi thay đổi

## Bắt buộc khi đổi dữ liệu (thêm/bớt/đổi cột, đổi đơn vị, thêm nguồn)

Một thay đổi chỉ xong khi **mọi nơi dùng dữ liệu đó** đã được cập nhật và kiểm tra.
Trước khi báo xong, đi đủ checklist này:

1. **Lập danh sách nơi dùng** (impact list): `grep -rn "<tên cột/file>" scripts tests apps_script .github README.md`.
   Nơi dùng đã biết của `data/prices.csv` / `data/latest.csv`:
   | nơi dùng | file | người đọc thấy ở đâu |
   |---|---|---|
   | ghi dữ liệu | `scripts/fetch_prices.py` | CSV trên GitHub |
   | email báo cáo mỗi lượt | `scripts/notify_email.py` (`table_rows`, `TABLE_HEADERS`) | Gmail của No1 (nhãn PNJ Price Feed) |
   | tài liệu | `README.md` (bảng cột) | GitHub |
   | brief của Agent "Money" | đọc `data/latest.csv` qua raw.githubusercontent.com | brief PNJ hằng ngày |
2. **Cập nhật từng nơi** trong danh sách, hoặc ghi rõ vì sao nơi đó không cần đổi.
3. **Test hợp đồng cột**: `tests/test_schema_contract.py` buộc mọi cột trong `COLUMNS`
   phải hiện trong email hoặc nằm trong `EMAIL_HIDDEN_COLUMNS` (kèm lý do). Cột mới
   mà quên email → test đỏ → workflow không chạy tiếp.
4. **Kiểm tra trên bề mặt người dùng thấy**, không chỉ file: sau khi push, đợi lượt
   chạy xong và mở email mới nhất (Gmail, nhãn PNJ Price Feed) xác nhận nội dung mới có mặt.
5. **Báo cáo cho No1** có dòng "Đã cập nhật: …" liệt kê từng nơi trong danh sách ở bước 1.

## Khác
- Cột mới luôn **thêm vào cuối** `COLUMNS`; header cũ được migrate trong `read_csv`.
- Chạy `python -m unittest discover -s tests` trước khi push.
