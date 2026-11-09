# D58 依 D57 複審定案的補充規則（HER-30）

作者：claude-code（Opus 5.5），session 9c20ad14-a9f1-4819-83c8-af84e63c4608，2026-11-09。
**實作依據 = `docs/BATCH-RUNS-D56.md` ＋ 本文件**；兩者衝突時以本文件為準。D57 審查全文在 HER-30 中央留言。
本文件也修正 `docs/BATCH-PARALLEL-D55.md` 的結論（文末 §P）。

## 階段（更新）

D57 複審結論「D56 仍需修改」。下列規則逐項定案後直接進入實作，不再另跑規劃複審；D54 程式審查會逐條核對本文件。

| 階段 | 內容 | 執行者 |
|---|---|---|
| D58 | 本文件 | 主控 |
| D52 | 後端實作 | sol |
| D53 | 前端實作 | Sonnet |
| D54 | 程式審查（唯讀，對照 D56＋D58） | Astra |

D55 文中建議的並行階段編號作廢；若使用者決定做並行，屆時另配新編號。

## A1 批量 run 的 executor 開頭順序（取代 D56 §1 的等待位置與 §3 的 OAuth 時點）

批量 run（`run.batch_id` 不為 None）的 executor 在**解析 start frame、解析 secret 參照之前**依序：

1. `claimed = await gate.wait_claim(run.id, deadline)`；成功後**同步**設 `slot_held = True`。
2. 呼叫 BatchManager 提供的 `prepare(run)`（`run.batch_prepare`）：
   - OAuth：Web 版以 `run_in_threadpool` 呼叫 hook（`ensure_fresh`），驗證回傳存在、有 access、`expires`（>1e12 視為毫秒）晚於現在＋10 分鐘；桌面版只檢查 frame 內明文 token 的 `expires`。
   - hook 回來後先檢查 `run.cancel_requested`，已取消就 `raise asyncio.CancelledError()`，不得繼續啟動。
   - 失敗 → `run.error` code `oauth_refresh_failed`（Web）或 `oauth_expired`（桌面）並 `await ws.close(1008, reason=<同 code>)` 後 return。run 終態 failed，`terminal_code` 即該 code。
3. 之後才進入既有流程（解析 frame 與參照 → 既有 ticker／provider 檢查 → **跳過**既有取 slot 區塊，因為已持有 → 自動預留 → 分析）。

單檔 `/stream` 路徑：取 slot 改用 `gate.try_claim(run.id)`，失敗仍是現有 `run_busy`；其餘不變。**D56 §1 所說「單檔不變」更正為：單檔的 gate 實作會換，但對外行為不變。**

測試：fake clock 讓等待跨過 token 的 `expires`（應在取得 slot 後才檢查並刷新）；hook 執行中取消；hook 回舊 token。

## A2 wait_claim 取消契約

- 回傳 `False` **只代表逾時**。取消一律讓 `asyncio.CancelledError` 往上傳（包含呼叫端 cancelled 條件成立時主動 raise）。每次 `try_claim` 前先檢查取消與期限。
- 逾時：`run.error` code `slot_timeout`，`await ws.close(1008, reason="slot_timeout")` 後 return。
- executor 的 finally：只在 `slot_held` 時 `gate.release(run.id)`。移除原本 `server.py:1279-1281` 的 dict 清除，任何路徑都不得重複 claim。
- `RunManager._track` 認得 `run.waiting_slot` → `phase="waiting_slot"`；之後任何推進事件照既有映射覆寫 phase。
- BatchManager 等待已被取消的 `run.task` 時，必須捕捉該 task 的 CancelledError（不得讓 BatchManager 自己的 task 被帶成取消），讀 run 終態後收斂批量。
- 測試：claim 前取消、等待中 `task.cancel()`、持有者丟例外後釋放、兩次 cancel、逾時的 terminal_code 為 `slot_timeout`。

## A3 預留與 run 的關聯要能在硬崩潰後對帳

- `cost_reservations` 新增 `run_id TEXT`（schema v3 一起遷移）。`cost_guard.reserve(..., run_id=None)`：有 run_id 時在**同一交易**寫入。executor 自動預留時傳 `run.id`（單檔亦同；前端預先預留的舊路徑維持不帶 run_id）。
- 預留成功、設定 `run.reservation_id` 與 `run.engine_started = True` 之後，**立即嚴格寫入** runs metadata（失敗則結算預留並以 `storage_unavailable` 結束該 run，不開始計費）。
- `run.batch_id/batch_index/batch_total` 在 `start_or_attach` 的**第一次** `_save_meta` 之前設定。
- `RunManager.recover`：對 interrupted 的 run，若 `reservation_id` 為空，以 `cost_reservations.run_id` 找出未結算預留，依 `engine_started` 走既有 `settle_run_end` 語意。
- 未帶 run_id 的舊預留仍靠既有 15 分鐘 TTL 過期（既有行為，文件註明）。
- 測試：在「reserve commit 後、metadata 寫入前」模擬崩潰，重啟後該預留被結算。

## A4 秘密清洗：所有出口

建立 run 範圍的 `Scrubber`（`engine/` 新模組或放 `secret_refs.py`）：

- 來源：start frame 的秘密欄位值（provider auth `api_key/access/refresh`、`data_config.key_id/secret`、`alpha_vantage_key`、`finmind_token`、webhook URL 與 token 類欄位、local `base_url` 中的帳密）＋**每次解析參照得到的實際值**（`secret_refs` 解析時回報給呼叫端的 scrubber，不依賴全域 `_seen`）＋參照字串本身。`telegram_chat_ids` 不是憑證，不納入。
- 套用方式：遞迴走結構（dict／list／str），在 **JSON 序列化之前**逐字替換，長字串優先；之後再套既有 `redact`／`scrub_key_shapes`／`scrub_urls_and_tokens`。
- 套用位置（全部）：`Run.emit` 入口（清洗後的副本才進 events、replay、_fan、_persist_event）；`captured_events` 與 session 寫入；`cost_report` 與 `_save_report`；executor 的 stderr log 與 traceback；`RunManager._scrub`；BatchManager 的 `batch.error`、`item.error`。
- 短秘密：批量建立時秘密欄位值少於 8 字元 → 400 `invalid_batch`（訊息只說「某憑證欄位格式不符」，不給值）。單檔路徑不拒絕，但 scrubber 對秘密欄位值不論長短都清洗。
- run 終態後清掉 scrubber 與 start frame 參考；BatchManager 範本在批量終態清掉。
- 測試（單檔與批量都要）：成功／失敗／取消／replay；隨機無前綴 token、無 `sk-` 的 local key、含 token 的 webhook URL、含 `"` 與 `\` 的秘密、彼此前綴重疊的兩個秘密；搜尋 DB 全部相關表、所有 GET、WS 事件、log。既有事件型別與正常報告內容不變。

## A5 冪等指紋

- 指紋 = sha256（canonical JSON：排序鍵、無空白）of `{tickers（正規化去重後）, start 範本全部欄位}`，其中秘密欄位值替換為：是參照字串 → 保留參照字串；是明文 → 固定字串 `"<secret>"`。
- 政策（寫進 API 說明）：只輪替憑證、其他相同 → 視為同內容，回原批量；任何非秘密設定不同（pro_config、tw_dexter、tw_data_mode、資料來源、webhook 結構等）→ 409 `idempotency_conflict`。
- 「同 owner 已有 active 批量」檢查與建立在**同一把鎖、同一交易**內。
- 測試：同鍵改 quick model／分析師／webhook／tw_dexter → 409；同鍵只換 API key → 原批量。

## A6 項目成功判準（正常與 recover 一致）

- 只有 `run.state == "completed"` **且** `session_id` 有值 → item `completed`。
- `completed` 但無 session → item `failed`/`session_persist_failed`，批量停止 `stopped`/`storage_unavailable`。
- run 結束後 BatchManager 以嚴格讀取從 DB 重讀該 run 資料列；讀不到或欄位缺失 → 同上停批。
- recover 時同一規則。

## A7 recover 與清理

- `RunManager.recover` 的清理（7 天／200 筆）**排除**屬於非終態批量的 run（`runs.batch_id` 對應 `batches.state IN ('queued','running')`）。
- 順序維持：runs recover（不清理批量 run）→ batches recover → 之後才可能清理。

## A8 API 細節更正

- 未知或不允許的欄位：一律 400 `invalid_batch`，訊息只列**白名單中**的合法欄位名，不回顯收到的鍵名。
- tickers 正規化順序：去頭尾空白 → 英文轉大寫 → 去重保序 → 長度／數量檢查。
- 建立後到第一檔取得 slot 前，`current_index` 可能為 `null` 而批量 `queued`；前端顯示「準備中」，不得計算 `null + 1`。
- `D56` 根因一節最後一句改為：「新設計移除了批量對前端連線的依賴；CostGuard 擋下仍是預期中的停批行為。」

## A9 前端補充（D53）

- Analyze 提示只在**有 active 批量**時顯示（以 `GET /batches?limit=1` 的 `active` 為準）；`recallRun()` 指到已終態的批量 run → `forgetRun()`、不顯示提示、不接上。兩個來源以 `/batches` 為準。
- `current_index === null` → 顯示「準備中」。
- 「停止」送出失敗（網路錯誤／5xx）→ 恢復按鈕可再按，重送同一個 batch_id 的 cancel（冪等）；只有成功回應後才進入「停止中…」。

## A10 單檔路徑受影響的範圍（明列）

A1（gate 實作）、A3（自動預留帶 run_id、嚴格 metadata 寫入）、A4（清洗）、D56 §4（RunSink.scope）會影響所有 run。對外行為與事件型別不變；既有測試必須全過，並各自補單檔回歸測試。

## §P 修正 D55 結論

- 結論改為：**有條件可行**。正式預設並行數維持 **1**；並行 2 只作為經授權的受控試驗值，量測吞吐、延遲、429、重試次數、取消清理與成本後再決定。
- 共用資源清單補上（D57 指出的漏項）：
  - 台股 `FreeTaiwanSources`／`MARKET` 單例的快取與節流（`tw_free_sources.py:123-165` 無鎖；`tw_graph_tools.py:41-43,441,472,669`）：需共享的來源級節流＋同 key 只抓一次。
  - `tw_graph_tools` 的 `_health` 計數與健康檔 `.tmp` 寫入、`_ohlcv_cache`（`:129-130,193-234,269-280`）：加鎖與原子替換；健康資料是全域觀測，不當 run 證據。
  - **同代號並行不只發生在同一批量內**：單檔 Analyze 與批量、不同 owner 的批量都可能同代號；`stockstats_utils.py:209-245` 共用 CSV 快取且直接 `to_csv`，需每 key 鎖＋原子替換。`trading_graph.py` 的結果檔與 checkpoint 列為條件風險（目前 `full_debate.py:802` 直接 `astream`，未走那些路徑）。
  - 記憶檔：`full_debate.py:923` 在 event loop 上同步呼叫，同一 loop 的兩個 run 不會在該段交錯；風險限於其他 thread／程序。
  - ContextVar 化 dataflows 設定：每個 run 從深拷貝的 defaults＋自己的 config 初始化，不從可變全域 fallback 繼承；set 之後 finally reset；以 `test_tw_d47.py:229-254`（真 LangGraph）為傳遞證據；`interface.VENDOR_METHODS/VENDOR_LIST` 的註冊移到服務啟動時一次完成。
  - env：Alpaca 實際是 `APCA_API_KEY_ID`／`APCA_API_SECRET_KEY`；`FRED_API_KEY` 為工具呼叫時讀取的共享唯讀設定；`secret_refs._store/_seen` 與 web_oauth 的刷新鎖是共享狀態。
  - 429：`codex_llm.py` 經 SDK client，`trading_graph.py:195-199` 會轉傳 `llm_max_retries`，未指定時用 SDK 預設；要先確認實際生效的重試設定再決定補哪一層，避免重複重試。
  - CostGuard：目前在同一 event loop 同步呼叫所以 admission 可序列化；若移到 threadpool／子程序需 `BEGIN IMMEDIATE` 或單一 writer。
  - 容量制 gate：zombie meter 仍有 inflight 時持續占格，直到真正結束；本機獨占需同時排除任何仍有 inflight 的雲端 run。
