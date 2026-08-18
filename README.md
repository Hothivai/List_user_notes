# NFT App - Token List with User Notes

Trang web quản lý danh sách token (PancakeSwap, Aerodrome, Solana) kèm tính năng ghi chú cá nhân (User Notes).

## 🚀 Tính năng chính
- Hiển thị danh sách token kèm Chain, Contract Address, Symbol, PERPETUAL.
- **User Note:** Nhập và lưu ghi chú cho từng token (Tự động lưu khi nhấn Enter, rời chuột, hoặc nút Save).


## 🛠️ Cài đặt và chạy (Copy & Paste 1 lần duy nhất)

Mở **Terminal** (hoặc CMD/PowerShell), sau đó **copy và dán toàn bộ đoạn code dưới đây** vào đó, rồi nhấn Enter:

```bash
# 1. Clone code về máy
cd nft_app_project

# 2. Tạo môi trường ảo và kích hoạt (Windows)
python -m venv venv
venv\Scripts\activate

# Nếu bạn dùng Mac/Linux, hãy thay 2 dòng trên bằng:
# python3 -m venv venv
# source venv/bin/activate

# 3. Tạo file requirements.txt
echo flask > requirements.txt
echo pymysql >> requirements.txt

# 4. Cài dependencies từ requirements.txt
pip install -r requirements.txt

# 5. Tạo file .env chứa cấu hình database

# 6. Chạy ứng dụng
python app.py
http://localhost:5000/list_token_notes