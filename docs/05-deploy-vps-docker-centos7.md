# Deploy VPS BDS Agent Bang Docker Tren CentOS 7

Tai lieu nay duoc viet theo code hien tai trong repo, dung `docker compose` de chay ca `bds-agent` va `postgres`.

## 1. Danh gia nhanh VPS

VPS hien tai cua ban:

- CPU: 4 vCPU
- RAM: 7.6 GB
- Disk trong: 12 GB tren `/`
- Swap: 0 GB
- OS: CentOS Linux 7

Co the chay thu, nhung nen:

- Don them dung luong de trong it nhat 20 GB
- Them 2-4 GB swap
- Uu tien dung Docker de giam cong setup thu cong tren CentOS 7

## 2. Don dung luong truoc khi cai

Kiem tra thu muc nao dang chiem cho:

```bash
df -h
du -xh / --max-depth=1 2>/dev/null | sort -h
du -xh /var --max-depth=1 2>/dev/null | sort -h
```

Neu may da tung cai Docker truoc do:

```bash
docker system df
docker system prune -af
docker volume prune -f
```

Don cache yum:

```bash
yum clean all
rm -rf /var/cache/yum
```

## 3. Tao swap 4 GB

CentOS 7 cua ban dang `0B swap`, nen bo sung truoc khi build image:

```bash
fallocate -l 4G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=4096
chmod 600 /swapfile
mkswap /swapfile
swapon /swapfile
echo '/swapfile swap swap defaults 0 0' >> /etc/fstab
swapon --show
free -h
```

## 4. Cai Docker va Docker Compose plugin

```bash
yum install -y yum-utils
yum-config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
yum install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker
docker --version
docker compose version
```

Neu `docker compose` chua co, thu lai sau khi dang nhap lai shell. Truong hop hiem hoi khong co plugin, dung ban Compose standalone phu hop voi CentOS 7.

## 5. Clone project

```bash
mkdir -p /opt
cd /opt
git clone <repo-url-cua-ban> bds-agent
cd /opt/bds-agent/crawl4ai
```

## 6. Tao file `.env`

Copy tu mau:

```bash
cp .env.example .env
```

Mau `.env` toi thieu de chay tren VPS:

```env
POSTGRES_USER=bds
POSTGRES_PASSWORD=doi-mat-khau-manh
POSTGRES_DB=bds_agent
DATABASE_URL=postgresql://bds:doi-mat-khau-manh@postgres:5432/bds_agent

LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=sk-...
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
OPENROUTER_MODEL=qwen/qwen3.6-plus:free

OLLAMA_API_KEY=
OLLAMA_BASE_URL=https://cloud.ollama.com
OLLAMA_MODEL=gpt-oss:120b-cloud

EMBEDDING_PROVIDER=huggingface_local
EMBEDDING_MODEL=AITeamVN/Vietnamese_Embedding
EMBEDDING_DIMENSIONS=1024
EMBEDDING_API_KEY=
EMBEDDING_BASE_URL=https://api.openai.com/v1

TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
TELEGRAM_ENABLED=false

AUTO_START_ENRICHER=true
ENRICHER_POLL_INTERVAL=15

LOG_LEVEL=INFO
PYTHONUNBUFFERED=1
```

## 7. Build va chay

```bash
cd /opt/bds-agent/crawl4ai
docker compose build
docker compose up -d
docker compose ps
```

Xem log:

```bash
docker compose logs -f postgres
docker compose logs -f bds
```

## 8. Chay migration database

Sau khi `postgres` da healthy:

```bash
docker compose exec bds python migrations/run.py
```

Neu muon kiem tra ket noi DB:

```bash
docker compose exec bds python -c "from db import connect_db; conn = connect_db(); print('DB OK'); conn.close()"
```

## 9. Kiem tra app

```bash
curl http://127.0.0.1:8000/api/health
curl http://127.0.0.1:8000/api/stats
```

Neu can mo public tam thoi:

```bash
firewall-cmd --permanent --add-port=8000/tcp
firewall-cmd --reload
```

Sau do mo tren trinh duyet:

```text
http://IP_VPS:8000
```

## 10. Len production an toan hon

Khuyen nghi:

- Khong public lau dai cong `8000`
- Dung Nginx hoac Caddy reverse proxy qua domain
- Chi mo `80` va `443`
- Backup volume Postgres dinh ky

## 11. Lenh van hanh thuong dung

```bash
cd /opt/bds-agent/crawl4ai

docker compose ps
docker compose logs -f bds
docker compose logs -f postgres
docker compose restart bds
docker compose stop
docker compose up -d
docker compose down
```

## 12. Update phien ban moi

```bash
cd /opt/bds-agent
git pull
cd /opt/bds-agent/crawl4ai
docker compose build --no-cache
docker compose up -d
docker compose exec bds python migrations/run.py
```

## 13. Neu VPS bi day o

Kiem tra dung luong Docker:

```bash
docker system df
```

Don build cache va image khong dung:

```bash
docker system prune -af
docker builder prune -af
```

Kiem tra volume Postgres:

```bash
docker volume ls
docker volume inspect crawl4ai_postgres-data
```

Khong xoa volume Postgres neu ban con du lieu can giu.
