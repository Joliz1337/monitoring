// Общие помощники карты клиентов Remnawave: сравнение «к вчера», страны, время, провайдеры.

export type CompareLevel = 'normal' | 'warn' | 'drop'

export const COMPARE_COLORS: Record<CompareLevel, string> = {
  normal: '#22d3ee',
  warn: '#f59e0b',
  drop: '#ef4444',
}

// Меньше пяти клиентов вчера — сравнение шумит: один ушедший клиент даёт −50 %
export const COMPARE_MIN_PREV = 5
export const COMPARE_WARN_RATIO = 0.7
export const COMPARE_DROP_RATIO = 0.4

const regionNames = new Map<string, Intl.DisplayNames | null>()

export function countryName(code: string | null | undefined, lang: string): string {
  if (!code) return ''
  if (!regionNames.has(lang)) {
    try {
      regionNames.set(lang, new Intl.DisplayNames([lang], { type: 'region' }))
    } catch {
      regionNames.set(lang, null)
    }
  }
  try {
    return regionNames.get(lang)?.of(code.toUpperCase()) ?? code
  } catch {
    return code
  }
}

export function formatAgo(iso: string | null | undefined, lang: string): string {
  if (!iso) return '—'
  const seconds = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000))
  const rtf = new Intl.RelativeTimeFormat(lang, { numeric: 'always', style: 'short' })
  if (seconds < 60) return rtf.format(-seconds, 'second')
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return rtf.format(-minutes, 'minute')
  const hours = Math.round(minutes / 60)
  if (hours < 48) return rtf.format(-hours, 'hour')
  return rtf.format(-Math.round(hours / 24), 'day')
}

export interface ProviderBrand {
  label: string
  mark: string
  bg: string
  fg: string
}

interface KnownProvider {
  pattern: RegExp
  label: { ru: string; en: string }
  mark: { ru: string; en: string }
  bg: string
  fg: string
}

// Сопоставление по имени организации ASN, а не по номерам: у крупных операторов
// десятки ASN, и имя организации в базе DB-IP у них устойчивее номеров
const KNOWN_PROVIDERS: KnownProvider[] = [
  { pattern: /vimpelcom|beeline/i, label: { ru: 'Билайн', en: 'Beeline' }, mark: { ru: 'Б', en: 'B' }, bg: '#ffc800', fg: '#111111' },
  { pattern: /mobile telesystems|\bmts\b/i, label: { ru: 'МТС', en: 'MTS' }, mark: { ru: 'МТС', en: 'MTS' }, bg: '#e30611', fg: '#ffffff' },
  { pattern: /megafon/i, label: { ru: 'МегаФон', en: 'MegaFon' }, mark: { ru: 'М', en: 'M' }, bg: '#00b956', fg: '#ffffff' },
  { pattern: /rostelecom/i, label: { ru: 'Ростелеком', en: 'Rostelecom' }, mark: { ru: 'Р', en: 'R' }, bg: '#7700ff', fg: '#ffffff' },
  { pattern: /t2 mobile|tele2/i, label: { ru: 'T2', en: 'T2' }, mark: { ru: 't2', en: 't2' }, bg: '#1f2229', fg: '#ffffff' },
  { pattern: /scartel|yota/i, label: { ru: 'Yota', en: 'Yota' }, mark: { ru: 'Y', en: 'Y' }, bg: '#00aeef', fg: '#ffffff' },
  { pattern: /er-telecom/i, label: { ru: 'Дом.ру', en: 'Dom.ru' }, mark: { ru: 'Д', en: 'D' }, bg: '#e1251b', fg: '#ffffff' },
]

const LEGAL_WORDS = new Set([
  'llc', 'ltd', 'limited', 'pjsc', 'jsc', 'ojsc', 'cjsc', 'ooo', 'oao', 'zao', 'ao', 'inc', 'gmbh', 'co', 'corp',
  'company', 'the', 'of', 'and', 'sa', 'srl', 'bv', 'plc', 'llp',
])

function initials(org: string): string {
  const words = org
    .split(/[^\p{L}\p{N}]+/u)
    .filter(w => w && !LEGAL_WORDS.has(w.toLowerCase()))
  if (words.length === 0) return 'AS'
  return words.slice(0, 2).map(w => w[0].toUpperCase()).join('')
}

export function providerBrand(asOrg: string | null, asn: number | null, lang: string): ProviderBrand | null {
  if (!asOrg && asn === null) return null
  const isRu = lang.startsWith('ru')
  const known = asOrg ? KNOWN_PROVIDERS.find(p => p.pattern.test(asOrg)) : undefined
  if (known) {
    return {
      label: isRu ? known.label.ru : known.label.en,
      mark: isRu ? known.mark.ru : known.mark.en,
      bg: known.bg,
      fg: known.fg,
    }
  }
  const hue = ((asn ?? 0) * 47) % 360
  return {
    label: asOrg ?? `AS${asn}`,
    mark: asOrg ? initials(asOrg) : 'AS',
    bg: `hsl(${hue} 45% 32%)`,
    fg: '#ffffff',
  }
}
