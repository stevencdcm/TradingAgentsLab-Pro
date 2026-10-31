"""Static metadata for Lab Pro's future data-provider settings page.

`verified_at` is documentation/sample verification, never a runtime success time.
Callers must obtain runtime health from actual FetchResult observations.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

_VERIFIED_AT = "2026-10-30"
_PRICING = "https://finmind.github.io/Pricing/"
_FUNDAMENTAL = "https://github.com/FinMind/FinMind-Doc/blob/master/docs/tutor/TaiwanMarket/Fundamental.md"
_DATASETS = "https://github.com/FinMind/FinMind-MCP/blob/master/knowledge/datasets.md"

_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "id": "finmind_free",
        "name": "FinMind Free（匿名）",
        "official_docs": [_PRICING, _FUNDAMENTAL, _DATASETS],
        "data_types": ["income_statement", "balance_sheet", "cash_flow_statement", "news_headlines"],
        "free_scope": "個股四碼；綜合損益表、資產負債表、現金流量表、相關新聞。新聞 API 每次只取單日；回傳標題、來源、URL，摘要不保證提供。",
        "token_requirement": "匿名查詢無需 token；註冊會員另有較高額度，但此模組不讀取 token。",
        "limits": "Free 匿名 300 次／小時；非商業用途；來源須標示 FinMind；不得再散布原始資料或建鏡像 API。",
        "update_cadence": "unknown",
        "license_notes": "僅用於符合 FinMind Free 條款的非商業研究；原始資料不得再散布。設定頁需明示來源 FinMind。",
        "verified_at": _VERIFIED_AT,
        "verification_scope": "官方文件與 2330/6488 少量匿名 API 探測；不代表完整歷史或研究圖已接通。",
        "implementation_status": "adapter_available_unwired",
    },
    {
        "id": "twse_material_disclosures",
        "name": "臺灣證券交易所每日重大訊息",
        "official_docs": ["https://openapi.twse.com.tw/", "https://openapi.twse.com.tw/v1/swagger.json"],
        "data_types": ["listed_material_disclosures"],
        "free_scope": "公開 OpenAPI 當前每日上市公司重大訊息集合；本模組按公司代號過濾，沒有歷史查詢保證。",
        "token_requirement": "本次匿名樣本無需 token。",
        "limits": "官方端點未在本次文件中確認固定額度；保守快取與限流，遵守交易所條款。",
        "update_cadence": "unknown",
        "license_notes": "呈現須標明 TWSE 原始來源；公告發言/事實日期不等於可證明的公開時間。",
        "verified_at": _VERIFIED_AT,
        "verification_scope": "OpenAPI schema 與當日匿名 GET；非歷史完整性驗證。",
        "implementation_status": "adapter_available_unwired",
    },
    {
        "id": "tpex_material_disclosures",
        "name": "櫃買中心每日重大訊息",
        "official_docs": ["https://www.tpex.org.tw/openapi/", "https://www.tpex.org.tw/openapi/swagger.json"],
        "data_types": ["otc_material_disclosures"],
        "free_scope": "公開 OpenAPI 當前每日上櫃公司重大訊息集合；本模組按公司代號過濾，沒有歷史查詢保證。",
        "token_requirement": "本次匿名樣本無需 token。",
        "limits": "官方端點未在本次文件中確認固定額度；保守快取與限流，遵守櫃買中心條款。",
        "update_cadence": "unknown",
        "license_notes": "呈現須標明 TPEx 原始來源；公告發言/事實日期不等於可證明的公開時間。",
        "verified_at": _VERIFIED_AT,
        "verification_scope": "OpenAPI schema 與當日匿名 GET；6488 當日無列，不代表歷史無公告。",
        "implementation_status": "adapter_available_unwired",
    },
    {
        "id": "mops_historical_disclosures",
        "name": "公開資訊觀測站歷史重大訊息",
        "official_docs": ["https://mops.twse.com.tw/"],
        "data_types": ["historical_material_disclosures"],
        "free_scope": "未找到可核實且穩定的匿名歷史 API 契約。",
        "token_requirement": "unknown",
        "limits": "unknown",
        "update_cadence": "unknown",
        "license_notes": "不以爬取或繞過封鎖替代 API 契約。",
        "verified_at": _VERIFIED_AT,
        "verification_scope": "僅確認此次未建立穩定歷史 API；未探測個別頁面。",
        "implementation_status": "unsupported",
    },
)


def get_source_catalog() -> list[dict[str, Any]]:
    """Return editable copies; UI must show status and limits without claiming live health."""
    return deepcopy(_CATALOG)
