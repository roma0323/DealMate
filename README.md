# DealMate 原型

二手交易 AI 代理人的 MCP server。設計說明見 Drive 上的「DealMate 原型技術設計文件」。

## 目前進度

| 部分 | 狀態 |
|---|---|
| MCP server、6 個 tools | 完成 |
| SQLite 資料庫（5 張表） | 完成 |
| 媒合（同品類關鍵字） | 完成 |
| 等待本人回答、雙方確認成交、破局 | 完成 |
| Email 通知 | 完成（未設定 Gmail 時只寫在 log） |
| Claude API 寫議價訊息 | 函式完成，尚未接進議價迴圈 |
| **議價出價規則** | **尚未實作**：`src/dealmate/engine.py` 的 `run_negotiation()` |
| 照片上傳 | 尚未實作 |

## 檔案

| 檔案 | 內容 |
|---|---|
| `src/dealmate/server.py` | MCP tools 與啟動設定 |
| `src/dealmate/engine.py` | 媒合、議價狀態、通知時機 |
| `src/dealmate/llm.py` | 呼叫 Claude API |
| `src/dealmate/notify.py` | 寄 email |
| `src/dealmate/db.py` | SQLite 資料表與讀寫 |
| `.env.example` | 設定檔範本 |

## 第一次設定

1. 安裝 [uv](https://docs.astral.sh/uv/)（這台 Mac 已經有了）。
2. 複製設定檔：`cp .env.example .env`，依下面「帳號」一節填入。
3. 啟動：`uv run dealmate`，看到 `Uvicorn running on http://127.0.0.1:8000` 就成功了。

## 帳號（要自己註冊的東西）

| 帳號 | 必要嗎 | 做什麼 | 填進 .env |
|---|---|---|---|
| ngrok | 接 Claude 時必要 | 到 ngrok.com 註冊，下載 macOS 版 ngrok，執行 `ngrok config add-authtoken <你的 token>`；在 Dashboard 的 Domains 領取免費的固定網域 | `PUBLIC_HOST=你的網域`（不含 https://） |
| Anthropic API | 選用 | 到 console.anthropic.com 建立 API key，儲值約 US$5 | `ANTHROPIC_API_KEY` |
| 團隊 Gmail | 選用 | Google 帳號開啟兩步驟驗證，再到「應用程式密碼」產生一組 16 碼密碼 | `GMAIL_ADDRESS`、`GMAIL_APP_PASSWORD` |

沒有填 Anthropic 和 Gmail 也能跑：訊息改用固定句子，email 只印在終端機。

## 連到 Claude

1. 終端機 A：`uv run dealmate`
2. 終端機 B：`ngrok http 8000 --url=你的網域`
3. Claude → 設定 → Connectors → Add custom connector，網址填 `https://你的網域/mcp`。
4. 在 Claude 開兩個對話測試：一個說「我是 Amy，我要賣水壺……」，另一個說「我是 Ben，我想買水壺……」。

Demo 時用 `caffeinate -i uv run dealmate` 啟動，避免 Mac 睡眠。

## 注意

- `.env` 與 `dealmate.db` 已列在 `.gitignore`，不要上傳或截圖。
- 想清空資料：停止 server 後刪除 `dealmate.db`。
