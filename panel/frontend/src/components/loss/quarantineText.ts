import type { QuarantineBlockedReason, QuarantineItem } from '../../api/client'
import i18n from '../../i18n'

type Translate = (key: string, options?: Record<string, unknown>) => string

type ReasonKind = QuarantineItem['reason']['kind']

const SECONDS_IN_MINUTE = 60
const MINUTES_IN_HOUR = 60
const HOURS_IN_DAY = 24
// Адрес бывает отключён на сотнях релеев — поимённо показываются первые
const RELAYS_LISTED = 12

export function formatSpan(seconds: number, t: Translate): string {
  const totalMinutes = Math.max(1, Math.round(seconds / SECONDS_IN_MINUTE))
  if (totalMinutes < MINUTES_IN_HOUR) return t('loss.span_minutes', { minutes: totalMinutes })
  const totalHours = Math.floor(totalMinutes / MINUTES_IN_HOUR)
  const minutes = totalMinutes % MINUTES_IN_HOUR
  if (totalHours < HOURS_IN_DAY) {
    return minutes
      ? t('loss.span_hours_minutes', { hours: totalHours, minutes })
      : t('loss.span_hours', { hours: totalHours })
  }
  const days = Math.floor(totalHours / HOURS_IN_DAY)
  const hours = totalHours % HOURS_IN_DAY
  return hours ? t('loss.span_days_hours', { days, hours }) : t('loss.span_days', { days })
}

// Переведённых соединений бывают тысячи — с разделителем разрядов языка интерфейса
export function formatCount(value: number): string {
  return value.toLocaleString(i18n.language)
}

function earliest(times: (number | null)[]): number | null {
  const known = times.filter((time): time is number => time !== null)
  return known.length > 0 ? Math.min(...known) : null
}

function worst(relays: QuarantineItem[], pick: (relay: QuarantineItem) => number): number {
  return Math.max(...relays.map(pick))
}

export function refusalText(reason: QuarantineBlockedReason | null, t: Translate): string {
  return t(`loss.quarantine_blocked_${reason ?? 'error'}`)
}

export function relayList(names: string[], t: Translate): string {
  const hidden = names.length - RELAYS_LISTED
  const listed = names.slice(0, RELAYS_LISTED).join(', ')
  return hidden > 0 ? `${listed}, ${t('loss.quarantine_more_relays', { count: hidden })}` : listed
}

function reasonText(relays: QuarantineItem[], sustainedSeconds: number | null, t: Translate): string {
  const { kind } = relays[0].reason
  if (kind === 'stalled') return t('loss.quarantine_reason_stalled')
  if (kind === 'no_reply') return t('loss.quarantine_reason_no_reply')
  if (kind === 'downs') return t('loss.quarantine_reason_downs', { count: worst(relays, relay => relay.reason.downs) })
  const loss = worst(relays, relay => relay.reason.loss_pct)
  return sustainedSeconds === null
    ? t('loss.quarantine_reason_loss_short', { loss })
    : t('loss.quarantine_reason_loss', { loss, duration: formatSpan(sustainedSeconds, t) })
}

// Причины адреса для краткой строки: каждая один раз, с худшими цифрами
export function reasonsBrief(items: QuarantineItem[], t: Translate): string {
  const byKind = new Map<ReasonKind, QuarantineItem[]>()
  for (const item of items) {
    const relays = byKind.get(item.reason.kind) ?? []
    byKind.set(item.reason.kind, relays)
    relays.push(item)
  }
  return [...byKind].map(([kind, relays]) => {
    if (kind === 'stalled') return t('loss.quarantine_reason_stalled_short')
    if (kind === 'no_reply') return t('loss.quarantine_reason_no_reply_short')
    if (kind === 'downs') return t('loss.quarantine_reason_downs', { count: worst(relays, relay => relay.reason.downs) })
    return t('loss.quarantine_reason_loss_short', { loss: worst(relays, relay => relay.reason.loss_pct) })
  }).join(' · ')
}

// Отключённый сервер трафика не получает: заговорил ли молчавший, покажет только возврат в работу
function returnText(relays: QuarantineItem[], now: number, t: Translate): string {
  const term = earliest(relays.map(relay => relay.until))
  const span = term !== null && term > now ? formatSpan(term - now, t) : null
  const { kind } = relays[0].reason
  // Проверка «как клиент» идёт и к отключённому серверу: вернётся он по факту ответа
  if (kind === 'no_reply') {
    return span ? t('loss.quarantine_returns_after_answers', { span }) : t('loss.quarantine_returns_when_answers')
  }
  if (kind === 'stalled') {
    return span ? t('loss.quarantine_returns_after_stalled', { span }) : t('loss.quarantine_returns_soon_stalled')
  }
  return span ? t('loss.quarantine_returns_after', { span }) : t('loss.quarantine_returns_when_calm')
}

// Когда адрес вернётся — коротко: самый ранний срок среди его релеев
export function returnBrief(disabled: QuarantineItem[], now: number, t: Translate): string {
  const term = earliest(disabled.map(relay => relay.until))
  if (term !== null && term > now) return t('loss.quarantine_back_in', { span: formatSpan(term - now, t) })
  const kinds = new Set(disabled.map(relay => relay.reason.kind))
  if (kinds.has('no_reply')) return t('loss.quarantine_back_when_answers')
  if (kinds.has('stalled')) return t('loss.quarantine_back_after_calm')
  return t('loss.quarantine_back_when_calm')
}

// У строки из нескольких релеев — худшая причина, самое раннее отключение и самый ранний возврат
export function situationText(relays: QuarantineItem[], sustainedSeconds: number | null, now: number, t: Translate): string {
  const [{ state, blocked_reason: blockedReason, partial_reason: partialReason }] = relays
  const parts = [reasonText(relays, sustainedSeconds, t)]
  if (state === 'blocked') parts.push(refusalText(blockedReason, t))
  if (state === 'blocked' || state === 'pending') return parts.join(' · ')

  const since = earliest(relays.map(relay => relay.disabled_at))
  if (since !== null) parts.push(t('loss.quarantine_ago', { span: formatSpan(now - since, t) }))
  if (state === 'would_disable') return parts.join(' · ')

  parts.push(returnText(relays, now, t))
  const sessions = relays.reduce((sum, relay) => sum + relay.sessions, 0)
  if (sessions > 0) parts.push(t('loss.quarantine_sessions_moved', { sessions: formatCount(sessions) }))
  if (partialReason) parts.push(t('loss.quarantine_partial', { reason: refusalText(partialReason, t) }))
  return parts.join(' · ')
}
