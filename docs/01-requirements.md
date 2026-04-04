# Yêu cầu hệ thống

## Phần cứng

| Thành phần | Tối thiểu | Khuyến nghị |
|------------|-----------|-------------|
| RAM | 4 GB | 8 GB+ |
| Ổ cứng | 10 GB trống | 20 GB+ SSD |
| CPU | 2 cores | 4 cores+ |
| GPU | Không bắt buộc | CUDA GPU (tùy chọn, cho embedding) |

## Phần mềm

| Thành phần | Phiên bản | Ghi chú |
|-----------|-----------|---------|
| Python | 3.10+ | 3.14 được khuyến nghị |
| PostgreSQL | 14+ | 16 được khuyến nghị |
| Node.js | 18+ | Cần cho Playwright browser binaries |
| Git | Bất kỳ | |

## Dependencies

```
python-dotenv>=1.0.1
playwright>=1.52.0
psycopg[binary]>=3.2.9
requests>=2.32.3
sentence-transformers>=3.0.1
fastapi>=0.116.1
uvicorn>=0.35.0
```

## Biến môi trường (`.env`)

### Bắt buộc

```env
# PostgreSQL connection
DATABASE_URL=postgresql://user:password@localhost:5432/bds_agent
```

### LLM & Embedding

```env
# OpenRouter (dùng cho LLM enricher — miễn phí tier)
OPENAI_API_KEY=sk-or-v1-...

# Hoặc Ollama Cloud
OLLAMA_CLOUD_API_KEY=sk-...
OLLAMA_CLOUD_API_BASE=https://llm.fastnative.com/v1
```

### Telegram Notifications (tùy chọn)

```env
TELEGRAM_BOT_TOKEN=123456789:ABCdefGHI...
TELEGRAM_CHAT_ID=987654321
TELEGRAM_ENABLED=false
```

### Khác

```env
# Tối ưu cho macOS (tránh lỗi MPS)
PYTORCH_MPS_HIGH_WATERMARK_RATIO=0.0

# Logging
LOG_LEVEL=INFO
```

## Database Schema

Database cần các extension:

```sql
CREATE EXTENSION IF NOT EXISTS pg_trgm;   -- near-duplicate detection
CREATE EXTENSION IF NOT EXISTS vector;     -- vector similarity search
```

Chạy migration tự động:

```bash
python migrations/run.py
```

## Browser (Playwright)

Playwright cần Chromium browser. Cài đặt tự động qua CLI:

```bash
playwright install chromium
playwright install-deps chromium
```

Trên Linux (Docker), browsers được cài trong image tại `/ms-playwright`.

## Network

- **Port 8000**: API server (uvicorn)
- **PostgreSQL port 5432**: Database
- **Internet**: Cần để crawl Facebook, gọi LLM API, gửi Telegram

### Firewall

Mở port 8000 nếu cần truy cập từ máy khác:

```bash
# macOS
sudo firewall-cmd --add-port=8000/tcp --permanent
sudo firewall-cmd --reload

# Linux (ufw)
sudo ufw allow 8000/tcp

# Docker
# Đã mở sẵn trong docker-compose.yml
```
