import type { BackendServer } from '../api/client'

const SERVER_KEYWORD = 'server'
const SERVER_NAME_RE = /^[a-zA-Z0-9_.:-]+$/
const TARGET_RE = /^(\S+):(\d+)$/
const TARGET_LIST_SEPARATOR = /[\s,;]+/
const MAX_PORT = 65535

export interface ParsedServerLines {
  servers: BackendServer[]
  unrecognizedLines: number[]
}

// Синтаксис тот же, что пишет генератор конфига панели (_build_server_line), кроме cookie и resolvers —
// их генератор добавляет сам по настройкам правила и виду адреса
function formatServerLine(srv: BackendServer): string {
  const parts = [SERVER_KEYWORD, srv.name, `${srv.address}:${srv.port}`]
  const weight = srv.weight ?? 1
  if (weight !== 1) parts.push('weight', String(weight))
  if (srv.maxconn) parts.push('maxconn', String(srv.maxconn))
  if (srv.send_proxy_v2) parts.push('send-proxy-v2')
  else if (srv.send_proxy) parts.push('send-proxy')
  if (srv.check ?? true) {
    parts.push('check', 'inter', srv.inter ?? '5s', 'fall', String(srv.fall ?? 3), 'rise', String(srv.rise ?? 2))
  }
  if (srv.backup) parts.push('backup')
  if (srv.slowstart) parts.push('slowstart', srv.slowstart)
  if (srv.disabled) parts.push('disabled')
  return parts.join(' ')
}

export function formatServerLines(servers: BackendServer[]): string {
  return servers.map(formatServerLine).join('\n')
}

interface Target {
  address: string
  port: number
}

function parseTarget(token: string): Target | null {
  const match = TARGET_RE.exec(token)
  if (!match) return null
  const port = Number(match[2])
  return port >= 1 && port <= MAX_PORT ? { address: match[1], port } : null
}

function parseInteger(value: string | undefined): number | undefined {
  return value !== undefined && /^\d+$/.test(value) ? Number(value) : undefined
}

// Строка `server` читается так, как её понял бы HAProxy: чего в строке нет, то выключено
function parseServerKeywordLine(tokens: string[]): BackendServer | null {
  const [, name, targetToken, ...options] = tokens
  const target = targetToken ? parseTarget(targetToken) : null
  if (!name || !SERVER_NAME_RE.test(name) || !target) return null

  const valueOf = (key: string): string | undefined => {
    const index = options.indexOf(key)
    return index >= 0 ? options[index + 1] : undefined
  }
  const integerOption = (key: string): number | undefined | null => {
    const raw = valueOf(key)
    if (raw === undefined) return undefined
    return parseInteger(raw) ?? null
  }

  const weight = integerOption('weight')
  const maxconn = integerOption('maxconn')
  const fall = integerOption('fall')
  const rise = integerOption('rise')
  if (weight === null || maxconn === null || fall === null || rise === null) return null

  const sendProxyV2 = options.includes('send-proxy-v2')
  return {
    name, ...target,
    weight: weight ?? 1,
    maxconn,
    check: options.includes('check'),
    inter: valueOf('inter') ?? '5s',
    fall: fall ?? 3,
    rise: rise ?? 2,
    send_proxy: !sendProxyV2 && options.includes('send-proxy'),
    send_proxy_v2: sendProxyV2,
    backup: options.includes('backup'),
    slowstart: valueOf('slowstart'),
    disabled: options.includes('disabled'),
  }
}

/**
 * Разбирает вставленный текст: строки `server имя адрес:порт опции` и списки голых `адрес:порт`
 * (через пробел, запятую или с новой строки). Голый адрес получает настройки из defaults и пустое имя —
 * имя назначает вызывающий. Комментарии `#` и пустые строки пропускаются.
 */
export function parseServerLines(text: string, defaults: BackendServer): ParsedServerLines {
  const servers: BackendServer[] = []
  const unrecognizedLines: number[] = []

  text.split('\n').forEach((rawLine, index) => {
    const line = rawLine.replace(/#.*/, '').trim()
    if (!line) return

    const tokens = line.split(/\s+/)
    if (tokens[0] === SERVER_KEYWORD) {
      const server = parseServerKeywordLine(tokens)
      if (server) servers.push(server)
      else unrecognizedLines.push(index + 1)
      return
    }

    const candidates = line.split(TARGET_LIST_SEPARATOR).filter(Boolean)
    const targets = candidates.map(parseTarget).filter((target): target is Target => target !== null)
    if (targets.length !== candidates.length) {
      unrecognizedLines.push(index + 1)
      return
    }
    for (const target of targets) servers.push({ ...defaults, name: '', ...target })
  })

  return { servers, unrecognizedLines }
}
