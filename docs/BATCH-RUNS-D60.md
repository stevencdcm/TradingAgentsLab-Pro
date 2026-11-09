# D60 主控裁定：D54 審查後的修正規則（HER-30）

作者：claude-code（Opus 5.5），session 9c20ad14-a9f1-4819-83c8-af84e63c4608，2026-11-09。
依據：D54（Astra）程式審查，結論「需修正後再開 Draft PR」。實作依據順序：本文件 > `BATCH-RUNS-D58.md` > `BATCH-RUNS-D56.md`。

| 階段 | 內容 | 執行者 |
|---|---|---|
| D60 | 本文件 | 主控 |
| D61 | 後端修正（D54 第 1–7、10 項＋A4 其他） | sol |
| D62 | 前端修正（D54 第 8、9 項） | Sonnet |
| D63 | 複審（唯讀） | Astra |

## R1 短秘密（D54-1，修正 D58 A4）

- 長度 ≥ 8 的秘密值：在所有字串值**與 dict 鍵**中做子字串替換（不看詞邊界），長字串優先。
- 長度 < 8 的值：明文規定的例外，以完整詞邊界清洗。理由：真正的雲端憑證不會短於 8 字元；本機伺服器常用 `x`、`none` 這類假 key，若做子字串替換會破壞正常報告內容。
- 批量：建立時拒絕 < 8 的明文秘密（既有），**參照解析後的實際值也要檢查**；不合格 → 該項 `failed`/`invalid_credential`，批量停止 `stopped`/`invalid_credential`（新增 terminal_code，前端只需顯示通用訊息）。
- 單檔：不拒絕（行為不變），套用上述清洗規則。
- 測試：≥ 8 的值嵌在其他字裡、出現在 dict 鍵、經參照解析的值；< 8 的例外行為。

## R2 local URL 帳密（D54-2）

- `base_url` 用 `urlsplit` 拆出 username、password，各自原值與 percent-decoded 值都加入秘密集合；完整 URL 仍加入。
- `scrub_urls_and_tokens` **只套用在錯誤文字與 log**（`error` 欄位、`run.error` 訊息、stderr／traceback、batch／item error），不套用在報告內容（新聞來源連結等需保留）。報告內容靠 R1 的逐值替換保護。
- 測試：provider 錯誤回顯單獨 password、userinfo、完整 URL、percent-encoded 形式。

## R3 webhook 指紋（D54-5，依 D58 A5 字面）

- webhook 的 URL 與所有 token／secret 類欄位視為秘密：指紋中**整值**替換為 `"<secret>"`（是參照字串則保留參照）。移除依路徑猜 token 的啟發式。
- id、kind、enabled、名稱等非秘密欄位仍納入指紋。
- 結果：只改 webhook URL ＝ 憑證輪替，同鍵回原批量；新增／刪除 webhook、改 kind／enabled → 409。
- Scrubber 與指紋共用同一份「秘密欄位分類」定義。

## R4 終態持久化驗證（D54-3，補 D58 A6）

- 項目完成判準改為：DB 中該 run 資料列 `state` 為終態、與記憶體 run 一致，且 `state == completed` 時 `session_id` 存在並與記憶體一致。任一不符或讀取失敗 → item `failed`/`session_persist_failed`，批量 `stopped`/`storage_unavailable`。
- 批量 run 的終態 metadata 寫入改為嚴格（失敗重試一次）；正常與 recover 使用同一判準函式。

## R5 executor 早期失敗（D54-4）

- `config`、`reservation_id` 等在 executor 的 try 之前初始化。
- 取得 slot 後的批量狀態寫入失敗 → `run.error` code `storage_unavailable` 並 `close(1008, "storage_unavailable")`；BatchManager 遇此 terminal_code 停批。
- 測試要跑真 executor：running 狀態更新連續失敗兩次後 DB 恢復，批量必須停止、不得啟動下一檔。

## R6 run 第一次落盤（D54-6）

- 批量 run：`start_or_attach` 的第一次 `_save_meta` 必須嚴格成功才建立 task；失敗 → 不建 run task，批量 `stopped`/`storage_unavailable`。
- 嚴格 metadata 寫入失敗時把 `engine_started` 改回 False 再結算。
- `recover` 另外對帳「有 run_id 但沒有 runs 資料列」的未結算預留：視為未計費並釋放（既有語意：run 未建立就不可能開始計費）。補這個故障序列的測試。

## R7 recover 補結果（D54-7）

- recover 把 item 補為 completed 時，從 session（或 run_events 的 `session.complete`）補上 stance、conviction、成本與耗時。

## R8 tickers 數量檢查（D54-10）

- 先正規化去重，再檢查 1–50；原始陣列另設上限 500 筆防止過大請求。

## R9 A4 其他

- log 中的 ticker 等任何字串也經 scrubber。
- 所有提早結束路徑（`_settle_if_unfinished`、`too_many_runs`、建立失敗）集中清理 scrubber、`batch_prepare` 與 frame 參考。
- 秘密掃描測試改為：JSON 回應與 DB JSON 欄位先 `json.loads` 再遞迴檢查字串；log 同時檢查原文與 JSON 轉義形式。

## R10 前端（D54-8、9）

- BatchRunner：回到前景時**一律**重抓最新批量（即使目前沒有或已是終態）；終態時平常不輪詢。測試：舊批量已完成或空白畫面 → 別處開新批量 → 回前景後顯示並鎖定勾選。
- Analyze 批量提示：顯示期間每 10 秒與回前景時重新查 `/batches?limit=1`，批量不再 active 就移除提示。測試：掛載時 active，之後變終態，提示消失。

## D64 補充裁定（依 D63 複審）

D63（Astra）：D54 各項已修，新增兩項 major。D64 由 sol 修正，D65 由 Astra 複審。

### R11 清洗不得影響控制流程與協定結構（D63-N1）

- `Run.emit`：內部狀態（`_track`、`completed_seen`、phase、成本、session 持久化時挑選 `session.complete`）一律以**清洗前**的原始事件計算；清洗後的副本只用於輸出與儲存（events／replay／WS／DB）。
- 清洗副本：`type`、`seq` 的值永不修改。dict 鍵只在「鍵本身含秘密值且不符合 `^[A-Za-z_][A-Za-z0-9_]*$`」時才替換（協定鍵都是識別字；真實憑證幾乎都含 `-`、`.`、`/` 等字元）。其他字串值照 R1 清洗。
- session 持久化使用的事件：完成判斷與欄位擷取用原始事件，寫入內容用清洗副本。
- 測試（單檔與批量）：秘密值剛好等於 `session.complete`、`decision` 等協定字樣時，run 仍正常 completed、session 正確寫入、輸出中秘密仍被遮蔽（若出現在自由文字中）。

### R12 已終態 run 的未結算預留也要對帳（D63-N2）

- `RunManager.recover` 枚舉**所有** `run_id` 非空且 `finalized=0` 的預留，不論 run 狀態：
  - 無 runs 資料列 → 釋放（既有 R6）。
  - run 為 queued／running → 既有 interrupted 流程。
  - run 已終態 → 依 DB 的 `engine_started`：False 釋放；True 依既有 `settle_run_end`（incomplete 規則）結算。已 completed 且有 session 的 run，依既有完成結算語意處理，不重複計費。
- 測試：計費前 strict 寫入失敗 → 兩次 settle 都遇 DB 例外 → 終態 metadata 在 DB 恢復後寫入成功 → 重啟 → 預留被正確結算且只結算一次。

## D66 補充裁定（依 D65 複審）

D65（Astra）：N1、N2 原反例已修；新增 S1、S2（major）、S3（minor）。D66 由 sol 修正，D67 由 Astra 複審。

### R13 對外狀態一律用清洗後副本（D65-S1）

- `Run` 內部控制只從原始事件取必要值（type、phase、數字型成本與 token、完成旗標）。存在 `self.cost_report`、`self.final_cost_report` 及任何會被 `Run.status()`、`/runs/*`、`/runs/active`、batch snapshot 讀出的成員，一律存 `scrubber.clean(...)` 後的副本。
- `_finish` 重建 partial report 後同樣清洗；`clear_sensitive` 只能在所有對外成員都已清洗之後執行。
- 測試：真流程中 `session.complete`（cost_report 內含假秘密）之後取消／例外 → `GET /runs/{id}`、`/runs/active` 的 recent、batch snapshot 都不含秘密。

### R14 recover 不得把未知或 ceiling 成本歸零（D65-S2，修正 R12）

- 已 completed 且有 session 的 run 的未結算預留，依 session 實際記帳狀況處理：
  - session 的 `estimated_cost_usd` 為已知數值（ledger 已涵蓋）→ 釋放預留（finalized、cost 0，由 session 計入）。
  - 免費路徑（OAuth／local）→ 釋放。
  - 成本未知或屬 ceiling／usage gap → 依既有 `hold_ceiling` 語意：能從持久化的 cost_report 還原量測上界就用上界；不能還原就**保留原預留估計**（settled_as ceiling），不得變 0。
- 測試四類：exact、ceiling／usage gap、免費、缺 session；斷言 recover 前後 `_compute_spend` 的數字正確，且第二次 recover 不重複也不漏計。

### R15 保留全域已解析值的完整值替換（D65-S3）

- run scrubber 對自由文字仍套用 `secret_refs._seen` 中的**完整值**替換（長度 ≥ 8 子字串、< 8 詞邊界，同 R1）；只停用「尾八字元」替換。協定欄位依 R11 不受影響。
- 測試：先前其他 run 解析過、不在本 run scrubber 的秘密出現在本 run 自由文字中 → 被遮蔽；`completed` 等一般字不被誤改。

## D68 補充裁定（依 D67 複審）

D67（Astra）：S1、S3 已修；S2 剩阻擋項 B1；F1 列為 PR 已知風險。D68 由 sol 修正 B1。

### R16 ceiling 上界只信最新完成報告（D67-B1）

- `_recovered_ceiling_bound`：session 中的 `session.complete.cost_report` 具權威性。只要它存在：完整 → 用它算上界；有任何 usage gap／缺量測／缺價格／格式不完整 → **立即回 None**（保留原預留估計），不得退到 runs.cost_report。
- 只有 session 沒有完成報告時，才考慮 runs.cost_report，且必須是終態（`run_state` 不是 running）並經驗證完整；否則回 None。
- 測試：「session 有 gap＋runs 有較舊 running 報告」與「只有 partial run 報告」兩個情境，以真 recover／finalize／`_compute_spend` 斷言保留原估計，第二次 recover 不變。

### F1（不阻擋，列入 PR 已知風險）

recover 的上界是逐模型加總；`full_debate` 的 ceiling 是以較貴模型乘全部 token。完整用量時逐模型加總仍是合理上界，但多模型時數字可能與執行期不同。後續可共用 ceiling 計算或持久化結算依據。
