# VPS 監控流量面板

[![test](https://github.com/wwintj/vps-traffic-panel/actions/workflows/test.yml/badge.svg)](https://github.com/wwintj/vps-traffic-panel/actions/workflows/test.yml)
[![release](https://img.shields.io/github/v/release/wwintj/vps-traffic-panel)](https://github.com/wwintj/vps-traffic-panel/releases)

一個輕量、適合長期掛在 VPS 上的流量監控面板。直接讀取 Linux 的 `/proc/net/dev`，資源占用很低，1C1G 的小機器也能跑。

- 後端：FastAPI + SQLite（按小時彙總）
- 前端：單頁 HTML + ECharts
- 部署：一鍵腳本 + systemd，可選 Nginx HTTPS

## 功能

**流量統計**
- 即時上下行速度
- 今日、本期（自訂每月重置日）、累計流量
- 最近 24 小時 / 30 天 / 12 個月趨勢圖
- 月流量配額進度條：手動設定，或直接讀取 Bandwagon 官方用量
- 一鍵清零統計

**伺服器資訊**
- CPU、記憶體、磁碟使用率
- 系統版本、Kernel、運行時間、公網 IP
- IP 歸屬地、ISP、ASN、線路類型（機房 / 家寬 / 代理）
- 訪問者 IP、位置與網路類型
- 網卡離線偵測，自動切換到有流量的網卡

**整合**
- Telegram 每日流量推送（自訂時區與時間）
- Cloudflare DDNS：公網 IP 變動時自動更新 A 記錄
- Bandwagon（搬瓦工）：查詢官方配額、讀取可用機房、一鍵切換機房並追蹤新 IP 與 DNS 生效

**管理**
- 在面板內修改登入帳密、標題、副標題、重置日
- 版本號顯示在頁尾

## 系統需求

- Debian / Ubuntu（腳本使用 `apt-get` 與 systemd）
- Python 3.8 以上
- root 權限（僅安裝時需要）

## 安裝

```bash
apt-get update && apt-get install -y git curl
cd /opt
git clone https://github.com/wwintj/vps-traffic-panel.git vps-traffic-panel-src
cd /opt/vps-traffic-panel-src
chmod +x scripts/*.sh
sudo ./scripts/install.sh
```

安裝腳本會詢問登入帳號、密碼（至少 8 位，只能包含字母、數字與 `@ _ - . ! %`）和每月重置日，然後自動：

1. 偵測舊安裝並備份到 `/opt/vps-traffic-panel.old.<時間>`，**流量歷史會自動帶到新安裝**
2. 自動選擇端口（預設 8088，被占用時讓你輸入）
3. 搜尋 VPS 上已有的 SSL 證書（Let's Encrypt、acme.sh、Nginx、x-ui / 3x-ui 等）
4. 建立 Python 虛擬環境、安裝依賴、產生 `.env`
5. 建立 `vps-traffic-panel` 系統用戶與 systemd 服務並啟動

安裝完成後，終端會顯示訪問地址。

### 三種訪問模式

| 模式 | 條件 | 面板監聽 | 訪問方式 |
| --- | --- | --- | --- |
| HTTP | 沒有證書 | `0.0.0.0:端口` | `http://IP:端口` |
| Nginx HTTPS | 有證書並填寫域名 | `127.0.0.1:端口` | `https://域名` |
| 直連 HTTPS | 有證書但不填域名 | `0.0.0.0:端口` | `https://IP:端口` |

Nginx 模式下面板只監聽本機，外部只能透過 Nginx 的 HTTPS 訪問。直連 HTTPS 模式需要讀取證書私鑰，因此以 root 運行，其他模式都以低權限用戶運行。

> 只用 HTTP 時，帳密以 Basic Auth 明文傳輸，建議至少放在 HTTPS 反向代理後面。

## 升級

```bash
cd /opt/vps-traffic-panel-src
git fetch origin
git reset --hard origin/main
sudo ./scripts/update.sh
```

升級腳本會：

- 用 `sqlite3 .backup` 備份 `data.db` 和 `.env` 到 `/opt/vps-traffic-panel/backups/`（保留最近 10 份）
- 同步程式檔案、更新依賴、重啟服務
- 顯示升級前後的版本號

`.env`、資料庫和面板設定都會保留。從舊版升級到 v1.0.0 的注意事項見 [CHANGELOG.md](CHANGELOG.md)。

## 卸載

```bash
cd /opt/vps-traffic-panel
sudo ./scripts/uninstall.sh              # 全部刪除
sudo ./scripts/uninstall.sh --keep-data  # 先把 .env 和 data.db 備份到 /root/vps-traffic-panel-backup-<時間>
```

卸載會停止並移除 systemd 服務、Nginx 設定、安裝目錄和服務用戶。

## 設定

設定檔位於 `/opt/vps-traffic-panel/.env`。大多數選項都可以直接在面板裡修改，改完立即生效；手動改 `.env` 則需要重啟服務。

| 變數 | 說明 | 預設 |
| --- | --- | --- |
| `HOST` / `PORT` | 監聽地址與端口 | 由安裝腳本決定 |
| `AUTH_USERNAME` / `AUTH_PASSWORD` | 登入帳密，**未設定密碼時面板拒絕所有登入** | — |
| `PANEL_TITLE` / `PANEL_SUBTITLE` | 面板標題與副標題 | |
| `INTERFACE` | 指定網卡，留空自動偵測 | 空 |
| `MONTH_RESET_DAY` | 每月流量重置日（1–31，月份天數不足時取月底） | `1` |
| `TRAFFIC_QUOTA_BYTES` | 手動月流量配額（位元組），`0` 為不設定 | `0` |
| `TELEGRAM_ENABLED` / `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | Telegram 推送 | 關閉 |
| `TELEGRAM_PUSH_HOUR` / `TELEGRAM_PUSH_MINUTE` | 每日推送時間 | `20:00` |
| `TELEGRAM_TIMEZONE_OFFSET` / `TELEGRAM_TIMEZONE_LABEL` | **面板時區**（見下方） | `8` / `中國時間` |
| `BANDWAGON_VEID` / `BANDWAGON_API_KEY` | 搬瓦工 API | 空 |
| `CLOUDFLARE_DDNS_ENABLED` / `CLOUDFLARE_API_TOKEN` / `CLOUDFLARE_ZONE_ID` / `CLOUDFLARE_RECORD_NAME` / `CLOUDFLARE_PROXIED` | Cloudflare DDNS | 關閉 |
| `SSL_ENABLED` / `SSL_CERTFILE` / `SSL_KEYFILE` / `HTTPS_MODE` / `PANEL_DOMAIN` | HTTPS 設定，由安裝腳本寫入 | |

### 時區

面板內「TG 推送」裡的 **面板時區** 決定了：

- 今日流量從幾點開始算
- 每月重置日在哪個時刻切換
- 24 小時 / 30 天圖表的時間軸
- Telegram 推送時間與報表內容

伺服器本身是什麼時區都沒有關係。例如 VPS 用 UTC、面板時區設為 UTC+8，今日流量就從北京時間 0 點開始算。

### Cloudflare DDNS

需要一個有 `Zone.DNS:Edit` 權限的 API Token，以及域名所在的 Zone ID。啟用後每 10 分鐘檢查一次公網 IP，變動時更新 A 記錄（記錄不存在時會自動建立）。

### Bandwagon（搬瓦工）

在 KiwiVM 的 API 頁面取得 VEID 與 API Key。填寫後：

- 月流量配額進度條改用官方數據
- 「切換機房」可以讀取可用機房並發起遷移。遷移期間面板會每 20 秒檢查新 IP、自動同步 DDNS，並在 IP 變更和域名生效時發送 Telegram 通知

## 安全設計

- **最小暴露面**：Nginx 模式下後端只監聽 `127.0.0.1`
- **CSRF 防護**：所有 POST 介面都要求 `X-Panel-Request: 1` 請求頭，惡意網頁無法利用瀏覽器快取的帳密發送請求
- **密鑰不外洩**：Token / API Key 只以 `********末4位` 的形式返回給瀏覽器；保存時沿用原值，填入新值則覆蓋
- **防爆破**：同一 IP 在 15 分鐘內嘗試 10 組不同的錯誤帳密後，會被鎖定 15 分鐘（重複送出同一組舊密碼不計入，避免改密碼後把自己鎖住）
- **安全寫入**：`.env` 以原子方式寫入、權限 600，不會因斷電或磁碟滿而損壞
- **低權限運行**：服務以 `vps-traffic-panel` 用戶運行，並啟用 `NoNewPrivileges`
- 反向代理的 `X-Forwarded-For` / `X-Real-IP` 只在請求來自本機時才採信

### 自行呼叫 API

API 使用 Basic Auth。GET 可以直接呼叫，POST 需要多帶一個請求頭：

```bash
curl -u admin:你的密碼 https://panel.example.com/api/summary
curl -u admin:你的密碼 -X POST -H 'X-Panel-Request: 1' https://panel.example.com/api/telegram-test
```

## 自訂 Nginx 反向代理

安裝腳本會自動產生 `/etc/nginx/conf.d/vps-traffic-panel.conf`。如果你要自己寫，請保留 `Host`、`X-Real-IP` 與 `X-Forwarded-For` 請求頭，並讓面板只監聽本機（`.env` 設定 `HOST=127.0.0.1`）：

```nginx
server {
    listen 443 ssl http2;
    server_name monitor.example.com;

    ssl_certificate     /path/to/fullchain.pem;
    ssl_certificate_key /path/to/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8088;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

```bash
nginx -t && systemctl reload nginx
```

## 常用命令

```bash
systemctl status vps-traffic-panel
systemctl restart vps-traffic-panel
journalctl -u vps-traffic-panel -f
```

## 常見問題

### 面板打不開 / 502 Bad Gateway

502 通常是 Nginx 還在，但後端服務沒有正常運行。先看服務狀態和日誌：

```bash
systemctl status vps-traffic-panel
journalctl -u vps-traffic-panel -n 50
```

### 升級後 `http://IP:8088` 打不開了

如果你用的是 Nginx HTTPS 模式，這是預期行為：面板現在只監聽本機，請改用 `https://你的域名` 訪問。

### 提示 Too many failed logins

同一 IP 錯誤次數太多，等 15 分鐘，或執行 `systemctl restart vps-traffic-panel` 立即解除。

### 忘記密碼 / 提示 AUTH_PASSWORD is not configured

直接編輯 `.env`，修改或補上 `AUTH_PASSWORD=新密碼`，然後重啟服務：

```bash
nano /opt/vps-traffic-panel/.env
systemctl restart vps-traffic-panel
```

### 流量一直是 0

面板會自動偵測預設路由的網卡。偵測不準時，先確認網卡名稱：

```bash
ip route
ip a
```

然後在 `.env` 中指定，例如 `INTERFACE=eth0`，再重啟服務。

### 統計是否準確

面板只計算自己運行期間的流量，每秒取樣一次、每分鐘寫入資料庫。網卡計數器重置（例如 VPS 重啟）時會自動接續。和服務商帳單相比，可能因為統計方式不同（例如只算出站或雙向計費）而有差異。如果你用搬瓦工，配額進度條會直接採用官方數據。

## 開發

```bash
python3 -m venv venv
. venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest -q tests
```

本機運行（資料放在獨立目錄，不影響專案目錄）：

```bash
mkdir -p /tmp/panel-data
printf 'AUTH_USERNAME=admin\nAUTH_PASSWORD=devpassword\n' > /tmp/panel-data/.env
VPS_PANEL_DATA_DIR=/tmp/panel-data uvicorn app.main:app --port 8088
```

專案結構：

```
app/
  main.py        API、整合（Telegram / Cloudflare / Bandwagon）、背景任務
  collector.py   每秒讀取 /proc/net/dev，按小時彙總
  database.py    SQLite
  timeutil.py    面板時區
  config.py      讀取 .env
  templates/     前端頁面
  static/        前端靜態資源
scripts/         安裝 / 升級 / 卸載
tests/           pytest
```

## 更新日誌

見 [CHANGELOG.md](CHANGELOG.md) 與 [Releases](https://github.com/wwintj/vps-traffic-panel/releases)。
