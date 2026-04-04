# Cron Job Setup — BDS Agent Crawler

## Cách 1: Qua giao diện Settings (Khuyến nghị)

1. Mở app → tab **Settings** → **Cron Jobs**
2. Chọn tần suất: Mỗi 2h / 4h / Hàng ngày / Mỗi giờ
3. Nhấn **Tạo Cron**
4. Job chạy trong 7 ngày (auto-expiry của Claude Code scheduler)

---

## Cách 2: CLI `bds` (Đa nền tảng)

Sau khi thêm vào PATH (xem README):

```bash
# macOS / Linux / Windows (PowerShell/CMD)
bds crawl     # Chạy scraper 1 lần ngay
bds enrich   # Chạy enricher 1 lần ngay
```

Kết hợp với system scheduler:

### macOS — LaunchAgent

```bash
cat > ~/Library/LaunchAgents/com.bds.crawl.plist << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.bds.crawl</string>
  <key>ProgramArguments</key>
  <array>
    <string>/path/to/bds</string>
    <string>crawl</string>
  </array>
  <key>StartCalendarInterval</key>
  <array>
    <dict><key>Hour</key><integer>8</integer><key>Minute</key><integer>37</integer></dict>
    <dict><key>Hour</key><integer>12</integer><key>Minute</key><integer>37</integer></dict>
    <dict><key>Hour</key><integer>16</integer><key>Minute</key><integer>37</integer></dict>
    <dict><key>Hour</key><integer>20</integer><key>Minute</key><integer>37</integer></dict>
  </array>
  <key>StandardOutPath</key><string>/tmp/bds_crawl.log</string>
  <key>StandardErrorPath</key><string>/tmp/bds_crawl.err</string>
</dict>
</plist>
EOF

launchctl load ~/Library/LaunchAgents/com.bds.crawl.plist
```

### Linux — Cron

```bash
crontab -e
# Thêm dòng:
37 8,12,16,20 * * * /path/to/bds crawl >> /tmp/bds_crawl.log 2>&1
```

### Windows — Task Scheduler (PowerShell)

```powershell
# Chạy PowerShell as Administrator:
$action = New-ScheduledTaskAction -Execute "python.exe" -Argument "bds.py crawl" -WorkingDirectory "C:\path\to\BDS-Agent\crawl4ai"
$trigger = New-ScheduledTaskTrigger -Daily -At "08:37"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries
Register-ScheduledTask -Action $action -Trigger $trigger -TaskName "BDS Crawl" -Settings $settings
```

---

## Cấu hình nâng cao

### Giới hạn chỉ crawl groups cụ thể

```bash
# macOS / Linux
./bds crawl

# Hoặc gọi trực tiếp:
.venv314/bin/python facebook_group_scraper.py \
  --headless \
  --use-db-cookies \
  --group-url "https://www.facebook.com/groups/1206082637439108/" \
  --scroll-rounds 8 \
  --workers 1
```

### Kiểm tra log

```bash
# macOS / Linux
tail -50 /tmp/bds_crawl.log
# Hoặc dùng CLI:
bds log 50

# Windows PowerShell
Get-Content bds.log -Tail 50
```

---

## Troubleshooting

| Vấn đề | Nguyên nhân | Xử lý |
|--------|------------|--------|
| `No alive cookies` | Tất cả cookie dead | Settings → Cookies → Validate |
| `Desktop feed khong san` | Facebook checkpoint | Chờ 30-60 phút, giảm tần suất |
| Process treo | Playwright timeout | Thêm `--log-level ERROR` |
