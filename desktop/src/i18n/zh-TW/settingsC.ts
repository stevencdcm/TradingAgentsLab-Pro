import type { settingsC } from '../en/settingsC';

export const settingsCZhTW: Record<keyof typeof settingsC, string> = {
  'tg.err.token': '必須填寫機器人權杖。請貼上 BotFather 給你的權杖。',
  'tg.err.allowlist':
    '允許清單是空的。請至少加入一個 Telegram 數字使用者 ID，每行一個。對你的機器人傳送 /start 即可查到自己的 ID。',
  'tg.err.cap': '每日上限必須是不小於 0 的美元金額。',
  'tg.err.noCreds': '尚未設定 {label} 憑證。{hint}',
  'tg.hint.openai': '請到「LLM 供應商」分頁用 OpenAI OAuth 登入，或在 OpenAI API 金鑰備援列貼上 API 金鑰。',
  'tg.hint.other': '請到「LLM 供應商」分頁設定，或在上方改選其他供應商。',
  'tg.title': '<b>Telegram 機器人（雙向）</b>',
  'tg.intro':
    '用手機傳一個股票代碼（例如 <code>NVDA</code>）給機器人，就能取得一次盡職調查的結果。機器人以對外輪詢方式連線 Telegram，因此你的電腦不會對網際網路開放任何連接埠。必須設定允許清單；機器人會默默忽略其他人傳來的訊息。若機器人權杖外洩，每個聊天的每日花費上限可防止 token 被盜用耗盡。',
  'tg.edu': '<i>僅供教育與研究用途。機器人回傳的是與桌面應用程式相同的委員會評估，並附上相同的免責聲明。</i>',
  'tg.pending.title.one': '待核准',
  'tg.pending.title.other': '待核准',
  'tg.pending.hint':
    '有人對機器人傳送了 <code>/start</code>。核准後會把對方加入允許清單，讓對方觸發盡職調查，機器人也會私訊通知對方；拒絕則會默默丟棄該請求。項目會在 30 分鐘後自動過期。',
  'tg.pending.noName': '（無名稱）',
  'tg.pending.chat': '聊天 {id}',
  'tg.approve': '核准',
  'tg.deny': '拒絕',
  'tg.token': '機器人權杖（Bot Token）',
  'tg.tokenStored': '（已儲存，留空表示保留）',
  'tg.tokenPlaceholder': '貼上 BotFather 給你的權杖',
  'tg.provider': '機器人觸發辯論所用的 LLM 供應商',
  'tg.providerHint.pre':
    '使用「LLM 供應商」分頁中為該供應商儲存的憑證。OpenAI 在已連線時優先使用 OAuth（Codex）；否則使用 OpenAI 備援列中的 API 金鑰。預設模型：',
  'tg.providerHint.openai': '{apiModel}（API 金鑰）或 {oauthModel}（OAuth）',
  'tg.providerHint.post': '。機器人目前尚不支援本機執行環境。',
  'tg.allowlist': '允許清單（數字聊天 ID，每行一個）',
  'tg.cap': '每個聊天的每日上限（美元）',
  'tg.stop': '停止機器人',
  'tg.stopping': '停止中…',
  'tg.polling': '輪詢中',
  'tg.start': '儲存並啟動',
  'tg.starting': '啟動中…',
  'tg.stopped': '已停止',
  'tg.lastError': '上次錯誤：{error}',
  'tg.spend': '<b>今日花費（UTC）：</b>',
  'tg.spendEntry': '聊天 {id}：{cost}',
};
