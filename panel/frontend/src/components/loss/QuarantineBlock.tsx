import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import { Loader2, PauseCircle, ShieldOff } from 'lucide-react'
import { lossApi, type QuarantineItem, type QuarantineOverview } from '../../api/client'

type Translate = (key: string, options?: Record<string, unknown>) => string

export interface BackendAddress {
  ip: string
  port: number
}

interface AddressGroup extends BackendAddress {
  target: string
  owner: string | null
  items: QuarantineItem[]
}

interface Props {
  view: QuarantineOverview | null
  // Удержание из настроек — для текста причины; null, пока настройки не загружены
  sustainedSeconds: number | null
  canExempt: boolean
  onReleased: () => void
  onExempt: (address: BackendAddress) => void
  onEdit: (address: BackendAddress) => void
}

const SECONDS_IN_MINUTE = 60
const MINUTES_IN_HOUR = 60
const HOURS_IN_DAY = 24

const actionButtonCls = 'px-2.5 py-1 rounded-lg text-xs transition-colors'

function formatSpan(seconds: number, t: Translate): string {
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

function groupByAddress(items: QuarantineItem[]): AddressGroup[] {
  const groups = new Map<string, AddressGroup>()
  for (const item of items) {
    const group = groups.get(item.target)
    if (group) group.items.push(item)
    else groups.set(item.target, { target: item.target, ip: item.ip, port: item.port, owner: item.owner, items: [item] })
  }
  return [...groups.values()]
}

// Релеи адреса с одним состоянием и одной причиной идут одной строкой
function groupBySituation(items: QuarantineItem[]): QuarantineItem[][] {
  const situations = new Map<string, QuarantineItem[]>()
  for (const item of items) {
    const key = `${item.state}|${item.reason.kind}|${item.blocked_reason}`
    const situation = situations.get(key)
    if (situation) situation.push(item)
    else situations.set(key, [item])
  }
  return [...situations.values()]
}

function earliest(times: (number | null)[]): number | null {
  const known = times.filter((time): time is number => time !== null)
  return known.length > 0 ? Math.min(...known) : null
}

function reasonText(relays: QuarantineItem[], sustainedSeconds: number | null, t: Translate): string {
  if (relays[0].reason.kind === 'downs') {
    return t('loss.quarantine_reason_downs', { count: Math.max(...relays.map(relay => relay.reason.downs)) })
  }
  const loss = Math.max(...relays.map(relay => relay.reason.loss_pct))
  return sustainedSeconds === null
    ? t('loss.quarantine_reason_loss_short', { loss })
    : t('loss.quarantine_reason_loss', { loss, duration: formatSpan(sustainedSeconds, t) })
}

// У строки из нескольких релеев — худшая причина, самое раннее отключение и самый ранний возврат
function situationText(relays: QuarantineItem[], sustainedSeconds: number | null, now: number, t: Translate): string {
  const [{ state, blocked_reason: blockedReason }] = relays
  const parts = [reasonText(relays, sustainedSeconds, t)]
  const since = earliest(relays.map(relay => relay.disabled_at))

  if (state === 'pending') {
    parts.push(t('loss.quarantine_pending'))
  } else if (state === 'blocked') {
    parts.push(t('loss.quarantine_blocked', { reason: t(`loss.quarantine_blocked_${blockedReason ?? 'error'}`) }))
  } else if (state === 'would_disable') {
    if (since !== null) parts.push(t('loss.quarantine_would_disable_ago', { span: formatSpan(now - since, t) }))
  } else {
    if (since !== null) parts.push(t('loss.quarantine_disabled_ago', { span: formatSpan(now - since, t) }))
    const until = earliest(relays.map(relay => relay.until))
    parts.push(until !== null && until > now
      ? t('loss.quarantine_returns_after', { span: formatSpan(until - now, t) })
      : t('loss.quarantine_returns_when_calm'))
    const sessions = relays.reduce((sum, relay) => sum + relay.sessions, 0)
    if (sessions > 0) parts.push(t('loss.quarantine_sessions_moved', { count: sessions }))
  }
  return parts.join(' · ')
}

interface AddressRowProps extends Omit<Props, 'view'> {
  group: AddressGroup
  chronicStrikes: number | undefined
  now: number
}

function AddressRow({ group, chronicStrikes, now, sustainedSeconds, canExempt, onReleased, onExempt, onEdit }: AddressRowProps) {
  const { t } = useTranslation()
  const [releasing, setReleasing] = useState(false)
  const releasable = group.items.filter(item => item.state === 'disabled' || item.state === 'pending')

  const release = async () => {
    setReleasing(true)
    const outcomes = await Promise.allSettled(
      releasable.map(item => lossApi.releaseQuarantine(item.server_id, item.target)),
    )
    setReleasing(false)
    if (outcomes.some(outcome => outcome.status === 'rejected')) {
      toast.error(t('loss.quarantine_release_failed', { target: group.target }))
    } else {
      toast.success(t('loss.quarantine_released', { target: group.target }))
    }
    onReleased()
  }

  return (
    <div className="px-4 py-3 space-y-1.5">
      <div className="flex items-start justify-between gap-x-3 gap-y-2 flex-wrap">
        <div className="flex items-center gap-x-2 gap-y-1 flex-wrap min-w-0">
          <span className="font-mono text-sm text-dark-100 break-all">{group.target}</span>
          <span className="text-xs text-dark-500">
            {group.owner ? t('loss.quarantine_owner', { name: group.owner }) : t('loss.owner_unknown')}
          </span>
          {chronicStrikes !== undefined && (
            <span
              title={t('loss.quarantine_chronic_hint')}
              className="px-1.5 py-0.5 rounded text-2xs font-medium bg-danger/10 text-danger border border-danger/20"
            >
              {t('loss.quarantine_chronic', { count: chronicStrikes })}
            </span>
          )}
        </div>
        <div className="flex items-center flex-wrap -mx-2.5">
          {releasable.length > 0 && (
            <button
              onClick={release}
              disabled={releasing}
              title={t('loss.quarantine_release_hint')}
              className={`${actionButtonCls} inline-flex items-center gap-1.5 text-accent-400 hover:bg-accent-500/10 disabled:opacity-40`}
            >
              {releasing && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
              {t('loss.quarantine_release')}
            </button>
          )}
          {canExempt && (
            <button
              onClick={() => onExempt(group)}
              title={t('loss.quarantine_exempt_hint')}
              className={`${actionButtonCls} text-dark-400 hover:text-dark-200 hover:bg-dark-800/60`}
            >
              {t('loss.quarantine_exempt')}
            </button>
          )}
          {group.ip.includes('.') && (
            <button onClick={() => onEdit(group)} className={`${actionButtonCls} text-accent-400 hover:bg-accent-500/10`}>
              {t('loss.edit_open')}
            </button>
          )}
        </div>
      </div>
      {groupBySituation(group.items).map(relays => (
        <p
          key={relays[0].server_id}
          className={`text-xs ${relays[0].state === 'blocked' ? 'text-warning' : 'text-dark-400'}`}
        >
          <span className="text-dark-200">
            {t(relays.length === 1 ? 'loss.quarantine_on_relay' : 'loss.quarantine_on_relays', {
              names: relays.map(relay => relay.server_name).join(', '),
            })}
          </span>
          {' · '}
          {situationText(relays, sustainedSeconds, now, t)}
        </p>
      ))}
    </div>
  )
}

export default function QuarantineBlock({ view, ...rowProps }: Props) {
  const { t } = useTranslation()
  if (!view) return null

  const groups = groupByAddress(view.items)
  const nothingToShow = groups.length === 0 && view.paused_relays.length === 0
  if (view.mode === 'off' && nothingToShow) return null

  const observing = view.mode === 'observe'
  const chronicStrikes = new Map(view.chronic.map(address => [address.target, address.strikes]))
  const now = Date.now() / 1000

  return (
    <div className="bg-dark-900/50 rounded-xl border border-dark-800/50">
      <div className="flex items-center gap-2 px-4 py-3 border-b border-dark-800/50">
        <ShieldOff className="w-4 h-4 text-warning" />
        <h2 className="text-sm font-medium text-dark-200">
          {t(observing ? 'loss.quarantine_title_observe' : 'loss.quarantine_title')}
        </h2>
      </div>
      {nothingToShow ? (
        <p className="px-4 py-4 text-sm text-dark-500">
          {t(observing ? 'loss.quarantine_empty_observe' : 'loss.quarantine_empty')}
        </p>
      ) : (
        <div className="divide-y divide-dark-800/50">
          {groups.map(group => (
            <AddressRow
              key={group.target}
              group={group}
              chronicStrikes={chronicStrikes.get(group.target)}
              now={now}
              {...rowProps}
            />
          ))}
          {view.paused_relays.map(relay => (
            <div key={relay.server_id} className="flex items-center gap-2 px-4 py-2.5 text-xs text-warning">
              <PauseCircle className="w-3.5 h-3.5 shrink-0" />
              {t('loss.quarantine_paused', { name: relay.name, span: formatSpan(now - relay.since, t) })}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
