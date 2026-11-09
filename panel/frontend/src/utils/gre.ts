import type { GreTunnelInput } from '../api/client'

const IPV4 = /^(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(\.(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}$/

export function isIpv4(value: string): boolean {
  return IPV4.test(value.trim())
}

function toInt(ip: string): number {
  return ip.split('.').reduce((acc, octet) => acc * 256 + Number(octet), 0)
}

function toIp(value: number): string {
  return [24, 16, 8, 0].map(shift => Math.floor(value / 2 ** shift) % 256).join('.')
}

/** Второй узел сети /30: PE для CE. Обычно CE − 1, но у CE на первом узле — CE + 1. */
export function ptpPeer(ce: string): string | null {
  const address = ce.trim().split('/')[0]
  if (!isIpv4(address)) return null
  const value = toInt(address)
  const offset = value % 4
  if (offset === 1) return toIp(value + 1)
  if (offset === 2) return toIp(value - 1)
  return null
}

export function isPtpPair(pe: string, ce: string): boolean {
  return ptpPeer(ce) === pe.trim()
}

const LABELLED: Record<'pe' | 'ce' | 'local' | 'remote', RegExp> = {
  pe: /\bPE\b\s*:?\s*(\d{1,3}(?:\.\d{1,3}){3})/i,
  ce: /\bCE\b\s*:?\s*(\d{1,3}(?:\.\d{1,3}){3})/i,
  local: /GRE\s*SRC\s*:?\s*(\d{1,3}(?:\.\d{1,3}){3})/i,
  remote: /GRE\s*DST\s*:?\s*(\d{1,3}(?:\.\d{1,3}){3})/i,
}
const CIDR = /(\d{1,3}(?:\.\d{1,3}){3})\/(\d{1,2})/g

/**
 * Поля туннеля из текста, скопированного со страницы «Параметры подключения»
 * DDoS-Guard: адрес идёт следом за подписью (PE, CE, GRE SRC, GRE DST). Сеть
 * проверок подписи не имеет — это единственная сеть шире /30 в блоке маршрутов.
 */
export function parseDdgConnection(text: string): Partial<GreTunnelInput> {
  const result: Partial<GreTunnelInput> = {}
  for (const [field, pattern] of Object.entries(LABELLED) as [keyof typeof LABELLED, RegExp][]) {
    const match = text.match(pattern)
    if (match && isIpv4(match[1])) result[field] = match[1]
  }
  for (const match of text.matchAll(CIDR)) {
    const prefix = Number(match[2])
    if (prefix >= 8 && prefix < 30 && isIpv4(match[1])) result.health_net = `${match[1]}/${prefix}`
  }
  return result
}

/** Первая ошибка ответа API: строка detail или первое сообщение валидации pydantic. */
export function apiErrorMessage(err: unknown, fallback: string): string {
  const e = err as { response?: { data?: { detail?: unknown } }; message?: string }
  const detail = e?.response?.data?.detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail) && detail.length > 0) {
    const msg = (detail[0] as { msg?: unknown })?.msg
    if (typeof msg === 'string') return msg.replace(/^Value error, /, '')
  }
  return e?.message || fallback
}
