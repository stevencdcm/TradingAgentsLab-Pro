import type { dialogs } from '../en/dialogs';

export const dialogsZhTW: Record<keyof typeof dialogs, string> = {
  'providerCallout.title': '連接 AI 模型以執行真實分析',
  'providerCallout.text':
    '目前尚未設定任何 LLM 供應商，所以「盡職調查」只會執行內建範例，而非真正的多代理分析。新增供應商即可解鎖完整功能：貼上 OpenAI、Anthropic、OpenRouter 或 Google Gemini 的 API 金鑰即可。',
  'providerCallout.cta': '設定供應商',
  'costGuard.title.rate': '已達每日分析次數上限',
  'costGuard.title.budget': '已達預算上限',
  'costGuard.dim.daily': '每日',
  'costGuard.dim.weekly': '每週',
  'costGuard.dim.monthly': '每月',
  'costGuard.dim.sessions': '分析次數',
  'costGuard.cancel': '取消',
  'costGuard.override': '覆寫並繼續',
  'costGuard.overrideWait': '覆寫（{seconds}）',
  'costGuard.body.rate': '你今天已使用 {used} / {cap} 次分析。再執行這一次就會超過上限。',
  'costGuard.body.budget':
    '你已花費 {current}，{dimension}上限為 {cap}。這次分析最多可能花費 {estimate}，將超過上限。',
  'costGuard.dimWord.daily': '每日',
  'costGuard.dimWord.weekly': '每週',
  'costGuard.dimWord.monthly': '每月',
  'costGuard.hint': '覆寫僅適用於這一次分析。若要永久調整上限，請至<b>設定 → 成本守門</b>。',
  'consent.title': '開始之前',
  'consent.p1':
    '<b>Trading Agents Lab 是教育研究工具，並非財務建議。</b>它以多代理 LLM 分析呈現同一檔股票的多種觀點。分析可能有誤或不完整，你做出的任何決定都由你自行負責。',
  'consent.p2': '本應用程式僅供個人與教育用途。它是免費的開放原始碼軟體（AGPL-3.0），不附帶任何形式的保證。',
  'consent.p3':
    '<b>零資料蒐集。</b>與你有關的資料不會離開你的電腦。應用程式主動發出的連線，只有可選的更新檢查，以及你以自己的金鑰設定的資料與 LLM 供應商。',
  'consent.listSep': '、',
  'consent.listEnd': '。',
  'consent.fullTerms': '完整條款：',
  'consent.terms': '服務條款',
  'consent.privacy': '隱私權政策',
  'consent.disclaimer': '免責聲明',
  'consent.englishNote': '上列法律文件以英文發布，以英文原文為準。',
  'consent.decline': '拒絕並結束',
  'consent.agree': '我同意',
  'errorBoundary.badge': '發生錯誤',
  'errorBoundary.title': '此畫面發生未預期的錯誤',
  'errorBoundary.text':
    '你的資料與設定都安全無虞。請重新載入以回到可正常運作的畫面。若在同一頁面持續發生，請記下當時的操作，以便重現問題。',
  'errorBoundary.reload': '重新載入',
};
