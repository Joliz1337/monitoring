// Клиентский поиск по спискам блокировок: префикс строки, точный IP или CIDR.
// Вся арифметика IPv4 — через умножение и 2 ** n: битовые сдвиги в JS знаковые
// 32-битные, адреса >= 128.0.0.0 ушли бы в отрицательные числа.

interface Ipv4Range {
  start: number
  end: number
}

function parseIpv4(s: string): number | null {
  const parts = s.split('.')
  if (parts.length !== 4) return null
  let value = 0
  for (const part of parts) {
    if (!/^\d{1,3}$/.test(part)) return null
    const octet = Number(part)
    if (octet > 255) return null
    value = value * 256 + octet
  }
  return value
}

export function parseIpv4Cidr(s: string): Ipv4Range | null {
  const [ipPart, prefixPart, rest] = s.split('/')
  if (rest !== undefined) return null
  const ip = parseIpv4(ipPart)
  if (ip === null) return null

  let prefix = 32
  if (prefixPart !== undefined) {
    if (!/^\d{1,2}$/.test(prefixPart)) return null
    prefix = Number(prefixPart)
    if (prefix > 32) return null
  }

  const size = 2 ** (32 - prefix)
  const start = Math.floor(ip / size) * size
  return { start, end: start + size - 1 }
}

function rangesOverlap(a: Ipv4Range, b: Ipv4Range): boolean {
  return a.start <= b.end && b.start <= a.end
}

export function matchesIpQuery(
  rule: { ip_cidr: string; comment?: string | null },
  query: string
): boolean {
  const q = query.trim().toLowerCase()
  if (!q) return true

  const substringMatch =
    rule.ip_cidr.toLowerCase().includes(q) ||
    (rule.comment ? rule.comment.toLowerCase().includes(q) : false)

  const queryRange = parseIpv4Cidr(q)
  if (queryRange === null) return substringMatch

  // Запрос-CIDR ищет по пересечению диапазонов (IPv6-правила — только substring);
  // запрос-одиночный IP дополнительно находит накрывающую его подсеть.
  const ruleRange = parseIpv4Cidr(rule.ip_cidr)
  if (q.includes('/')) {
    return ruleRange !== null ? rangesOverlap(ruleRange, queryRange) : substringMatch
  }
  return substringMatch || (ruleRange !== null && rangesOverlap(ruleRange, queryRange))
}
