# Hướng dẫn cài đặt — Docker / Linux

## Mục lục

1. [Tổng quan](#1-tổng-quan)
2. [Cài đặt Docker](#2-cài-đặt-docker)
3. [Cấu hình docker-compose](#3-cấu-hình-docker-compose)
4. [Khởi động](#4-khởi-động)
5. [Quản lý](#5-quản-lý)
6. [Build image tùy chỉnh](#6-build-image-tùy-chỉnh)
7. [Production deployment](#7-production-deployment)
8. [Cron job trong Docker](#8-cron-job-trong-docker)
9. [Troubleshooting](#9-troubleshooting)

---

## 1. Tổng quan

Docker tách biệt ứng dụng khỏi hệ thống, đảm bảo hoạt động nhất quán trên mọi môi trường.

### Kiến trúc

```
┌──────────────────────────────────────┐
│          Docker Compose               │
│                                      │
│  ┌────────────┐    ┌───────────────┐  │
│  │  bds-agent │    │  PostgreSQL   │  │
│  │  (API +   │───▶│  16 + vector  │  │
│  │  Crawler) │    │  + pg_trgm   │  │
│  └────────────┘    └───────────────┘  │
│        │                               │
│        │ :8000                        │
│        ▼                               │
│   Host Browser                         │
└──────────────────────────────────────┘
```

---

## 2. Cài đặt Docker

### macOS

```bash
brew install --cask docker
# Hoặc tải Docker Desktop từ https://docker.com
```

### Linux (Ubuntu/Debian)

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -D /etc/apt/keyrings/docker.gpg
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
    | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) \
    signed-by=/etc/apt/keyrings/docker.gpg] \
    https://download.docker.com/linux/ubuntu \
    $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    | sudo tee /etc/apt/sources.list.d/docker.list
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

### Linux (CentOS/RHEL)

```bash
sudo yum install -y yum-utils
sudo yum-config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
sudo yum install -y docker-ce docker-ce-cli containerd.io
sudo systemctl start docker
sudo systemctl enable docker
```

---

## 3. Cấu hình docker-compose

### 3.1 Tạo `.env`

```bash
cp .env.example .env
```

Sửa `.env`:

```env
# Database — dùng container PostgreSQL
DATABASE_URL=postgresql://bds:bds_password@postgres:5432/bds_agent

# LLM (OpenRouter)
OPENAI_API_KEY=sk-or-v1-xxxxxxxxxxxxxxxxxxxx

# Telegram
TELEGRAM_BOT_TOKEN=123456789:ABCdefGHI...
TELEGRAM_CHAT_ID=987654321
TELEGRAM_ENABLED=false

# Docker-specific
PYTORCH_MPS_HIGH_WATERMARK_RATIO=0.0
PYTHONUNBUFFERED=1
```

### 3.2 Tùy chỉnh docker-compose.yml

```yaml
services:
  bds:
    build: .
    container_name: bds-agent
    restart: unless-stopped
    ports:
      - "8000:8000"
    environment:
      - DATABASE_URL=${DATABASE_URL}
      - TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}
      - TELEGRAM_CHAT_ID=${TELEGRAM_CHAT_ID}
      - TELEGRAM_ENABLED=${TELEGRAM_ENABLED}
      - OPENAI_API_KEY=${OPENAI_API_KEY}
      - PYTHONUNBUFFERED=1
      - PYTORCH_MPS_HIGH_WATERMARK_RATIO=0.0
    volumes:
      - ./browser_profile_desktop:/app/browser_profile_desktop
      - bds-data:/app/data
    depends_on:
      postgres:
        condition: service_healthy

  postgres:
    image: pgvector/pgvector:pg16
    container_name: bds-postgres
    restart: unless-stopped
    environment:
      - POSTGRES_USER=bds
      - POSTGRES_PASSWORD=bds_password
      - POSTGRES_DB=bds_agent
    volumes:
      - postgres-data:/var/lib/postgresql/data
      - ./init-db.sql:/docker-entrypoint-initdb.d/init.sql:ro
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U bds -d bds_agent"]
      interval: 5s
      timeout: 5s
      retries: 5

volumes:
  postgres-data:
  bds-data:
```

### 3.3 Tạo `init-db.sql`

```bash
cat > init-db.sql << 'EOF'
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS vector;
EOF
```

---

## 4. Khởi động

### 4.1 Build và chạy

```bash
# Build image
docker compose build

# Khởi động (chạy nền)
docker compose up -d

# Xem log real-time
docker compose logs -f

# Kiểm tra trạng thái
docker compose ps
```

### 4.2 Chạy migration

```bash
# Vào container
docker compose exec bds python migrations/run.py

# Hoặc một lệnh
docker compose exec bds python -c "
from db import connect_db, get_database_url, ensure_schema
from psycopg import connect as pg_connect
url = 'postgresql://bds:bds_password@postgres:5432/bds_agent'
with pg_connect(url) as conn:
    from db import ensure_schema
    ensure_schema(conn)
print('Schema OK')
"
```

### 4.3 Kiểm tra

```bash
# Health check
curl http://localhost:8000/api/stats

# Mở giao diện
open http://localhost:8000
```

---

## 5. Quản lý

### Lệnh thường dùng

```bash
# Stop
docker compose stop

# Start lại
docker compose start

# Restart
docker compose restart

# Xem logs
docker compose logs -f bds

# Shell vào container
docker compose exec bds /bin/bash

# Stop và xóa
docker compose down

# Xóa toàn bộ (bao gồm data)
docker compose down -v
```

### Backup database

```bash
# Backup
docker compose exec postgres pg_dump -U bds bds_agent > backup_$(date +%Y%m%d).sql

# Restore
cat backup_20260101.sql | docker compose exec -T postgres psql -U bds bds_agent
```

### Update

```bash
git pull
docker compose build --no-cache
docker compose up -d
```

---

## 6. Build image tùy chỉnh

### Build với arguments

```bash
# Build không cache (fresh dependencies)
docker build --no-cache -t bds-agent:latest .

# Build với Python version tùy chỉnh
docker build --build-arg PYTHON_VERSION=3.12 -t bds-agent:py312 .
```

### Registry

```bash
# Tag
docker tag bds-agent:latest your-registry.com/bds-agent:latest

# Push
docker push your-registry.com/bds-agent:latest

# Pull trên server
docker pull your-registry.com/bds-agent:latest
```

---

## 7. Production deployment

### 7.1 Server requirements

| Thành phần | Khuyến nghị |
|------------|-------------|
| CPU | 2 vCPUs+ |
| RAM | 4 GB+ |
| Disk | 20 GB+ SSD |
| OS | Ubuntu 22.04 LTS |

### 7.2 Deploy script

```bash
#!/bin/bash
# deploy.sh
set -e

IMAGE="your-registry.com/bds-agent:latest"
CONTAINER="bds-agent"

echo "Pulling latest image..."
docker pull $IMAGE

echo "Stopping old container..."
docker stop $CONTAINER || true
docker rm $CONTAINER || true

echo "Starting new container..."
docker run -d \
  --name $CONTAINER \
  --restart unless-stopped \
  -p 8000:8000 \
  --env-file .env \
  --volume bds-profile:/app/browser_profile_desktop \
  $IMAGE

echo "Done!"
docker compose logs -f --tail=20 $CONTAINER
```

### 7.3 Caddy reverse proxy (HTTPS tự động)

```yaml
# Caddyfile
bds.yourdomain.com {
    reverse_proxy localhost:8000
    encode gzip
}
```

### 7.4 Systemd service (Linux)

```bash
cat > /etc/systemd/system/bds-agent.service << 'EOF'
[Unit]
Description=BDS Agent
Requires=docker.service
After=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/opt/bds-agent
ExecStart=/usr/local/bin/docker compose up -d
ExecStop=/usr/local/bin/docker compose stop
TimeoutStartSec=0

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable bds-agent
sudo systemctl start bds-agent
```

---

## 8. Cron job trong Docker

### Cách 1: Dùng Docker cron container

```yaml
# docker-compose.yml bổ sung
services:
  bds-cron:
    build: .
    container_name: bds-cron
    restart: unless-stopped
    command: >
      /bin/sh -c "
        echo '0 */4 * * * python facebook_group_scraper.py --headless --use-db-cookies --use-db-groups --scroll-rounds 16 --workers 2 >> /var/log/crawl.log 2>&1'
        > /etc/crontabs/root
      && cron -f
    volumes:
      - ./bds-data:/app
    environment:
      - DATABASE_URL=${DATABASE_URL}
      - PYTHONUNBUFFERED=1
    depends_on:
      bds:
        condition: service_started
```

### Cách 2: Host cron gọi Docker

```bash
# /etc/cron.d/bds-crawl
0 */4 * * * root /usr/local/bin/docker exec bds-agent python facebook_group_scraper.py --headless >> /var/log/bds-crawl.log 2>&1
```

---

## 9. Troubleshooting

### Container không start được

```bash
# Xem log chi tiết
docker compose logs bds

# Kiểm tra lỗi config
docker compose config --quiet
```

### Database connection failed

```bash
# Kiểm tra postgres
docker compose logs postgres

# Test kết nối từ container
docker compose exec bds python -c "import psycopg; print(psycopg.connect('${DATABASE_URL}')); print('OK')"
```

### Playwright browser lỗi trong container

Chromium cần thêm flags để chạy trong Docker:

```yaml
services:
  bds:
    environment:
      - PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
      - PYTHONUNBUFFERED=1
    command: >
      python -m uvicorn api_app:app --host 0.0.0.0 --port 8000
      --extra-args="--no-sandbox --disable-setuid-sandbox"
```

### Disk full

```bash
# Xem disk usage
docker system df

# Dọn
docker system prune -af --volumes
```

### Port bị chiếm

```bash
# Tìm process
sudo lsof -ti:8000

# Giết process
sudo kill -9 $(sudo lsof -ti:8000)
```
