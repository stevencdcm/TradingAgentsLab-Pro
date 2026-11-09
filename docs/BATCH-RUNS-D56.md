# D56 批量分析改由引擎執行：修訂規劃與契約（HER-30）

> 補充：依 D57 複審，實作規則以 `docs/BATCH-RUNS-D58.md` 為準（兩者衝突時以 D58 為準）。

作者：claude-code（Opus 5.5），session 9c20ad14-a9f1-4819-83c8-af84e63c4608，2026-11-09。
取代 `docs/BATCH-RUNS-D50.md` 的設計與 API 契約（D50 保留作歷史；根因證據沿用）。依據 D51 審查（Astra，結論「需修改後再做」）逐項修正，對照表在文末。
規範：`/Users/steven/AGENTS.md`。中央：hermes-infra / HER-30。

## 階段

| 階段 | 內容 | 執行者 |
|---|---|---|
| D50 | 初版規劃（已被 D56 取代） | 主控 |
| D51 | 初版審查：需修改 | Astra |
| D55 | 並行可行性評估（只評估） | 主控 |
| D56 | 修訂規劃（本文件） | 主控 |
| D57 | D56＋D55 複審（唯讀） | Astra |
| D52 | 後端實作（D57 通過後） | sol |
| D53 | 前端實作（D57 通過後） | Sonnet |
| D54 | 程式審查（唯讀） | Astra |

worker 不 commit、不 push、不部署；主控整合後開 Draft PR。

## 根因（修正措辭）

- **已證實的設計缺陷**：批量佇列只活在分頁 JS 裡（`BatchRunner.tsx:84-88` 卸載即設 stop、`:143-145,213-215` 卸載後迴圈 return）。關頁、手機背景回收、切頁都會讓後續項目永遠不啟動，重開也沒有進度。
- **11-09 那次最可能的原因**是上述情況（HIMX 完成後引擎沒有第二個 run 啟動紀錄）。但不能只憑「沒有第二筆 run」排除其他會讓前端不送出第二檔的路徑：CostGuard 預留被擋時前端會直接停批（`run-analysis.ts:128-139`、`BatchRunner.tsx:198-209`，被擋時沒有 run 紀錄）；半開的 WebSocket 沒有閒置逾時，`done` 只在 close 時 resolve（`engine-client.ts:1403,1412-1414`），迴圈可能卡住。
- 正常順序沒有「前一檔 slot 尚未釋放」的競態：slot 在 executor finally 釋放（`server.py:1274-1281`）之後才 `_finish`。
- 新設計讓批量完全不依賴前端連線，以上路徑一併消失。

## 架構

```
POST /batches ──> BatchManager（engine/batch_manager.py）
                    │ 每檔：OAuth 檢查 → RunManager.start_or_attach(owner, frame, _execute_run, batch=(id, idx, total))
                    │        └─ _execute_run：等 slot（批量 run 才等）→ 自動 CostGuard 預留 → 原有分析流程
                    │ 等 run 終態 → 從 run 讀結果 → 寫 batch_items → 下一檔
SQLite: batches / batch_items / runs.batch_id, batch_index
GET /batches, GET /batches/{id}, POST /batches/{id}/cancel（前端輪詢）
```

### 1. Run slot：共用 gate，等待放在 run 自己的 executor 裡

- 把 `server.py` 的 `run_slot` 字典抽成 `SlotGate` 物件（可放 `engine/run_slot.py`），提供：
  - `try_claim(run_id) -> bool`：**同步、無 await**，條件為「未被佔用且 `graph_thread_still_running()` 為 False」，成立即設佔用者。
  - `release(run_id)`：只有佔用者本人能釋放。
  - `async wait_claim(run_id, cancelled: Callable[[], bool], timeout_s) -> bool`：迴圈 `try_claim`，失敗則 `await asyncio.sleep(1)`；被取消或逾時回 False。
  - 介面保留 `capacity` 概念（目前固定 1），供 D55 方案 A 日後改容量制。
- 單檔 `/stream` 路徑行為不變：`try_claim` 失敗 → 現有 `run_busy` 事件與 1008 關閉。
- **批量 run**（`run.batch_id` 不為 None）：executor 在原本取 slot 的位置改呼叫 `wait_claim`；等待期間發 `{"type":"run.waiting_slot"}` 事件與 heartbeat，run 狀態 `phase="waiting_slot"`。逾時（45 分鐘）→ `run.error` code `slot_timeout`，run failed。被取消 → 走既有 `user_cancelled`。
- 因為等待在 run 內部，run 已存在並持久化，取消、重啟 recover、成本結算都沿用 RunManager 既有路徑；slot 取得與建立之間沒有空隙問題。
- `client_run_key = f"batch:{batch_id}:{index}"`，每檔只有一次嘗試，**絕不**以同鍵重新嘗試已終態的 run。
- `run.batch_id/batch_index/batch_total` 只由 BatchManager 經 `start_or_attach(..., batch=...)` 在 `create_task` 前設定；**start frame 裡同名欄位一律忽略**（客戶端不能自稱批量 run 來取得等待行為）。
- 測試：兩個提交者（批量項目＋單檔 `/stream`）同時看到空 slot 的交錯；lingering graph（`_ZOMBIE_METERS` 非空）時批量等待、單檔 run_busy。

### 2. CostGuard：由 executor 自動預留，BatchManager 不碰預留

- 批量 start frame **不帶 `reservation_id`**。executor 既有的「無預留時以驗證過的 provider_config／pro_config 自動預留」路徑（`server.py:967-998`）負責預留，參數一定與實際計費模型一致，`override=False`；成功後綁定 `ws.run.reservation_id`（`server.py:1001-1004`），之後只由 RunManager／executor 既有邏輯結算。
- 順序：等到 slot → 預留 → 開始計費。等待 slot 期間不持有任何預留。
- 因此 OAuth 檢查失敗、取消、start 拋錯等切點都發生在預留之前，不會有孤立預留。
- 被擋：run 以 `cost.blocked` 事件與 `close_code=(1008,"cost_guard_blocked")` 結束 → BatchManager 讀到 `terminal_code == "cost_guard_blocked"` 時**停止整批**：該項 `failed`/`cost_guard_blocked`，剩餘 `skipped`，批量 `stopped`/`cost_guard_blocked`。不 override。
- 移除 D50 的 `reserve` 請求欄位。
- 測試：每個切點（等待中取消、預留被擋、預留後執行中取消、執行中引擎崩潰重啟）之後 `cost_reservations` 沒有未結算的批量預留。

### 3. OAuth：必須驗證拿到有效 token

- 每檔建 run **之前**，若 `provider_config.auth.type == "oauth"`：
  - Web 版：`web_app.py` 透過 `app.state.batch_oauth_refresh` 註冊 hook；BatchManager 以 `run_in_threadpool` 呼叫（底層 `oauth.ensure_fresh`）。`web_oauth.py:141-166` 在找不到 Node、刷新錯誤或逾時時會回傳**舊憑證**，所以必須檢查回傳：存在、有 access、`expires`（> 1e12 視為毫秒，否則秒）晚於現在＋5 分鐘。不符 → 批量 `stopped`/`oauth_refresh_failed`。成功時把新的 `expires` 寫回該檔使用的 frame（參照值不變，executor 會解析到新 token）。
  - 桌面版（無 hook，frame 內是明文 token）：只看 `expires`，已過期或 5 分鐘內到期 → 批量 `stopped`/`oauth_expired`。
- 停止時：該項 `skipped`（還沒建 run），剩餘 `skipped`，`error` 說明「請重新登入後重跑剩餘項目」。
- 測試：hook 回舊 token、hook 拋錯、hook 逾時、毫秒／秒兩種單位、桌面明文即將到期。

### 4. 台股快照 owner（既有缺陷，一併修）

- `RunSink.scope` 目前是空的（`run_manager.py:102-106`），`server.py:1179-1180` 從 `scope.tal_owner` 取 owner 寫快照 → proxy 模式下所有 run（不只批量）的快照都寫成 `local`，`GET /tw/run-snapshots`（`server.py:511-518`）用真 owner 查得到 404。
- 修法：`RunSink.__init__` 設 `self.scope = {"tal_owner": run.owner}`（來自可信的 run.owner，不從 start frame 取）。
- 測試：兩個 owner 各跑一檔台股（單檔與批量各一），各自讀得到自己的快照、讀不到對方的。

### 5. 秘密：請求範圍的秘密集合

- BatchManager 建立時從 start 範本收集所有秘密字串：`provider_config.auth` 的 `api_key/access/refresh`、`data_config.key_id/secret`、`alpha_vantage_key`、`finmind_token`、每個 webhook 的 URL 與 token 類欄位、`telegram_chat_ids` 值；參照字串本身也收。Web 版每次 OAuth 刷新後不需要額外值（frame 只有參照）；桌面版明文已在集合內。
- 同一集合傳給每個項目的 Run（新增 `Run.secret_values`），`RunManager._scrub` 與 `_clean_event` 在既有規則之外再逐字替換集合內的值（長度 ≥ 8 才替換，避免誤傷）。**單檔 run 也一併套用**（RunSink 從自己的 start frame 收集）。
- BatchManager 寫入 DB 或回應之前，`batch.error`、`item.error` 都先經同樣的清洗。
- 範本只存在記憶體；批量進入終態立即刪除參考。DB 只存非秘密欄位。
- **不用會回顯輸入的 Pydantic 422**：`POST /batches` 讀 `request.json()` 後自行驗證，錯誤訊息只提欄位名稱，不含任何值。
- 測試：無前綴的隨機字串 token、沒有 `sk-` 前綴的 local key、含 token 的 webhook URL，分別讓 executor 拋出含該值的例外，搜尋 DB（batches、batch_items、runs、run_events、sessions）、所有 GET 回應、事件與 log，都不得出現。

### 6. 冪等與持久化

- `batches` 加唯一索引 `(owner, client_key)`。建立流程在 asyncio 鎖內：查同鍵 → 有則比對指紋（`sha256(tickers, provider, model, auth_kind, engine, report_language, trade_date, tw_flow)`），相同回原快照（含已 interrupted 的舊批量），不同回 409 `idempotency_conflict`；無則在**單一交易**內寫入 batch 與全部 items，**寫入成功後才啟動**；寫入失敗回 503 `storage_unavailable`，不啟動任何東西。
- 項目狀態轉換使用會回報錯誤的寫入（不同於 `RunManager._db` 的吞錯）：失敗重試一次；仍失敗則目前那檔照常跑完，但**不再啟動下一檔**，批量 `stopped`/`storage_unavailable`（盡力寫入）。
- `client_batch_key`：8–80 字元 `[A-Za-z0-9_-]`；保留到該批量資料列存在為止。

### 7. 重啟對帳

- 啟動順序：`run_manager.recover()` 先跑，再 `batch_manager.recover()`。
- 對每個非終態批量：
  - 以 `runs.batch_id/batch_index` 找每個項目的 run（不依賴 `batch_items.run_id` 是否已寫入，處理「run 已建、item 尚未記 run_id」的窗口）。
  - run completed 且有 session → item `completed`（補結果）；run 是其他終態 → item 依 run（`failed`／`cancelled`／`interrupted`）；找不到 run 的 `running`/`waiting_slot` 項目 → `interrupted`；`pending` → `skipped`。
  - 批量：若有 `cancel_requested` → `cancelled`，否則 `interrupted`/`server_restart`。
  - 計數依項目重新計算。不重跑任何項目。
- item 狀態集合新增 `interrupted`。

### 8. Schema

- `storage.py` SCHEMA_VERSION 2 → 3：新增 `batches`、`batch_items`，`runs` 新增 `batch_id TEXT`、`batch_index INTEGER`（ALTER TABLE ADD COLUMN），加索引。舊資料庫自動升級。
- **舊版程式不能開新版資料庫**（`storage.py:130-134` 會拒絕較新 schema）。部署前必須備份 sessions.db（沿用 D49 作法）；回退 = 停服務、還原備份、啟動舊版。
- 測試：v2 資料庫（含既有 runs/sessions）升級後資料完整、新表可用。

## API 契約（D52 實作、D53 呼叫；變更需主控同意）

通則：全部掛 `dependencies=bearer`、`_owner(request.scope)` 隔離；時間一律 Unix 秒（float）；index 從 0 起；錯誤一律 `{"detail": {"code": "...", "error": "..."}}`，訊息不含輸入值。驗證失敗（token／cookie／Origin／proxy）沿用既有中介層的 401／403。

### POST /batches

```json
{
  "tickers": ["HIMX", "2330"],
  "client_batch_key": "k_8to80chars",
  "start": {
    "trade_date": "2026-11-09",
    "provider_config": {}, "data_config": {}, "webhooks": [], "telegram_chat_ids": {},
    "engine": "pro", "pro_config": {}, "alpha_vantage_key": "", "tw_flow": "full", "tw_dexter": false,
    "tw_data_mode": null, "finmind_token": "", "report_language": "zh-TW"
  }
}
```

- `start` 白名單即上列欄位；出現 `ticker`、`reservation_id`、`client_run_key`、`resumable`、`batch_*` 或其他欄位 → 400 `invalid_batch`（訊息列出欄位名）。
- `tickers`：1–50 個，字串、去頭尾空白後 1–32 字元；去重保序（回應的 `items` 以去重後為準）。
- `provider_config` 必須能通過 executor 使用的同一個解析函式（`llm_providers.ProviderConfig.from_dict`，無網路）且 provider 不為空 → 否則 400 `no_provider`。批量不跑 stub。
- secret 參照用途檢查（`secret_refs` 現有規則）失敗 → 400 `secret_ref_rejected`。
- 200 `{"batch": BatchSnapshot}`（新建或同鍵同內容重送）。
- 409 `{"detail": {"code": "batch_active", "batch_id": "...", "error": "..."}}`：同 owner 已有進行中批量。
- 409 `idempotency_conflict`：同鍵不同內容。
- 503 `storage_unavailable`。

### GET /batches?limit=N

`limit` 1–20，預設 5。回 `{"batches": [BatchSnapshot, ...]}`，新到舊。前端以第一筆為「最新批量」。

### GET /batches/{batch_id}

`BatchSnapshot`；不存在或非本人 → 404 `batch_not_found`。

### POST /batches/{batch_id}/cancel

冪等。回 `BatchSnapshot`：`cancel_requested=true`；目前那檔可能還在收尾，所以 `active` 可能仍為 true，直到該 run 終態。已終態的批量原樣回傳。404 同上。

### BatchSnapshot

```ts
type BatchState = 'queued' | 'running' | 'completed' | 'cancelled' | 'stopped' | 'interrupted';
type ItemState = 'pending' | 'waiting_slot' | 'running' | 'completed' | 'failed' | 'cancelled' | 'skipped' | 'interrupted';

interface BatchSnapshot {
  batch_id: string;
  state: BatchState;
  active: boolean;                 // state === 'queued' || state === 'running'
  cancel_requested: boolean;
  terminal_code: null | 'user_cancelled' | 'cost_guard_blocked' | 'oauth_expired' | 'oauth_refresh_failed'
               | 'storage_unavailable' | 'server_restart';
  error: string | null;            // 已清洗、給人看的一句話
  provider: string | null; model: string | null; auth_kind: string | null; engine: string | null;
  trade_date: string | null; report_language: string | null;
  created_at: number; started_at: number | null; ended_at: number | null;
  total: number;
  counts: Record<ItemState, number>;   // 各狀態數量，總和恆等於 total
  current_index: number | null;        // 目前 waiting_slot/running 的項目；沒有則 null
  cost: {
    known_usd: number;             // 所有項目（含失敗／取消的部分計量）已知成本加總
    unknown_items: number;         // 已開始計費但成本未知的項目數
    partial: boolean;              // 有任何部分計量或未知成本
  };
  items: BatchItem[];
  current_run: null | {
    run_id: string; phase: string | null; steps_done: number; elapsed_s: number;
    since_last_event_s: number | null; last_event_type: string | null; cancelling: boolean;
  };
}

interface BatchItem {
  index: number; ticker: string; state: ItemState;
  run_id: string | null; session_id: string | null;
  stance: string | null; conviction: number | null;     // 取自 session.complete 的 decision
  estimated_cost_usd: number | null; cost_known: boolean | null; cost_partial: boolean;
  elapsed_s: number | null; started_at: number | null; ended_at: number | null;
  error_code: string | null; error: string | null;      // 已清洗
}
```

語意：

- `completed`＝佇列走到最後（不論各項成敗；全部失敗也是 completed，靠 `counts` 表達）。
- `stopped`＝因 `terminal_code` 所列原因提前停止；`cancelled`＝使用者停止；`interrupted`＝引擎重啟。
- 單檔失敗（含 `ticker_unsupported`、`run_error`、`slot_timeout`）→ 該項 `failed`，繼續下一檔。只有 cost_guard_blocked／OAuth／storage 會停整批。

### RunStatusSnapshot 新增欄位

`batch_id: string | null`、`batch_index: number | null`、`batch_total: number | null`（`GET /runs/*`、`/runs/active`）。

## 前端（D53）

檔案範圍：`desktop/src/components/BatchRunner.tsx`、`BatchRunner.module.css`、`desktop/src/lib/engine-client.ts`（批量 API、型別、RunStatusSnapshot 新欄位）、`desktop/src/lib/run-analysis.ts`（抽出 `buildRunStart(opts)`：回傳上述 start 範本，不預留、不開 WS；`runAnalysis` 單檔行為與輸出不變）、`desktop/src/pages/Watchlist.tsx`、`desktop/src/pages/Analyze.tsx`（只改自動續接）、`desktop/src/i18n/{en,zh-TW}/batch.ts`（必要時 `pages.ts`）、`desktop/tests/unit/` 新測試。

### 還原與輪詢

- **Watchlist 無條件掛載 BatchRunner**（移除 `entries.length > 0 || batchRunning` 條件；空清單時 BatchRunner 自己只在有批量時顯示內容）。鎖定勾選依 BatchRunner 回報的「伺服器端 active」。
- 掛載時 `GET /batches?limit=1`。active → 每 2 秒 `GET /batches/{id}`；錯誤退避 2→5→10 秒並顯示「暫時無法取得進度，重試中」；`visibilitychange` 回前景立即重抓。每個請求帶遞增序號，**較舊的回應不得覆蓋較新的狀態**；卸載時中止進行中的請求與計時器。
- 終態 → 停止輪詢，顯示結果。使用者可「清除」**已終態**的批量（localStorage 記 batch_id，try/catch；進行中的批量不能隱藏）。被隱藏的最新批量不顯示；有新批量時自然取代。owner 由伺服器隔離，換帳號自然看到自己的資料。
- 卸載、切頁、關頁**不得**呼叫 cancel；只有「停止」按鈕呼叫一次 cancel，之後顯示「停止中…」直到 `active=false`。

### 啟動

- 「執行」→ `resolveActiveProvider()`、retired model 檢查 → `buildRunStart` → `POST /batches`（`client_batch_key` 用 `crypto.randomUUID()`，同一次點擊重送沿用同鍵）。409 `batch_active` → 直接載入並顯示該批量。400／503 → 依 code 顯示對應 i18n 訊息。

### Analyze 自動續接

- 決定最終候選 run 之後（含 `recallRun()` 記住的那個）再判斷：候選帶 `batch_id` → **不接上**，`forgetRun()`，改顯示一行「批量分析進行中（第 i/N 檔），到觀察清單查看」並附連結（i = batch_index+1，N = batch_total）。
- 另在掛載時 `GET /batches?limit=1`：批量 active 但目前沒有 run（例如 waiting_slot 之間）也顯示同一行提示。
- 批量項目不呼叫 `rememberRun`。

### 顯示

1. 標題列：狀態文字＋整體進度條（已終態項目數／total），計數「完成 x・失敗 y・略過 z・剩 n」。
2. 目前項目卡：代號、階段（沿用 `RunStatus` 的 phase 對照；沒有可匯出的就抽成共用函式）、步驟數、已耗時、「最後更新 N 秒前」；`since_last_event_s > 120` 顯示「可能卡住」提示；`waiting_slot` 顯示「等待其他分析結束後開始」。
3. 預估剩餘時間：已完成項目平均耗時 × 剩餘數（至少一檔完成才顯示）。
4. 每列：狀態標籤（8 種）、結論與信心、成本（partial／unknown 要標示）、耗時；完成列「查看報告」到 History 該 session；失敗列用 `describeEngineError`。
5. 結束摘要：沿用偏多／偏空／需複核／中性／失敗；總成本用 `cost`（有未知或部分時標示）；`terminal_code` 用一句話說明原因。
6. 提示：「批量在伺服器端執行，可以關閉此頁，回來會看到進度。」
7. 手機寬度可用。

## 測試與驗收證據

### D52（engine）

- **端到端主線**：真 REST → BatchManager → RunManager → `_execute_run`，只 mock 模型／資料呼叫（不 mock 整個 executor）；三檔、**零 WebSocket 訂閱者、零輪詢**，全部終態；每檔 executor 只被呼叫一次；sessions 三筆；預留全部結算；之後以新 client 讀回快照正確。
- §1–§8 各自列出的測試。
- 單檔失敗繼續；取消（等待中、執行中、冪等）；cost block 停整批；409 batch_active；owner 隔離（他人 404、不能取消）。
- Web 層：新路由經 `build_web_app` 的既有驗證；補錯 owner、缺 cookie、錯 Origin、偽造 `X-TAL-*` 標頭的拒絕測試。
- 既有 engine 測試全數通過。

### D53（vitest）

- 掛載時從伺服器還原（模擬關閉重開）；空 Watchlist 也能還原進行中批量。
- 輪詢更新、終態停止輪詢、錯誤退避、回前景立即重抓、舊回應不覆蓋新狀態。
- 卸載不 cancel；停止只呼叫一次。
- 409 顯示既有批量；失敗列不顯示為完成。
- Analyze：記住的 run 屬於批量時不接上並顯示提示。
- `tsc`、全部既有 vitest 通過（Node 26 需 `--no-experimental-webstorage`）。

### 整合（主控）

本機以 mock provider 跑三檔批量，中途關頁再開、切頁再回，確認持續執行與正確顯示；附 log／截圖。`/lab-pro` 基底路徑與真代理外層的端到端驗證列為部署前檢查（本階段不部署）。

## 不在範圍

不 merge、不部署；不改單次 Analyze 執行路徑（除 §4、§5 對所有 run 生效的修正與 RunStatus 新欄位）；不自動 override；重啟不自動續跑；並行執行另見 D55（本階段容量固定 1）。

## D51 發現對照

| D51 發現 | 處理 |
|---|---|
| slot 等待兩案皆不成立 | §1：SlotGate，等待在批量 run 的 executor 內；同步 try_claim；不重用終態鍵；交錯測試 |
| reserve→OAuth→start 未結算窗口 | §2：BatchManager 不預留，executor 在取得 slot 後自動預留並綁 run；OAuth 在建 run 前 |
| OAuth hook 不保證有效 | §3：驗證回傳與 expires（秒／毫秒），失敗停整批 |
| reserve 參數可與實際不符 | §2：移除 reserve 欄位，由 executor 以驗證過的設定推導；cost block 停整批 |
| 台股 owner 未傳遞 | §4：RunSink.scope 由 run.owner 填入；兩 owner 測試 |
| recover 狀態不一致 | §7：先 runs 後 batches、以 runs.batch_id 對帳、新增 interrupted |
| scrub 不足 | §5：請求範圍秘密集合、單檔也套用、不用回顯的 422、例外路徑測試 |
| 冪等／持久化不足 | §6：唯一索引、指紋、交易寫入後才啟動、503、項目寫入失敗停批 |
| 根因措辭 | 「根因」一節改寫 |
| 前端還原缺口 | 前端「還原與輪詢」「Analyze 自動續接」；batch_total |
| schema 不可回退 | §8：備份／還原策略 |
| 契約細節（index、時間、counts、成本、白名單、錯誤碼、key 規則） | API 契約一節 |
