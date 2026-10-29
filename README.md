# DealMate 原型

二手交易 AI 代理人的 MCP server。設計說明見 Drive 上的「DealMate 原型技術設計文件」。

## 目前進度

| 部分 | 狀態 |
|---|---|
| MCP server、13 個 tools（其中 2 個只給卡片用） | 完成 |
| SQLite 資料庫（5 張表） | 完成 |
| 媒合（同品類關鍵字） | 完成 |
| 等待本人回答、雙方確認成交、破局 | 完成 |
| Email 通知（配對、議價中、需要決定、談成附報告、成交、破局） | 完成（未設定 Gmail 時只寫在 log） |
| 議價出價規則（程式決定價格，最多 6 輪） | 完成：`src/dealmate/engine.py` |
| 議價訊息、雙方各自的成交報告 | 完成：沒有 API key 用固定句子，填入 `ANTHROPIC_API_KEY` 後自動改用 Claude |
| 照片 | 完成：商品卡片上傳（主要）、`upload_photo` tool（小圖）、`view_listing_photo_for_ai`（給 Claude 看）、`view_listing_photo_for_human`（給人看，跟 Google Drive 下載檔案同格式）、買家配對信附照片 |
| 商品卡片（MCP App）：瀏覽時顯示照片卡片、上架後在對話裡直接上傳照片 | 完成：`src/dealmate/ui.py` |
| 瀏覽全部商品（`browse_listings`） | 完成；`.env` 設 `DEALMATE_DEBUG=1` 時會顯示底價、預算、email 等私密資料 |
| 配對改進（目前只比對品類字樣） | 尚未實作 |

## 商品卡片（MCP App）

Claude.ai 網頁版與桌面版會把下面這些 tool 的結果畫成互動卡片，直接顯示在對話裡：

| Tool | 卡片內容 |
|---|---|
| `browse_listings` | 所有商品的照片卡片：品名、價格、狀態、狀況、地點，點「詳細資料」看全部欄位 |
| `create_listing` | 「上架成功」卡片，附「上傳照片」按鈕：在對話裡選照片就會上傳，不用開連結 |
| `show_listing` | 單一商品卡片；賣家本人看到的版本有上傳／更換照片按鈕 |

- 照片由卡片向 server 要（`app_get_photo`），上傳也由卡片送出（`app_upload_photo`）。這兩個 tool 只給卡片用，Claude 本身不會看到照片資料，所以不會佔用對話長度。
- 不支援卡片的地方（例如 Claude Code）只會看到文字結果，也就沒有上傳按鈕；小圖可以改用 `upload_photo`。

## 瀏覽全部商品

在 Claude 說「DealMate 有哪些商品？」，Claude 會呼叫 `browse_listings`，列出所有在賣或議價中的商品；說「包含已成交的」會連已結束的一起列出。

- 一般模式：只顯示公開資料（品名、狀況、地點、開價、狀態、賣家名字、是否有照片）。
- 除錯模式（`.env` 裡 `DEALMATE_DEBUG=1`，改完要重開 server）：另外顯示每件商品的最低價、自動接受價、賣家 email、議價紀錄，以及所有買家需求和使用者。
- **給別人用或 demo 前，記得改回 `DEALMATE_DEBUG=0`**，否則任何連上的人都看得到別人的底價和預算。

## 照片

| 做什麼 | 怎麼做 |
|---|---|
| 上傳照片 | 上架後的商品卡片上按「上傳照片」；或請 Claude 顯示商品（`show_listing`），賣家看到的卡片有上傳按鈕 |
| 給人看照片 | `view_listing_photo_for_human`：回傳格式和 Google Drive 下載檔案一樣（`content` 是 base64、`mimeType`、`title`），Claude 會還原成圖片檔顯示（需要 Claude 開啟程式執行） |
| 給 Claude 看照片 | `view_listing_photo_for_ai`：Claude 直接看圖，可以描述商品 |
| 瀏覽時看照片 | `browse_listings` 的商品卡片會直接顯示照片 |

照片存在專案資料夾的 `photos/`。配對成功時，買家收到的 email 會直接附上照片。不再提供照片網址和上傳網頁。

## 出價規則

- 賣家從開價開始，每輪讓 (開價 − 最低價) ÷ 4，不會低於最低價。
- 買家從預算的 80% 開始，每輪加 (預算 − 起始出價) ÷ 4，不會高於預算。
- 買家出價 ≥ 賣家出價時，取兩者中間值（四捨五入到 10 元）。
  - 不低於自動接受價：直接談成，雙方收到各自的報告。
  - 低於自動接受價：先問賣家要不要接受。
- 6 輪後仍沒有交集：破局，商品和需求重新開放配對。
- 每輪間隔 1 秒，可用 `DEALMATE_ROUND_DELAY` 調整。

範例：開價 400、最低 300、自動接受 350、預算 350 → 第 4 輪賣家 325、買家 333 → 建議 330 元，問賣家。

## 檔案

| 檔案 | 內容 |
|---|---|
| `src/dealmate/server.py` | MCP tools 與啟動設定 |
| `src/dealmate/engine.py` | 媒合、議價狀態、通知時機 |
| `src/dealmate/llm.py` | 呼叫 Claude API |
| `src/dealmate/notify.py` | 寄 email |
| `src/dealmate/db.py` | SQLite 資料表與讀寫 |
| `src/dealmate/photos.py` | 照片檢查與儲存 |
| `src/dealmate/ui.py` | 對話裡顯示的商品卡片（MCP App） |
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
