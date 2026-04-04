# Hướng dẫn cài đặt — Windows

## Mục lục

1. [Cài đặt dependencies](#1-cài-đặt-dependencies)
2. [Cài đặt PostgreSQL](#2-cài-đặt-postgresql)
3. [Cấu hình project](#3-cấu-hình-project)
4. [Chạy lần đầu](#4-chạy-lần-đầu)
5. [Sử dụng CLI](#5-sử-dụng-cli)
6. [Tự khởi động cùng hệ thống](#6-tự-khởi-động-cùng-hệ-thống-windows-task-scheduler)
7. [Troubleshooting](#7-troubleshooting)

---

## 1. Cài đặt Dependencies

### 1.1 Clone project

```cmd
git clone https://github.com/tanle25/hrm_crawl_bds.git
cd bds-agent\crawl4ai
```

### 1.2 Python

Tải Python 3.14 từ [python.org](https://www.python.org/downloads/).

Khi cài đặt, tick **"Add Python to PATH"**.

Kiểm tra:

```cmd
python --version
```

### 1.3 Tạo Virtual Environment

```cmd
python -m venv .venv314
.venv314\Scripts\activate
```

### 1.4 Cài dependencies

```cmd
pip install -r requirements.txt
```

### 1.5 Node.js (cho Playwright)

Tải từ [nodejs.org](https://nodejs.org) (phiên bản LTS).

### 1.6 Playwright browsers

Mở **PowerShell** (Run as Administrator):

```powershell
playwright install chromium
playwright install-deps chromium
```

---

## 2. Cài đặt PostgreSQL

### 2.1 Tải và cài đặt

Tải PostgreSQL 16 từ [postgresql.org](https://www.postgresql.org/download/windows/) hoặc dùng [Chocolatey](https://chocolatey.org/):

```powershell
choco install postgresql --version=16 -y
```

### 2.2 Khởi động PostgreSQL

```powershell
# Khởi động service
Start-Service postgresql-x64-16

# Hoặc dùng pgAdmin để quản lý
```

### 2.3 Tạo database

Mở **pgAdmin** hoặc **psql**:

```sql
CREATE USER bds WITH PASSWORD 'bds_password';
CREATE DATABASE bds_agent OWNER bds;
```

### 2.4 Kích hoạt extension

```sql
\c bds_agent
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS vector;
```

---

## 3. Cấu hình Project

### 3.1 Tạo `.env`

```cmd
copy .env.example .env
```

Mở `.env` bằng Notepad hoặc VS Code:

```env
# Database (dùng password đã tạo ở bước 2.3)
DATABASE_URL=postgresql://bds:bds_password@localhost:5432/bds_agent

# LLM (OpenRouter)
OPENAI_API_KEY=sk-or-v1-xxxxxxxxxxxxxxxxxxxx

# Telegram (tùy chọn)
TELEGRAM_BOT_TOKEN=123456789:ABCdefGHI...
TELEGRAM_CHAT_ID=987654321
TELEGRAM_ENABLED=false
```

### 3.2 Chạy migration

```cmd
python migrations\run.py
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
2. Paste cookies JSON
3. Nhấn Save

**Qua CLI:**

```cmd
python -c "from db import connect_db, get_database_url, ensure_schema, upsert_facebook_cookie; import json; raw=json.load(open('cookies.json')); cookies=raw.get('cookies',raw) if isinstance(raw,dict) else raw; url=get_database_url(None); conn=connect_db(url); ensure_schema(conn); cid=upsert_facebook_cookie(conn,'default',cookies); print(f'Cookie id={cid}')"
```

---

## 4. Chạy lần đầu

### 4.1 Khởi động API

```cmd
# Cách 1: CLI
python bds.py start

# Kiểm tra
python bds.py status

# Mở trình duyệt
start http://localhost:8000
```

### 4.2 Validate cookie

1. **Settings → Cookies** → nhấn 🔄 Validate
2. Cookie phải hiển thị **🟢 Alive**

### 4.3 Test crawl thử

```cmd
python bds.py crawl
```

---

## 5. Sử dụng CLI

### Cách 1: Dùng `python bds.py`

```cmd
cd C:\path\to\bds-agent\crawl4ai
python bds.py start
python bds.py status
python bds.py log
python bds.py crawl
python bds.py enrich
python bds.py telegram
```

### Cách 2: Dùng `bds.bat` (thêm vào PATH)

Thêm vào **System Environment Variables**:

```
Variable: PATH
Value: C:\path\to\bds-agent\crawl4ai
```

Sau đó mở CMD/PowerShell mới, gõ `bds.bat start`.

---

## 6. Tự khởi động cùng hệ thống (Windows Task Scheduler)

### 6.1 Mở Task Scheduler

```powershell
taskschd.msc
```

### 6.2 Tạo Task mới

1. **Action** → **Create Basic Task**
2. **Name**: `BDS Agent`
3. **Trigger**: `When the computer starts`
4. **Action**: `Start a program`
5. **Program**: `C:\path\to\bds-agent\crawl4ai\.venv314\Scripts\python.exe`
6. **Arguments**: `-m uvicorn api_app:app --host 0.0.0.0 --port 8000`
7. **Start in**: `C:\path\to\bds-agent\crawl4ai`
8. Tick **"Run whether user is logged on or not"**
9. **OK** → nhập password admin

### 6.3 Auto-crawl định kỳ

Tạo thêm scheduled task:

```powershell
$action = New-ScheduledTaskAction -Execute "C:\path\to\bds-agent\crawl4ai\bds.bat" -Argument "crawl"
$trigger = New-ScheduledTaskTrigger -Daily -At "08:00"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -Action $action -Trigger $trigger -TaskName "BDS Crawl Daily" -Settings $settings -Description "BDS Crawl Daily 8h"
```

---

## 7. Troubleshooting

### Lỗi `pg` (psycopg) không cài được

```powershell
pip uninstall psycopg
pip install psycopg-binary
```

### Playwright không tìm thấy browser

```powershell
playwright install chromium
# Thử cài thủ công:
python -m playwright install chromium
```

### PostgreSQL không kết nối được

```powershell
# Kiểm tra service
Get-Service | Where-Object {$_.Name -like "*postgres*"}

# Restart
Restart-Service postgresql-x64-16
```

### Lỗi `uvicorn` không chạy

```powershell
pip install uvicorn fastapi
python -m uvicorn api_app:app --reload
```

### Port 8000 bị chiếm

```powershell
netstat -ano | findstr :8000
taskkill /PID <PID> /F
```
