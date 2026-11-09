# D55 批量並行可行性評估（僅雲端模型）— HER-30

> 更正：依 D57 複審，結論改為「有條件可行、正式預設維持 1」，漏列的共用資源見 `docs/BATCH-RUNS-D58.md` §P。

作者：claude-code（Opus 5.5），session 9c20ad14-a9f1-4819-83c8-af84e63c4608，2026-11-09。只評估，不實作。
範圍：使用者要求「先評估可行性，限制在雲端模型」。本機模型（provider=local）維持一次一檔且獨占。

## 現況限制

`engine/server.py:733-755`：Pro 完整圖或台股研究同時只允許一個（`run_slot`），第二個直接 `run_busy`。註解理由：「完整圖執行時會改動整個 process 共用的函式庫設定與環境變數」。限制在 E6（beeca9b）加入。

## 共用狀態盤點

| # | 共用狀態 | 位置 | 並行時的影響 | 判定 |
|---|---|---|---|---|
| 1 | 模型 API key 寫入環境變數 | `full_debate.py:735-781` | 只在同步建構區間設定並在任何 await 前還原；建好的 client 不再讀 env | 已安全 |
| 2 | Codex OAuth 工廠 patch | `codex_llm.py:153-178` | 同上，同步區間內 patch／還原 | 已安全 |
| 3 | 台股工具與 trade_date／token／快照 | `tw_graph_tools.py:45-69`（ContextVar，D47） | 有並行測試 `test_tw_d47.py:210` | 已安全 |
| 4 | 台股對上游的 helper patch | `tw_graph_tools.py:775-797` | 依 ContextVar 判斷是否台股 run，其他 ticker 走原函式 | 已安全 |
| 5 | **函式庫全域設定 `dataflows.config._config`** | `tradingagents/dataflows/config.py`；`trading_graph.py:102` 每次建圖 `set_config` | 工具在執行中途 `get_config()` 決定 data vendor（`interface.py:157`）與輸出語言（`agent_utils.py:62`）。美股與台股同時跑時，後建的 run 會把 vendor 全改成台股來源，美股 run 會抓錯資料；報告語言不同也會互蓋 | **阻擋，需修** |
| 6 | **`ALPHA_VANTAGE_API_KEY` 整段 run 期間在 env** | `full_debate.py:701-716` | 有 key 的 run 跑到一半，另一個沒 key 的 run 會把它清掉，新聞／基本面工具失效 | **阻擋，需修** |
| 7 | Alpaca 新聞憑證 | `server.py:939-943` `alpaca_news.set_alpaca_news_credentials` | 模組層級設定，同上性質 | **需修**（同 6 的作法） |
| 8 | 記憶檔（過去決策） | `memory.py:160-175` 讀→改→tmp→replace | 兩個 run 同時寫可能遺失一筆記憶（best-effort，不影響當次結果） | 小，加 process 鎖 |
| 9 | 結果目錄 | `trading_graph.py:611` 依 ticker 分目錄；checkpointer 依 ticker 分 SQLite | 批量已去重，同 ticker 不會並行 | 安全 |
| 10 | `_ZOMBIE_METERS`／slot | `full_debate.py:195-204` | 是 slot 判斷的一部分，改成容量制時要一起改 | 隨 slot 改 |
| 11 | CostGuard | `cost_guard.reserve` 已計入進行中預留 | 並行 N 檔會同時持有 N 份最壞情況預留，較早觸頂（保守方向） | 可接受 |
| 12 | SQLite（runs／sessions） | 單一 event loop 內寫入 | 無新增風險 | 安全 |

## 外部限制

- **模型供應商速率**：每檔數十次呼叫。API key 有 RPM／TPM 分級；OpenAI OAuth（ChatGPT 訂閱經 Codex 後端）的並行上限未公開，而 `codex_llm.py` 沒有 429 重試，被限流會直接讓該檔失敗。正式環境最近的 run 全是 OAuth（gpt-6-astra／gpt-6.1-sol），這是最大的未知數。
- **資料來源**：yfinance 會節流；FinMind 匿名額度與 TWSE／TPEx 對頻繁請求敏感。兩檔台股並行時請求量加倍。
- **資源**：引擎是單一 process，圖在 worker thread 跑，主要是等網路 I/O，2–3 檔並行的 CPU／記憶體負擔不大（未量測）。

## 兩種作法

**A. 同一 process 內並行（建議）**
1. 函式庫設定改為每個 run 一份：`dataflows/config.py` 加 ContextVar，`set_config` 寫目前 context，`get_config` 先讀 context 再讀全域（vendored 在本 repo，可直接改；台股工具已證明 LangGraph worker thread 會帶 context）。
2. Alpha Vantage key 與 Alpaca 新聞憑證改由 ContextVar 提供給工具，不再寫 process env／模組全域。
3. 記憶檔寫入加 process 鎖。
4. `run_slot` 改成容量制 gate：雲端 run 佔 1 格、上限 N；本機 run 要求獨占（沒有其他 run 時才能開始，開始後雲端也不能進）。
5. BatchManager 的並行數參數由 1 改為 N（預設 2，上限 3，使用者可設）；同一批量共用同一 provider／model。
6. Codex 後端加 429／5xx 有界退避重試。
7. 測試：美股＋台股、不同報告語言、有／無 Alpha Vantage key 交錯並行，各自的 vendor、語言、key 不互相污染；本機獨占；限流退避。

工作量：中（約 D50 後端的一半到同等）。風險集中在第 1 點必須涵蓋所有 `get_config()` 呼叫點，以及 LangGraph 未來版本是否仍傳遞 context。

**B. 每個 run 一個子程序**
完全隔離，但事件串流、取消、成本計量、秘密傳遞都要走 IPC，PyInstaller 凍結版（`engine.spec`、`freeze_entry.py`）啟動子程序也要另外處理。工作量大，不建議作為第一步。

## 結論

- **可行**，建議走 A，限雲端 provider，預設並行 2。
- 實際效益取決於供應商速率限制：OAuth 訂閱可能撐不住並行，需要先做小規模實測（例如 2 檔並行 × 3 輪，記錄 429、耗時、成本）才能決定預設值。
- 前置條件：D50 系列（引擎持有批量）先完成。D56 的 slot gate 與 BatchManager 會先做成「容量參數固定為 1」的形式，之後改並行不必重寫佇列。
- 若要做，建議另立階段：D58 實作 A（含測試）、D59 OAuth／API key 並行實測、D60 審查。需使用者決定是否進行。
