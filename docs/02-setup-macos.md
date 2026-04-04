# Hướng dẫn cài đặt — macOS

## Mục lục

1. [Cài đặt dependencies](#1-cài-đặt-dependencies)
2. [Cài đặt PostgreSQL](#2-cài-đặt-postgresql)
3. [Cấu hình project](#3-cấu-hình-project)
4. [Chạy lần đầu](#4-chạy-lần-đầu)
5. [Sử dụng CLI](#5-sử-dụng-cli)
6. [Tự khởi động cùng hệ thống](#6-tự-khởi-động-cùng-hệ-thống)
7. [Troubleshooting](#7-troubleshooting)

---

## 1. Cài đặt Dependencies

### 1.1 Clone project

```bash
git clone https://github.com/tanle25/hrm_crawl_bds.git
cd bds-agent/crawl4ai
```

### 1.2 Python

macOS thường có sẵn Python 3.x. Tạo virtual environment:

```bash
# macOS 13+ thường có Python 3.11
python3 --version

# Tạo venv với Python 3.14 (khuyến nghị)
/usr/local/bin/python3.14 -m venv .venv314
source .venv314/bin/activate

# Cài dependencies
pip install -r requirements.txt
```

### 1.3 Node.js (cho Playwright)

```bash
brew install node
```

### 1.4 Playwright browsers

```bash
playwright install chromium
playwright install-deps chromium
```

---

## 2. Cài đặt PostgreSQL

### 2.1 Cài qua Homebrew

```bash
brew install postgresql@16
brew services start postgresql@16
```

### 2.2 Tạo database

```bash
# Kết nối
psql -U $(whoami) -d postgres

# Tạo user và database
CREATE USER bds WITH PASSWORD 'bds_password';
CREATE DATABASE bds_agent OWNER bds;
GRANT ALL PRIVILEGES ON DATABASE bds_agent TO bds;

# Thoát
\q
```

### 2.3 Kích hoạt extension

```bash
psql -U bds -d bds_agent -c "CREATE EXTENSION IF NOT EXISTS pg_trgm;"
psql -U bds -d bds_agent -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

---

## 3. Cấu hình project

### 3.1 Tạo `.env`

```bash
cp .env.example .env
```

Sửa `.env`:

```env
# Database
DATABASE_URL=postgresql://bds:bds_password@localhost:5432/bds_agent

# LLM (OpenRouter — miễn phí tier)
OPENAI_API_KEY=sk-or-v1-xxxxxxxxxxxxxxxxxxxx

# Telegram (tùy chọn)
TELEGRAM_BOT_TOKEN=123456789:ABCdefGHI...
TELEGRAM_CHAT_ID=987654321
TELEGRAM_ENABLED=false
```

### 3.2 Chạy migration

```bash
python migrations/run.py
```

Output mẫu:

```
Applied: 0001_initial
Applied: add_classification_fields
Applied: 0002_cookies_and_groups
```

### 3.3 Thêm cookies và groups

**Qua giao diện web** (sau khi chạy API):

1. Mở http://localhost:8000 → Settings → Cookies
2. Paste cookies JSON từ browser DevTools
3. Nhấn Save

**Qua CLI:**

```bash
# Thêm cookie profile
python -c "
from db import connect_db, get_database_url, ensure_schema, upsert_facebook_cookie
import json
with open('cookies.json') as f:
    raw = json.load(f)
cookies = raw.get('cookies', raw) if isinstance(raw, dict) else raw
url = get_database_url(None)
with connect_db(url) as conn:
    ensure_schema(conn)
    cid = upsert_facebook_cookie(conn, 'default', cookies)
    print(f'Cookie id={cid}')
"

# Thêm groups
python -c "
from db import connect_db, get_database_url, ensure_schema, upsert_facebook_group
url = get_database_url(None)
with connect_db(url) as conn:
    ensure_schema(conn)
    with open('facebook_groups.txt') as f:
        for line in f:
            url = line.strip()
            if not url or url.startswith('#'):
                continue
            gid = upsert_facebook_group(conn, url=url)
            print(f'Group {gid}: {url}')
"
```

---

## 4. Chạy lần đầu

### 4.1 Khởi động API

```bash
# Cách 1: CLI (khuyến nghị)
./bds start

# Kiểm tra
./bds status
./bds log

# Mở trình duyệt
open http://localhost:8000
```

### 4.2 Validate cookie

Sau khi mở giao diện:

1. **Settings → Cookies** → nhấn 🔄 Validate
2. Cookie phải hiển thị **🟢 Alive**

### 4.3 Test crawl thử

**Qua CLI:**

```bash
./bds crawl
```

**Qua giao diện:**

1. **Settings → Crawler** → chọn groups
2. Nhấn **Bắt đầu Crawl ngay**

---

## 5. Sử dụng CLI

Sau khi thêm vào PATH (xem bên dưới):

```bash
# Quản lý server
bds start      # Khởi động API
bds stop       # Dừng server
bds restart    # Restart
bds status     # Kiểm tra trạng thái
bds log        # Xem log (30 dòng cuối)
bds log 100    # Xem 100 dòng cuối

# Chạy tools
bds crawl      # Chạy scraper 1 lần
bds enrich     # Chạy enricher 1 lần
bds telegram   # Test Telegram notification
```

---

## 6. Tự khởi động cùng hệ thống

### 6.1 Thêm vào PATH

```bash
# Mở ~/.zshrc
nano ~/.zshrc

# Thêm dòng này vào cuối:
export PATH="$PATH:/path/to/bds-agent/crawl4ai"
```

Áp dụng ngay:

```bash
source ~/.zshrc
```

Giờ dùng `bds` từ mọi thư mục.

### 6.2 LaunchAgent (chạy kể cả khi sleep)

```bash
cat > ~/Library/LaunchAgents/com.bds.agent.plist << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.bds.agent</string>
  <key>ProgramArguments</key>
  <array>
    <string>/path/to/bds-agent/crawl4ai/.venv314/bin/python</string>
    <string>-m</string>
    <string>uvicorn</string>
    <string>api_app:app</string>
    <string>--host</string>
    <string>0.0.0.0</string>
    <string>--port</string>
    <string>8000</string>
  </array>
  <key>WorkingDirectory</key>
  <string>/path/to/bds-agent/crawl4ai</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <dict>
    <key>SuccessfulExit</key>
    <false/>
  </dict>
  <key>StandardOutPath</key>
  <string>/tmp/bds.log</string>
  <key>StandardErrorPath</key>
  <string>/tmp/bds.err</string>
</dict>
</plist>
EOF
```

Kích hoạt:

```bash
launchctl load ~/Library/LaunchAgents/com.bds.agent.plist
launchctl start com.bds.agent
```

---

## 7. Troubleshooting

### Không cài được `pg` (psycopg)

```bash
brew install postgresql@16
# Thêm vào PATH:
echo 'export PATH="/usr/local/opt/postgresql@16/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
pip install psycopg[binary]
```

### Playwright browser không tìm thấy

```bash
playwright install chromium
# Nếu lỗi quyền:
sudo xattr -r -d com.apple.quarantine ~/Library/Caches/ms-playwright
```

### Không kết nối được PostgreSQL

```bash
# Kiểm tra PostgreSQL đang chạy
brew services list | grep postgresql

# Restart
brew services restart postgresql@16
```

### Cookie bị die sau vài ngày

Facebook thường expire session sau 1-2 tuần. Vào **Settings → Cookies** → nhấn 🔄 **Validate** thường xuyên.

### API chậm hoặc timeout

- Giảm `--workers` trong Settings → Crawler
- Giảm tần suất cron crawl
- Kiểm tra log: `bds log`
