# VPS 監控流量面板

[![test](https://github.com/wwintj/bandwagon-vps-traffic-panel/actions/workflows/test.yml/badge.svg)](https://github.com/wwintj/bandwagon-vps-traffic-panel/actions/workflows/test.yml)
[![release](https://img.shields.io/github/v/release/wwintj/bandwagon-vps-traffic-panel)](https://github.com/wwintj/bandwagon-vps-traffic-panel/releases)

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
- root 權限

## 一鍵安裝 / 升級 / 卸載

用 root 執行這一行，會出現選單：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/wwintj/bandwagon-vps-traffic-panel/main/panel.sh)
```

```
======== VPS Traffic Panel ========
  1) Install / 安裝
  2) Update  / 升級
  3) Uninstall / 卸載
  4) Status / 狀態
  5) Logs / 日誌
  6) Restart / 重啟
  0) Exit / 退出
```

裝好之後，隨時輸入 `vps-panel` 就能叫出同一個選單，也可以直接下指令：

```bash
vps-panel update        # 一鍵升級，保留資料與設定
vps-panel status        # 版本、服務狀態、訪問網址、是否有新版本
vps-panel logs          # 查看日誌
vps-panel restart       # 重啟服務
vps-panel uninstall     # 一鍵卸載（會問要不要先備份資料）
```

> 請用 `bash <(curl ...)`，不要用 `curl ... | bash`，否則安裝過程中無法輸入帳密。

### 安裝時會問什麼

1. 登入帳號（預設 `admin`）與密碼（長度不限，只能包含字母、數字與 `@ _ - . ! %`）
2. 每月流量重置日
3. 要不要用 HTTPS（見下一節）

其餘都會自動完成：安裝依賴、建立 Python 虛擬環境、產生 `.env`、建立 `vps-traffic-panel` 系統用戶與 systemd 服務、開放 ufw 端口（如果 ufw 有啟用）。如果之前裝過，舊目錄會備份到 `/opt/vps-traffic-panel.old.<時間>`，**流量歷史會自動帶到新安裝**。

### 無人值守安裝

所有問題都可以用環境變數預先回答，適合批次部署：

```bash
VPS_PANEL_PASSWORD='your-password' \
VPS_PANEL_CERT=/root/cert/fullchain.pem VPS_PANEL_KEY=/root/cert/private.key \
VPS_PANEL_DOMAIN=monitor.example.com \
bash <(curl -fsSL https://raw.githubusercontent.com/wwintj/bandwagon-vps-traffic-panel/main/panel.sh) install -y
```

| 變數 | 說明 |
| --- | --- |
| `VPS_PANEL_USERNAME` / `VPS_PANEL_PASSWORD` | 登入帳密（無人值守時密碼必填） |
| `VPS_PANEL_RESET_DAY` | 每月重置日，預設 `1` |
| `VPS_PANEL_PORT` | 面板後端端口，預設 `8088` |
| `VPS_PANEL_CERT` / `VPS_PANEL_KEY` | 指定證書與私鑰 |
| `VPS_PANEL_SSL` | `auto` 自動搜尋證書；`off` 不用 HTTPS。無人值守且沒指定證書時預設 `off` |
| `VPS_PANEL_DOMAIN` | 面板域名；設為空字串則直接用 `https://IP:端口` |
| `VPS_PANEL_HTTPS_PORT` | Nginx HTTPS 端口，預設 `443`，被占用時自動改用 `8443` |

### 手動安裝（不用一鍵腳本）

```bash
apt-get update && apt-get install -y git curl
git clone https://github.com/wwintj/bandwagon-vps-traffic-panel.git /opt/vps-traffic-panel-src
sudo /opt/vps-traffic-panel-src/scripts/install.sh   # 安裝
sudo /opt/vps-traffic-panel-src/scripts/update.sh    # 升級（會自動拉取最新版）
sudo /opt/vps-traffic-panel/scripts/uninstall.sh --keep-data   # 卸載並備份資料
```

## 已經有 SSL 證書？直接用

安裝腳本會自動搜尋 VPS 上已有的證書，找到後列出來讓你選：

- Let's Encrypt / certbot：`/etc/letsencrypt/live/`
- acme.sh：`/root/.acme.sh/`
- `/root/cert/`、`/root/*.crt` 這類常見路徑
- x-ui / 3x-ui 的資料庫與設定
- 現有 Nginx 設定裡引用的證書
- `/etc/v2ray-agent`、`/etc/hysteria`、`/etc/sing-box`、`/etc/trojan` 等

選好證書後，輸入你要用來打開面板的域名（預設是證書上的域名）：

| 你的選擇 | 面板怎麼跑 | 訪問方式 |
| --- | --- | --- |
| 填域名 | Nginx 負責 HTTPS，面板只監聽本機 | `https://域名` |
| 不填域名 | 面板直接跑 HTTPS（以 root 運行，才能讀取私鑰） | `https://IP:端口`，瀏覽器會提示證書與 IP 不符 |
| 沒有證書 / 不使用 | HTTP | `http://IP:端口` |

**443 已經被 xray / x-ui 等程式占用也沒關係。** 安裝腳本會偵測端口是誰在用：

- 443 被別的程式占用 → 自動改用 `8443`（或讓你輸入其他端口），訪問 `https://域名:8443`
- 80 被占用 → 不建立 HTTP 跳轉，也不會讓新裝的 Nginx 搶端口
- 已經在跑的 Nginx 不受影響，面板以獨立的 `server_name` 加進去
- Nginx 若因任何原因起不來，會自動退回 HTTP 模式，面板仍然能打開
- 安裝最後會自我檢查 HTTPS 是否真的能連上

> 如果用了 8443 之類的非標準端口，記得在雲服務商的防火牆 / 安全組放行。

**證書續期後自動生效。** 安裝時會建立 `vps-traffic-panel-cert.path` 監看證書檔案。不管是 certbot、acme.sh、x-ui 還是手動覆蓋，只要證書一變，就會自動 reload Nginx（直連模式則重啟面板），不需要額外設定續期鉤子。用 `vps-panel status` 可以看到「Cert watch: on」。

**證書上的域名必須和你訪問的域名一致**，例如證書是 `*.example.com`，就要用 `monitor.example.com` 這類子域名訪問，並在 DNS 把它解析到這台 VPS。

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
| `SSL_ENABLED` / `SSL_CERTFILE` / `SSL_KEYFILE` / `HTTPS_MODE` / `HTTPS_PORT` / `PANEL_DOMAIN` | HTTPS 設定，由安裝腳本寫入 | |

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
vps-panel               # 管理選單
vps-panel status
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

如果你用的是 Nginx HTTPS 模式，這是預期行為：面板現在只監聽本機，請改用 `https://你的域名` 訪問。執行 `vps-panel status` 可以看到正確網址。

### HTTPS 打不開，但 HTTP 可以

- 用了 `8443` 等非標準端口時，確認雲服務商的防火牆 / 安全組已放行
- 確認域名已解析到這台 VPS，且和證書上的域名一致
- 檢查 Nginx：`nginx -t && systemctl status nginx`

### 證書續期了，瀏覽器還是顯示舊證書

先確認監看服務在運行：`systemctl status vps-traffic-panel-cert.path`。手動套用新證書：`bash /opt/vps-traffic-panel/scripts/cert-watch.sh reload`。

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

端到端測試（會真的安裝 Nginx 與 systemd 服務，**只在用完即丟的機器上跑**，CI 每次都會跑）：

```bash
sudo bash tests/e2e/run.sh
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
panel.sh         一鍵管理選單（vps-panel）
scripts/         安裝 / 升級 / 卸載 / 證書續期監看
tests/           pytest 與端到端測試
```

## 更新日誌

見 [CHANGELOG.md](CHANGELOG.md) 與 [Releases](https://github.com/wwintj/bandwagon-vps-traffic-panel/releases)。
