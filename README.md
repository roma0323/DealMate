# DealMate 原型

懂行的二手交易代理人：用講的交代想賣或想買什麼，代理人替你補齊資訊、找到符合的東西、問該問的問題、約好面交。價格固定，不議價。產品說明見 Google Drive 上的「Mini-project idea」（ray 分頁）。

**設計原則：後端不呼叫任何 AI。** 需要思考的事（辨識照片、查規格、把需求轉成條件、判斷商品介紹答不答得出來、讀行事曆）都在使用者自己的 Claude 裡完成，再把整理好的資料送進後端。後端只存資料、用規則配對、寄 email。

## 目前進度

| 部分 | 狀態 |
|---|---|
| MCP server、18 個 tools（其中 2 個只給卡片用） | 完成 |
| SQLite 資料庫（6 張表） | 完成 |
| 品類指南：結構化欄位 + 買家檢查清單（冰箱、燈、手機、床墊、腳踏車、書、耳機，其他用通用欄位） | 完成：`src/dealmate/checklists.py` |
| 刊登：規格標示來源（官方規格／賣家確認／AI 推測）、尺寸與搬運 | 完成 |
| 配對：品類 + 預算區間 + 規格條件；需求持續開啟，新商品符合就寄信 | 完成 |
| 代問賣家、賣家回答可公開寫進商品介紹 | 完成 |
| 固定價購買、賣家確認 | 完成 |
| 面交：比對雙方時段與捷運站，寄確認信附行事曆邀請（.ics）；可要求改時間 | 完成 |
| 照片、商品卡片（MCP App） | 完成 |
| ChatGPT 連接器 | 尚未測試（卡片的 UI 格式不一定相容） |

## 一筆交易怎麼走（以落地燈為例）

| 步驟 | 使用者說 | Claude 做的事 | 呼叫的 tool |
|---|---|---|---|
| 賣家上架 | 「我要賣這盞燈，用了一年，500 元，平日晚上南港站或汐止站可以面交」 | 讀品類指南、從照片辨識型號、查規格和新品價，整理草稿給賣家確認；上架後請賣家用卡片按鈕上傳照片 | `get_category_guide` → `create_listing` |
| 買家交代需求 | 「想找落地燈，要能用 LED，預算 300～600，平日晚上公館或南港」 | 把需求轉成條件（`led_ok = true`），提醒檢查清單 | `get_category_guide` → `create_want` |
| 配對 | — | 現在就有的直接顯示成商品卡片；之後上架的寄信通知 | （自動） |
| 問問題 | 「可以調光嗎？」 | 先看商品介紹；答不出來就寄信問賣家 | `show_listing` → `ask_seller` |
| 賣家回答 | 「不行，只有開關，可以公開」 | 答案寫進商品介紹，也補進規格 | `answer_question`、`update_listing` |
| 決定購買 | 「我要買」 | 以售價提出購買，商品變「已有人要」，寄信請賣家確認 | `request_purchase` |
| 賣家確認 | 「確認」 | 成交；後端選出雙方都可以的最早時段和捷運站，寄信附行事曆邀請 | `confirm_deal` |
| 改時間 | 「那天不行」 | 換成之後的日子，或用使用者給的新時段（可以是從行事曆讀到的空檔日期） | `reschedule_meetup` |

任何時候說「DealMate 進度如何？」，Claude 會呼叫 `check_status`，列出待回答的問題、等確認的交易和面交時間。

## MCP tools

| Tool | 誰用 | 做什麼 |
|---|---|---|
| `register_user` | 全部 | 註冊名字、email、可面交時段與捷運站 |
| `get_category_guide` | 全部 | 品類的欄位 key、買家檢查清單、是不是大型物品 |
| `create_listing` / `update_listing` | 賣家 | 上架／修改（規格依 key 合併）；符合的買家會收到信 |
| `show_listing` | 全部 | 單一商品卡片：照片、規格與來源、公開問答 |
| `browse_listings` | 全部 | 所有在售商品，可指定品類 |
| `create_want` | 買家 | 建立需求，回傳現在符合的商品 |
| `ask_seller` / `answer_question` | 買家／賣家 | 代問與回答 |
| `request_purchase` / `confirm_deal` | 買家／賣家 | 購買與確認；`accept=false` 是婉拒或取消 |
| `reschedule_meetup` | 全部 | 更換面交時間地點 |
| `check_status` | 全部 | 我的商品、需求、問題、交易 |
| `upload_photo`、`view_listing_photo_for_ai`、`view_listing_photo_for_human` | 全部 | 照片（見下方） |
| `app_get_photo`、`app_upload_photo` | 只給卡片 | 卡片顯示和上傳照片 |

### 資料格式

- 時段 `meetup_slots`：`{"day": "weekday", "start": "18:00", "end": "22:00"}`。`day` 可以是 `mon`～`sun`、`weekday`、`weekend`、`daily`；也可以用 `{"date": "2026-11-12", ...}` 指定日期，例如從行事曆讀到的空檔。
- 捷運站 `meetup_stations`：`["南港", "汐止"]`，有沒有寫「站」都可以。
- 規格 `specs`：`{"key": "height_cm", "value": 150, "source": "official"}`，`source` 是 `official`、`seller` 或 `agent`。沒給 `label` 會用品類指南的名稱。
- 條件 `conditions`：`{"key": "height_cm", "op": "<=", "value": 83}`，`op` 可以是 `<=` `>=` `<` `>` `=` `!=` `contains`。

## 配對規則

- 品類相同：「小冰箱」「IKEA 落地燈」這類說法會先對到品類指南的名稱（冰箱、燈），兩邊才對得上。
- 售價在買家的 `budget_min`～`budget_max` 之間。
- 每個條件：商品規格有這個 key 而且不符合 → 不配對；商品沒寫這個 key → 仍然配對，但列在 `unknown_conditions`，請買家的 Claude 從型號查或問賣家。
- 同一個需求對同一件商品只寄一次信。賣家確認成交後，買家因為這件商品配對到的需求會關閉。

## 面交規則

- 地點：賣家列的捷運站中，第一個買家也有列的。只有一方列了捷運站，就用那一方的。
- 時間：從現在起（整點或半點）往後找，雙方時段重疊至少 60 分鐘的最早時間；預設找 14 天，如果時段裡有更晚的日期就找到那天。
- 找不到：雙方都會收到信，請他們回到 Claude 提供其他時間地點（`reschedule_meetup`）。
- 行事曆邀請（`invite.ics`）用 UTC 時間；改時間時用同一個 UID，會更新原本的活動。

## 商品卡片（MCP App）

Claude.ai 網頁版與桌面版會把下面這些 tool 的結果畫成互動卡片：

| Tool | 卡片內容 |
|---|---|
| `browse_listings`、`create_want` | 商品照片卡片：品名、價格、狀態；`create_want` 會標出介紹沒寫到的條件 |
| `create_listing`、`update_listing`、`show_listing` | 單一商品：照片、介紹、規格與來源、尺寸與搬運、公開問答；賣家看到上傳照片按鈕 |

- 照片由卡片向 server 要（`app_get_photo`），上傳也由卡片送出（`app_upload_photo`），照片資料不會進到 Claude 的對話。
- 不支援卡片的地方（例如 Claude Code）只會看到文字結果，也就沒有上傳按鈕；小圖可以改用 `upload_photo`。

## 照片

`upload_photo` 和 `view_listing_photo_for_human` 用和 Google Drive 一樣的格式：`content`（檔案內容的 base64）、`mimeType`、`title`（檔名）。

為什麼上傳不用 base64、下載可以：上傳時 base64 是 Claude 要自己一個字一個字寫出來的參數，一張圖要好幾分鐘；下載時 base64 在 tool 的回傳結果裡，Claude 只要讀，再用沙盒裡的小段 Python 解碼成檔案，很快。

| 做什麼 | 怎麼做 |
|---|---|
| 上傳照片（主要） | 上架後的商品卡片上按「上傳照片」；或請 Claude 顯示商品（`show_listing`），賣家看到的卡片有上傳按鈕。照片在瀏覽器裡縮小後直接傳給 server，不經過 Claude，一兩秒就好 |
| 上傳照片（備用） | 沒有卡片的地方（例如 Claude Code）用 `upload_photo`，只適合很小的圖（Claude 會先縮到長邊約 400 px） |
| 使用者想看照片 | `view_listing_photo_for_human`：像從 Drive 下載檔案，Claude 把 `content` 還原成圖片檔給使用者看（需要 Claude 開啟程式執行） |
| Claude 自己看照片 | `view_listing_photo_for_ai`：只給 Claude 辨識型號、回答問題用，使用者不會拿到照片 |
| 瀏覽時看照片 | 商品卡片會直接顯示照片 |

- 照片存在專案資料夾的 `photos/`，每件商品一張，換照片會取代舊的。上限 8 MB，只接受 JPG、PNG、GIF、WebP（看檔案內容判斷，不看副檔名）。
- 新商品配對到買家時，通知信會附上照片。但通知信是在 `create_listing` 時寄出，那時賣家還沒按卡片上傳，所以上架當下就配對到的買家收到的信不會有照片。

## 除錯模式

`.env` 設 `DEALMATE_DEBUG=1`（改完要重開 server）時，`browse_listings` 另外顯示每件商品的所有問題（含不公開的）、交易紀錄，以及所有需求和使用者。**給別人用或 demo 前，記得改回 `DEALMATE_DEBUG=0`。**

## 檔案

| 檔案 | 內容 |
|---|---|
| `src/dealmate/server.py` | MCP tools、server 說明（instructions）與啟動設定 |
| `src/dealmate/engine.py` | 配對、問答、交易狀態、面交排程 |
| `src/dealmate/checklists.py` | 品類指南：欄位與檢查清單 |
| `src/dealmate/notify.py` | 寄 email、行事曆邀請 |
| `src/dealmate/db.py` | SQLite 資料表與讀寫 |
| `src/dealmate/photos.py` | 照片檢查與儲存 |
| `src/dealmate/ui.py` | 對話裡顯示的商品卡片（MCP App） |
| `.env.example` | 設定檔範本 |

## 第一次設定

1. 安裝 [uv](https://docs.astral.sh/uv/)（這台 Mac 已經有了）。
2. 複製設定檔：`cp .env.example .env`，依下面「帳號」一節填入。
3. 啟動：`uv run dealmate`，看到 `Uvicorn running on http://127.0.0.1:8000` 就成功了。

**從議價版升級：** 資料表完全不同，請停止 server 後刪除 `dealmate.db` 再啟動（server 偵測到舊資料庫會拒絕啟動並提示）。`.env` 裡的 `ANTHROPIC_API_KEY` 已經用不到，可以刪掉。

## 帳號（要自己註冊的東西）

| 帳號 | 必要嗎 | 做什麼 | 填進 .env |
|---|---|---|---|
| ngrok | 接 Claude 時必要 | 到 ngrok.com 註冊，下載 macOS 版 ngrok，執行 `ngrok config add-authtoken <你的 token>`；在 Dashboard 的 Domains 領取免費的固定網域 | `PUBLIC_HOST=你的網域`（不含 https://） |
| 團隊 Gmail | 選用 | Google 帳號開啟兩步驟驗證，再到「應用程式密碼」產生一組 16 碼密碼 | `GMAIL_ADDRESS`、`GMAIL_APP_PASSWORD` |

沒有填 Gmail 也能跑：email 只印在終端機。

## 連到 Claude

1. 終端機 A：`uv run dealmate`
2. 終端機 B：`ngrok http 8000 --url=你的網域`
3. Claude → 設定 → Connectors → Add custom connector，網址填 `https://你的網域/mcp`。已經加過的話，tools 改了之後要在 Connectors 頁面重新連線，Claude 才會拿到新的 tool 清單。
4. 在 Claude 開兩個對話測試：一個說「我是 Amy，我要賣這盞落地燈……」，另一個說「我是 Ben，我想找一盞落地燈……」。

Demo 時用 `caffeinate -i uv run dealmate` 啟動，避免 Mac 睡眠。

## 注意

- `.env` 與 `dealmate.db` 已列在 `.gitignore`，不要上傳或截圖。
- 想清空資料：停止 server 後刪除 `dealmate.db`。
