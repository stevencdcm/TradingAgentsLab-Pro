import type { evidence } from '../en/evidence';

export const evidenceZhTW: Record<keyof typeof evidence, string> = {
  'evidence.title': '證據包',
  'evidence.snapshot': '快照 {id} · 截至 {asOf}',
  'evidence.unknown': '未知：',
  'dexter.title': 'Dexter（財報深研）',
  'dexter.status.succeeded': '已完成',
  'dexter.status.partial': '部分完成——不採用',
  'dexter.status.failed': '未完成',
  'dexter.status.timeout': '逾時',
  'dexter.status.canceled': '已取消',
  'dexter.status.unsupported': '未執行（不支援）',
  'dexter.tokens': '{n} tokens',
  'dexter.unverified': '未通過驗證',
  'dexter.pitUnknown': '公布時點未知',
  'dexter.noFindings': '沒有產生任何發現。',
};
