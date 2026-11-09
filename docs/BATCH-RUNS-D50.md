# D50 批量分析改由引擎執行（HER-30）

> 狀態：設計與 API 契約已由 `docs/BATCH-RUNS-D56.md` 取代（依 D51 審查修訂）。本文件保留作歷史與根因證據。

作者：claude-code（Opus 5.5），session 9c20ad14-a9f1-4819-83c8-af84e63c4608，2026-11-09。
中央任務：hermes-infra / HER-30。規範：`/Users/steven/AGENTS.md`（G1–G4）。

## 階段

| 階段 | 內容 | 執行者 |
|---|---|---|
| D50 | 根因與規劃（本文件） | 主控 Opus |
| D51 | 規劃審查（唯讀） | Codex gpt-6-astra |
| D52 | 後端實作（engine/） | Codex gpt-6-sol |
| D53 | 前端實作（desktop/src/） | Claude Sonnet |
| D54 | 程式審查（唯讀） | Codex gpt-6-astra |

D52 與 D53 依本文件「API 契約」平行進行，各自只改自己的檔案範圍。worker 不 commit、不 push、不部署；由主控統一整合、commit、開 Draft PR。

## 症狀與根因

使用者回報：批量分析「只會跑第一個」。

證據（正式 Web 版 `~/Library/Application Support/Trading Agents Lab Web/sessions.db` 的唯讀副本）：

- 2026-11-09 06:16:57 `run_195c97c20f47ac4f` HIMX completed（477 秒，約 06:24 結束）。之後引擎**沒有收到任何新的 run 啟動**，資料庫最後修改時間也是 06:24。
- 2026-11-07 23:27 INTC → 23:36:01 QCOM：前一檔結束後一秒內開始下一檔，代表**分頁開著時**佇列可以正常往下跑。

根因：批量佇列（`desktop/src/components/BatchRunner.tsx` 的 `onRun` 迴圈）只活在瀏覽器分頁的 JS 記憶體裡。分析本身已經是引擎持有的可續接 run（D7），但「跑完這檔→啟動下一檔」是前端在做。只要：

1. 關閉或重新整理網頁（手機上 Safari 切到背景常被回收），
2. 切換到其他頁面（`Watchlist` 卸載時 `mountedRef=false`，並把 `stopRequestedRef` 設為 true），

目前那一檔會在引擎裡跑完，但下一檔永遠不會被啟動。重新打開頁面時 `rows` 是空的，看不到任何批量進度。

次要缺陷（一起修）：

- `BatchRunner` 只看 `session.complete`，忽略 `run.error`／`run.finished.state != completed`：失敗的檔被標成「完成」，只是沒有結論。
- 每檔之間沒有等待 run slot：使用者在批量進行中另外按 Analyze，下一檔會撞到 `run_busy` 並被誤判。

## 設計：批量由引擎擁有

引擎新增 `BatchManager`（建議檔案 `engine/batch_manager.py`），與 `RunManager` 同一個 process、同一個 owner 範圍：

1. 前端一次送出整批（股票清單＋與單次 run 相同的 start 範本＋CostGuard 預留參數）。
2. 引擎依序執行：每檔都走現有的 `run_manager.start_or_attach(owner, frame, _execute_run)`，所以 provider 解析、secret 參照驗證、資料來源、Pro 圖、webhook、session 寫入、成本結算都與單次分析完全相同。
3. 批量與每一檔的狀態寫入 SQLite（不含任何秘密），網頁關閉、重開、換裝置都能用 REST 讀回。
4. 前端只負責「建立」「顯示」「停止」，關閉頁面不影響執行。

### 每檔流程（D52）

對第 i 檔：

1. 若已要求取消 → 剩餘全部 `skipped`，批量 `cancelled`。
2. 等 run slot 空出（狀態 `waiting_slot`；條件同 `server.py` 的 `run_slot["busy"] or graph_thread_still_running()`；每 1 秒檢查，可被取消；上限 45 分鐘，逾時該檔 `failed`/`slot_timeout` 並繼續下一檔）。為避免競態，slot 判斷與 run 建立之間不可有 await 空隙，或以 executor 回報 `run_busy` 時重新回到等待（擇一，需有測試）。
3. CostGuard：以 `cost_guard.reserve(model, auth_kind, max_tokens, extra_models)` 預留（參數來自建立請求的 `reserve`，與前端 `runAnalysis` 現行算法相同）。`CostGuardBlocked` → 該檔 `failed`/`cost_guard_blocked`，剩餘 `skipped`，批量 `stopped`/`cost_guard_blocked`（沿用現行「第一次被擋就停」，不自動 override）。
4. OAuth：若 `provider_config.auth.type == "oauth"`，每檔開始前呼叫可選的「憑證更新 hook」（Web 版由 `web_app.py` 註冊，呼叫其 `oauth.ensure_fresh`，參照會在 run 啟動時解析成新 token）。桌面版收到的是明文 token：若 `expires` 已過期或 5 分鐘內到期且無 hook，該檔 `failed`/`oauth_expired`，剩餘 `skipped`，批量 `stopped`/`oauth_expired`，訊息請使用者重新登入後重跑剩餘項目。
5. 組 start frame：範本深拷貝＋`ticker`、`reservation_id`、`resumable: true`、`client_run_key = f"{batch_id}:{i}"`（重試不會重複啟動）。
6. `start_or_attach` 後等待該 run 結束（`run.task` 完成或 `run.active` 變 false；不依賴任何 WebSocket 訂閱者）。
7. 從 run 讀結果：`state`、`session_id`、`terminal_code`、`error`（已 scrub）、成本（`estimated_cost_usd`、`cost_known`）、`elapsed_s`，以及 `session.complete` 事件裡的 `decision.stance`、`decision.conviction`。completed 且有 session → `completed`；否則 `failed`（含 `ticker_unsupported`、`run_error` 等）。**單檔失敗繼續下一檔**。
8. 每次狀態改變立即寫 DB。

取消：`POST /batches/{id}/cancel` 設旗標，若目前有 run 則呼叫 `run_manager.cancel(run)`（沿用 D10 冪等語意，已經有結果的不會被取消）；目前那檔依 run 結果記為 `cancelled` 或 `completed`，剩餘 `skipped`。

引擎重啟：啟動時 `recover()` 把未結束的批量標為 `interrupted`/`server_restart`，未開始的項目 `skipped`，執行中那檔照 run 的 recover 結果。**不自動續跑**（秘密只在記憶體裡）。

限制：每個 owner 同時最多一個進行中的批量；清單去重保序、最多 50 檔、每檔沿用現有 ticker 驗證（由 executor 回報）。

### 秘密

- 建立請求裡的 start 範本（provider_config、data_config、webhooks、telegram_chat_ids、alpha_vantage_key、finmind_token…）只存在 `BatchManager` 的記憶體，**不得寫入 DB、不得出現在任何 GET 回應、事件或 log**。批量結束（任何終態）立即丟棄。
- 建立時就做一次與單次 run 相同的 secret 參照用途檢查（`secret_refs` 現有規則），不合法直接 400，不回顯值。
- DB 只存：provider、model、auth_kind、engine、report_language、trade_date、tickers 與各項結果。
- 錯誤文字一律經現有 scrub（`_scrub` / `secret_refs.redact` / `scrub_urls_and_tokens`）。

### 資料表（SQLite，`storage.py` 現有遷移機制，SCHEMA_VERSION +1，舊資料庫可直接開啟升級）

```
batches(batch_id TEXT PK, owner TEXT NOT NULL, client_key TEXT, state TEXT NOT NULL, terminal_code TEXT, error TEXT,
        provider TEXT, model TEXT, auth_kind TEXT, engine TEXT, report_language TEXT, trade_date TEXT,
        total INTEGER NOT NULL, current_index INTEGER, created_at REAL NOT NULL, started_at REAL, ended_at REAL,
        cancel_requested INTEGER NOT NULL DEFAULT 0)
batch_items(batch_id TEXT, idx INTEGER, ticker TEXT NOT NULL, state TEXT NOT NULL, run_id TEXT, session_id TEXT,
            stance TEXT, conviction REAL, estimated_cost_usd REAL, cost_known INTEGER, elapsed_s REAL,
            started_at REAL, ended_at REAL, error_code TEXT, error TEXT, PRIMARY KEY(batch_id, idx))
```

## API 契約（D52 實作、D53 依此呼叫；有疑義由主控裁定，不可單方面改）

全部掛 `dependencies=bearer`、以 `_owner(request.scope)` 隔離，路徑與 `/runs/*` 同層。D52 必須確認 Web 代理模式（`web_app.py`）下這些路徑能經由同一 cookie／proxy 驗證通過。

`POST /batches`

```json
{
  "tickers": ["HIMX", "2330"],
  "client_batch_key": "uuid",
  "start": { "...與 streamDebate 的 start frame 相同欄位，但不含 ticker / reservation_id / client_run_key": "" },
  "reserve": { "model": "...", "auth_kind": "api_key|oauth|local", "max_tokens": 400, "extra_models": [] }
}
```

- 200 `{"batch": BatchSnapshot}`；同 owner 同 `client_batch_key` 重送 → 回傳同一批量（冪等）。
- 409 `{"detail": {"code": "batch_active", "batch_id": "..."}}`：已有進行中的批量。
- 400 `{"detail": {"code": "invalid_batch" | "secret_ref_rejected", "error": "..."}}`。
- `start.provider_config` 缺少（無 provider）→ 400 `no_provider`（批量不跑 stub）。

`GET /batches?limit=5` → `{"batches": [BatchSnapshot, ...]}`，新到舊，含進行中與已結束。
`GET /batches/{id}` → `BatchSnapshot`；404 不存在或非本人。
`POST /batches/{id}/cancel` → `BatchSnapshot`（冪等）。

```ts
interface BatchSnapshot {
  batch_id: string;
  state: 'queued' | 'running' | 'completed' | 'cancelled' | 'stopped' | 'interrupted';
  active: boolean;
  cancel_requested: boolean;
  terminal_code: string | null;      // cost_guard_blocked | oauth_expired | server_restart | user_cancelled | null
  error: string | null;
  provider: string | null; model: string | null; auth_kind: string | null;
  trade_date: string | null; report_language: string | null;
  created_at: number; started_at: number | null; ended_at: number | null;
  total: number; completed_count: number; failed_count: number; skipped_count: number; cancelled_count: number;
  current_index: number | null;
  estimated_cost_usd: number | null; cost_known: boolean;  // 已完成項目加總；任一未知則 cost_known=false
  items: Array<{
    index: number; ticker: string;
    state: 'pending' | 'waiting_slot' | 'running' | 'completed' | 'failed' | 'cancelled' | 'skipped';
    run_id: string | null; session_id: string | null;
    stance: string | null; conviction: number | null;
    estimated_cost_usd: number | null; cost_known: boolean | null;
    elapsed_s: number | null; started_at: number | null; ended_at: number | null;
    error_code: string | null; error: string | null;
  }>;
  current_run: null | {             // 目前那檔的即時狀態，取自 run.status()
    run_id: string; phase: string | null; steps_done: number; elapsed_s: number;
    since_last_event_s: number | null; last_event_type: string | null; cancelling: boolean;
  };
}
```

`RunStatusSnapshot`（`GET /runs/*`）新增選填 `batch_id`、`batch_index`。

## 前端（D53）

檔案範圍：`desktop/src/components/BatchRunner.tsx`、`BatchRunner.module.css`、`desktop/src/lib/engine-client.ts`（只加批量 API 與型別、`RunStatusSnapshot` 新欄位）、`desktop/src/lib/run-analysis.ts`（抽出「組 start 範本＋reserve 參數」的函式，`runAnalysis` 單檔行為不變）、`desktop/src/pages/Watchlist.tsx`、`desktop/src/pages/Analyze.tsx`（只改自動續接那段）、`desktop/src/i18n/{en,zh-TW}/batch.ts`、`desktop/tests/unit/` 新測試。

行為：

- 按「執行」→ 解析 provider／model（沿用 `resolveActiveProvider`、retired model 檢查）→ 組範本 → `POST /batches`。409 時直接顯示那個進行中的批量。
- 掛載時 `GET /batches?limit=1` 取最新批量並顯示；進行中每 2 秒輪詢 `GET /batches/{id}`，錯誤時退避（2→5→10 秒）並顯示「暫時無法取得進度，重試中」，`visibilitychange` 回到前景立即重抓。不使用 WebSocket。
- 卸載、切頁、關閉頁面**不得**取消批量；只有「停止」按鈕呼叫 cancel，按下後顯示「停止中…」直到狀態改變。
- Watchlist：伺服器端有進行中的批量時，即使清單為空也要顯示 BatchRunner，勾選鎖定；重開頁面後鎖定狀態也要正確。
- Analyze 自動續接：`getActiveRuns()` 回來的 run 若帶 `batch_id` 且不是本分頁記住的 run，不自動接上（避免把批量項目當成單次分析、誤按停止）；改為顯示一行「批量分析進行中（i/N），到觀察清單查看」的提示即可。
- 不再在批量項目上呼叫 `rememberRun`。

顯示（儘量清楚）：

1. 標題列：批量狀態文字＋整體進度條（已結束項目數 / 總數），計數「完成 x・失敗 y・略過 z・剩 n」。
2. 目前項目卡：股票代號、階段（沿用 `RunStatus` 既有的 phase 文字對照，若沒有可匯出的對照就在 i18n 新增）、已完成步驟數、已耗時、「最後更新 N 秒前」；超過 120 秒沒有新事件顯示「可能卡住」提示（不自動處理）；`waiting_slot` 顯示「等待其他分析結束」。
3. 預估剩餘時間：已完成項目平均耗時 × 剩餘數（至少 1 檔完成後才顯示）。
4. 每列：狀態標籤（排隊／等待中／執行中／完成／失敗／已取消／略過）、結論與信心、成本、耗時；完成列可點「查看報告」到 History 該 session（沿用現有導頁方式）；失敗列顯示 `describeEngineError` 的說明。
5. 結束摘要：沿用現有偏多／偏空／需複核／中性／失敗／總成本；終止原因（成本上限、OAuth 過期、伺服器重啟、使用者停止）用一句話講清楚。
6. 提示文字：「批量在伺服器端執行，可以關閉此頁，回來會看到進度。」
7. 已結束的批量可按「清除」隱藏（只在本機 localStorage 記住已隱藏的 batch_id，try/catch 包住；不刪伺服器資料）。
8. 手機寬度可用（Web 版會在手機上看）。

## 測試與驗收證據

D52（engine，`engine/tests/test_batches.py`）至少：

- 三檔依序全部完成，**全程沒有任何 WebSocket 訂閱者**（直接重現原 bug 的情境）。
- 單檔失敗（例如 ticker_unsupported）後繼續下一檔。
- 取消：目前那檔取消、剩餘 skipped、冪等。
- CostGuard 擋下：停止並標記原因，不 override。
- 等 slot：另一個 run 佔著 slot 時進入 waiting_slot，釋放後繼續；不會撞 run_busy 被誤判失敗。
- 冪等建立與 409 batch_active；owner 隔離（他人 404）。
- 重啟 recover：進行中批量 → interrupted，未開始項目 skipped。
- 秘密：DB 內容與所有 GET 回應不含 api_key/token/webhook URL（以假值字串搜尋）。
- 舊 schema 資料庫升級。
- 全部既有 engine 測試仍通過。

D53（vitest）至少：

- 掛載時從伺服器還原進度（模擬「關閉後重開」）。
- 輪詢更新列狀態與進度條；結束後停止輪詢。
- 卸載不呼叫 cancel；停止按鈕只呼叫一次 cancel。
- 409 時顯示既有批量。
- 失敗列不顯示成「完成」。
- `tsc`、全部既有 vitest 通過（Node 26 需 `--no-experimental-webstorage`，見 D47 紀錄）。

整合（主控）：stub／假 provider 本機實測三檔批量，中途關閉頁面再開啟，確認持續執行並顯示正確進度；附截圖或 log。

## 不在範圍

不 merge、不部署正式環境（G4，需使用者確認）；不改單次 Analyze 的執行路徑；不自動 override CostGuard；不在引擎重啟後自動續跑。
