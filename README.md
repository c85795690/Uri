# 🐱 OKX Order Flow DEMO

GitHub Actions 連線 OKX WebSocket，收到盤口或成交推播時重算訊號。Telegram 會發出做多／做空通知。啟用 DEMO 模式後，另有訂單結果與保護單查核通知。

| 訊號 | 五檔盤口不平衡率 | 最近 60 秒 Delta | 最近 300 秒 Delta |
| --- | ---: | ---: | ---: |
| 🐰 LONG | > +0.30 | > 0 | > 0 |
| 🐻 SHORT | < −0.30 | < 0 | < 0 |

- 五檔盤口不平衡率 = （前五檔買量 − 前五檔賣量）÷（前五檔買量 ＋ 前五檔賣量）。
- Delta = 主動買入成交張數 − 主動賣出成交張數。用成交時間滾動計算。OKX `trades` 可能合併多筆撮合，程式以推播的 `sz` 加總一次，依 `tradeId` 去重。［[OKX API 文件](https://www.okx.com/docs-v5/en/)］
- 行情用 OKX 公開市場。啟動或重連後先收滿長視窗，盤口超過 3 秒時暫停觸發。每個幣預設 30 分鐘通知冷卻。
- 進場訊號由盤口與成交量差決定。已收盤的 1 分鐘 K 線只用於計算 DEMO 的 ATR 止盈／止損。

## iPhone Safari 部署

1. 在「檔案」App 點 ZIP 解壓。到 GitHub 建立一個私有 repository。在預設分支上傳 `orderflow_runner.py`、`orderflow_engine.py`、`okx_demo.py`、`requirements.txt`、`test_orderflow.py` 和 `README.md`。
2. 工作流位置是 **`.github/workflows/okx_orderflow.yml`**。若手機檔案列表隱藏 `.github`，在 GitHub 選 **Add file → Create new file**，輸入整段路徑，再貼上另外提供的 `okx_orderflow.yml`。存到預設分支。
3. 在 repository 的 **Settings → Secrets and variables → Actions → Secrets** 新增下表的 Telegram Secrets。先到 Telegram 對自己的 bot 按 **開始**（`/start`）。［[GitHub Secrets](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets)］ ［[Telegram Bot API](https://core.telegram.org/bots/api#sendmessage)］
4. 打開 **Actions → 🐱 OKX Order Flow DEMO → Run workflow**。第一次選 **`notify`／15 分鐘**。Telegram 會先收到啟動訊息。等暖機完成後，有多空訊號時會收到選幣通知。Actions 日誌也會顯示暖機進度。

| GitHub Secret | 內容 | 使用模式 |
| --- | --- | --- |
| `TG_TOKEN` | Telegram BotFather 提供的 bot token | 兩種 |
| `TG_CHAT_ID` | 已開始與 bot 對話的聊天 ID | 兩種 |
| `OKX_API_KEY` | OKX 模擬交易的 API Key | DEMO |
| `OKX_API_SECRET` | 該 DEMO Key 的 Secret Key | DEMO |
| `OKX_API_PASSPHRASE` | 建立該 DEMO Key 時設定的 Passphrase | DEMO |

## 🧪 開啟 OKX 模擬下單

1. 在 OKX 切換到「模擬交易」，建立有讀取與交易權限的 **DEMO API Key**。確認 DEMO 帳戶支援逐倉，持倉模式選 **單向**。API 呼叫固定帶 `x-simulated-trading: 1`。［[OKX DEMO API](https://www.okx.com/docs-v5/en/#overview-demo-trading-services)］
2. 把三項 OKX 憑證填進 GitHub **Secrets**。在同一頁的 **Variables** 新增 `ENABLE_OKX_DEMO_ORDERS`，值填 `true`。
3. 手動 Run workflow 選 **`demo`**。程式先讀取帳戶和合約規格，逐筆查持倉、掛單、策略單和歷史冷卻。預設逐倉 **5×**，每筆 **100 USDT 名義金額**，最多 **5 個永續標的**持倉或有一般掛單。
4. 進場用 FOK 限價，需全量成交，價格上限按盤口和 `ENTRY_SLIPPAGE_BPS` 算。委託會帶一組 ATR 止盈與止損，兩者按最新成交價觸發市價出場。保護單依送單前盤口估算，實際成交價由 OKX 決定。GitHub 工作流結束後，已掛在 OKX 的保護單由 OKX 執行。［[OKX 下單文件](https://www.okx.com/docs-v5/en/#order-book-trading-trade-post-place-order)］

Telegram 會區分 **已成交且已確認保護單**、**未成交**、**略過／拒單** 和 **狀態待確認**。若送單回覆有疑義，程式用客戶單號查詢 OKX。仍無法核對訂單或保護單時，該次執行暫停新單並發出警示。收到警示後，請在 OKX DEMO 查持倉與 TP／SL，再啟動下一輪。

同一 repository 的執行會排隊，請將這個 DEMO 帳戶交給單一交易流程管理。選幣來自公開行情，FOK 以 DEMO 實際撮合結果為準。

## ⚙️ GitHub Variables

路徑為 **Settings → Secrets and variables → Actions → Variables**。

| 變數 | 預設 | 用途 |
| --- | --- | --- |
| `ORDERFLOW_SYMBOLS` | `BTC-USDT-SWAP,ETH-USDT-SWAP,SOL-USDT-SWAP` | 以逗號分隔，最多 20 個 USDT 永續 |
| `ORDERFLOW_IMBALANCE` | `0.30` | 多空不平衡率門檻，範圍 0.01～0.99 |
| `ORDERFLOW_SHORT_SECONDS` | `60` | 短 Delta，5～300 秒 |
| `ORDERFLOW_LONG_SECONDS` | `300` | 長 Delta 和暖機，15～600 秒，須大於短視窗 |
| `ORDERFLOW_COOLDOWN_SECONDS` | `1800` | 同幣通知與 DEMO 送單冷卻，60～7200 秒 |
| `POSITION_USDT` | `100` | 每筆目標名義金額，依合約面額及最小張數向下取整 |
| `MAX_POSITIONS` | `5` | DEMO 永續持倉或一般掛單標的數上限 |
| `ATR_PERIOD` | `14` | 已收盤 1m K 的 Wilder ATR 週期，2～80 |
| `ATR_SL_MULTIPLIER` | `1.5` | 停損距離的 ATR 倍數 |
| `TAKE_PROFIT_R` | `1.5` | 止盈距離的風險倍數 |
| `ENTRY_SLIPPAGE_BPS` | `8` | 相對送單前買一／賣一的 FOK 價格容許差，8 bps = 0.08% |

## ⏱ 選用排程

設定 `ORDERFLOW_SCHEDULE_ENABLED=true` 才會啟動排程。排程每 4 小時於第 13 分鐘觸發。用 `ORDERFLOW_SCHEDULE_MINUTES` 調整每輪時間（預設 230 分鐘，範圍 10～230）。長視窗 600 秒時，請設定至少 11 分鐘。

`ORDERFLOW_SCHEDULE_MODE` 預設為 `notify`。設定為 `demo` 並開啟 `ENABLE_OKX_DEMO_ORDERS` 後，排程才可送模擬單。

GitHub Actions 排程可能延遲。每輪之間有空窗，開始時需要重新暖機。私有 repository 使用 GitHub-hosted runner 會計入 Actions 使用量。［[GitHub Actions 排程](https://docs.github.com/en/actions/how-tos/troubleshoot-workflows#scheduled-workflows)］ ［[Actions 計費](https://docs.github.com/en/billing/concepts/product-billing/github-actions)］

## 檢查執行結果

- 收到啟動通知後，請在 Actions 日誌找「暖機完成」。有符合條件的行情才會收到多空選幣。
- 遇到 OKX 驗證失敗，請確認 API Key 建於模擬交易、三項 Secrets 對應同一把 Key，且該 Key 允許執行 runner 的來源。請從 Actions 日誌讀取 OKX 錯誤碼。
- 可用 `python -m unittest discover -v` 重跑本機測試。測試使用模擬行情與回覆。實際串接需用自己的 GitHub Secrets 執行。
