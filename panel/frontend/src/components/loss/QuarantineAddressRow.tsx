import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import { AlertTriangle, ChevronDown, Repeat, Replace, RotateCcw, Shield } from 'lucide-react'
import type { QuarantineBlockedReason, QuarantineItem } from '../../api/client'
import RowMenu, { type MenuAction } from './RowMenu'
import { releaseItems } from './quarantineRelease'
import {
  groupBySituation,
  relayNames,
  releasable,
  type AddressGroup,
  type BackendAddress,
  type ExemptAddress,
} from './quarantineGroups'
import { formatCount, reasonsBrief, refusalText, relayList, returnBrief, situationText } from './quarantineText'
import { spreadLevel, type Spread, type SpreadLevel } from './quarantineSpread'

// Столько релеев в краткой строке названо поимённо, больше — числом
const RELAYS_INLINE = 2
const HALF_DOT = 'bg-[linear-gradient(90deg,theme(colors.warning)_50%,theme(colors.danger)_50%)]'
const pillCls = 'px-2 py-0.5 rounded-md border text-2xs font-medium'
// Красным — только бан везде: такой адрес пора менять, а не ждать его возврата
const SPREAD_CLS: Record<SpreadLevel, string> = {
  all: 'bg-danger/10 text-danger border-danger/20',
  most: 'bg-warning/10 text-warning border-warning/20',
  limited: 'bg-dark-800/60 text-dark-300 border-dark-700',
}

export interface AddressActions {
  // Удержание из настроек — для текста причины; null, пока настройки не загружены
  sustainedSeconds: number | null
  canExempt: boolean
  onReleased: () => void
  onExempt: (address: ExemptAddress) => void
  onEdit: (address: BackendAddress) => void
}

interface Props extends AddressActions {
  group: AddressGroup
  mergePorts: boolean
  now: number
  // null — судить не о чем: таблица потерь не загружена, адреса в ней нет или он везде работает
  spread: Spread | null
}

function SpreadMark({ spread }: { spread: Spread }) {
  const { t } = useTranslation()
  const level = spreadLevel(spread)
  return (
    <span title={t('loss.quarantine_spread_hint', { ...spread })} className={`${pillCls} whitespace-nowrap ${SPREAD_CLS[level]}`}>
      {level === 'all' && `${t('loss.quarantine_spread_all')} · `}
      {t('loss.quarantine_spread', { ...spread })}
    </span>
  )
}

function dotClass(items: QuarantineItem[]): string {
  const states = new Set(items.map(item => item.state))
  const off = states.has('disabled') || states.has('pending')
  if (off && states.has('blocked')) return HALF_DOT
  if (off) return 'bg-warning'
  if (states.has('blocked')) return 'bg-danger'
  if (states.has('would_disable')) return 'border border-warning'
  return 'bg-dark-600'
}

// Отказ без названной причины показывается как ошибка HAProxy — и идёт с ней одной строкой
function refusals(items: QuarantineItem[]): [QuarantineBlockedReason, QuarantineItem[]][] {
  const byReason = new Map<QuarantineBlockedReason, QuarantineItem[]>()
  for (const item of items) {
    if (item.state !== 'blocked') continue
    const reason = item.blocked_reason ?? 'error'
    const relays = byReason.get(reason) ?? []
    byReason.set(reason, relays)
    relays.push(item)
  }
  return [...byReason]
}

function partialReasons(items: QuarantineItem[]): QuarantineBlockedReason[] {
  const reasons = items.map(item => item.partial_reason).filter((reason): reason is QuarantineBlockedReason => reason !== null)
  return [...new Set(reasons)]
}

function When({ items, now }: { items: QuarantineItem[]; now: number }) {
  const { t } = useTranslation()
  const disabled = items.filter(item => item.state === 'disabled')
  if (disabled.length > 0) return <>{returnBrief(disabled, now, t)}</>
  if (items.some(item => item.state === 'pending')) return <>{t('loss.quarantine_pending')}</>
  if (items.some(item => item.state === 'would_disable')) {
    return <span className={`${pillCls} bg-warning/10 text-warning border-warning/20`}>{t('loss.quarantine_would_disable_short')}</span>
  }
  if (items.length > 0) {
    return <span className={`${pillCls} bg-danger/10 text-danger border-danger/20`}>{t('loss.quarantine_not_disabled')}</span>
  }
  return <span className="text-dark-500">{t('loss.quarantine_in_service')}</span>
}

function Facts({ group }: { group: AddressGroup }) {
  const { t } = useTranslation()
  if (group.items.length === 0) return <>{t('loss.quarantine_chronic_in_service', { count: group.chronicStrikes })}</>

  const working = group.items.filter(item => item.state !== 'blocked')
  // Адрес, который нигде не отключён, описывается по релеям, где отключить его не удалось
  const shown = working.length > 0 ? working : group.items
  const names = relayNames(shown)
  const sessions = working.reduce((sum, item) => sum + item.sessions, 0)
  return (
    <>
      {names.length <= RELAYS_INLINE ? names.join(', ') : (
        <span title={relayList(names, t)} className="underline decoration-dotted decoration-dark-600 underline-offset-4">
          {t('loss.quarantine_relays_count', { count: names.length })}
        </span>
      )}
      {' · '}
      {reasonsBrief(shown, t)}
      {sessions > 0 && ` · ${t('loss.quarantine_sessions_short', { sessions: formatCount(sessions) })}`}
    </>
  )
}

function Situation({ relays, sustainedSeconds, now }: { relays: QuarantineItem[]; sustainedSeconds: number | null; now: number }) {
  const { t } = useTranslation()
  const names = relayNames(relays)
  const [{ state }] = relays
  return (
    <div className="text-xs">
      <p className={state === 'blocked' ? 'text-danger' : 'text-dark-100'}>
        <span className="font-medium">
          {t(`loss.quarantine_state_${state}`, { where: t('loss.quarantine_relays_loc', { count: names.length }) })}
        </span>
        <span className="text-dark-300"> · {relayList(names, t)}</span>
      </p>
      <p className="text-dark-400">{situationText(relays, sustainedSeconds, now, t)}</p>
    </div>
  )
}

export default function QuarantineAddressRow({
  group, mergePorts, now, spread, sustainedSeconds, canExempt, onReleased, onExempt, onEdit,
}: Props) {
  const { t } = useTranslation()
  const [open, setOpen] = useState(false)
  const [releasing, setReleasing] = useState(false)
  const toRelease = releasable(group.items)
  const expandable = group.items.length > 0

  const release = async () => {
    setReleasing(true)
    const failed = await releaseItems(toRelease)
    setReleasing(false)
    if (failed === 0) toast.success(t('loss.quarantine_released', { target: group.key }))
    else toast.error(t('loss.quarantine_release_failed', { target: group.key }))
    onReleased()
  }

  const actions: MenuAction[] = []
  if (toRelease.length > 0) {
    actions.push({ label: t('loss.quarantine_release'), hint: t('loss.quarantine_release_hint'), icon: RotateCcw, onSelect: release })
  }
  if (canExempt) {
    actions.push({
      label: t('loss.quarantine_exempt'),
      hint: t('loss.quarantine_exempt_hint'),
      icon: Shield,
      onSelect: () => onExempt({ ip: group.ip, port: mergePorts ? null : group.ports[0] }),
    })
  }
  // Правка профилей — только IPv4: DNAT других адресов не держит.
  // Окно замены по умолчанию правит все порты IP, поэтому склеенной строке хватает любого порта
  if (group.ip.includes('.')) {
    actions.push({ label: t('loss.edit_open'), icon: Replace, onSelect: () => onEdit({ ip: group.ip, port: group.ports[0] }) })
  }

  return (
    <li className="border-t border-dark-800/50">
      <div className="flex items-start gap-1 pl-4 pr-2.5 py-2.5 hover:bg-dark-800/30 transition-colors">
        <button
          type="button"
          aria-expanded={expandable ? open : undefined}
          disabled={!expandable}
          onClick={() => setOpen(prev => !prev)}
          className="flex-1 min-w-0 flex items-start gap-2.5 text-left"
        >
          <span className={`mt-[7px] w-2 h-2 rounded-full shrink-0 ${dotClass(group.items)}`} />
          <span className="flex-1 min-w-0 space-y-0.5">
            <span className="flex items-start justify-between gap-2">
              <span className="min-w-0 flex items-center gap-x-2 gap-y-0.5 flex-wrap">
                <span className="font-mono text-sm text-dark-100 break-all">{group.key}</span>
                {mergePorts && (
                  <span className="font-mono text-xs text-dark-500">{group.ports.map(port => `:${port}`).join(', ')}</span>
                )}
                {spread && <SpreadMark spread={spread} />}
                {group.chronicStrikes !== undefined && (
                  <span
                    title={`${t('loss.quarantine_chronic', { count: group.chronicStrikes })}. ${t('loss.quarantine_chronic_hint')}`}
                    className="inline-flex items-center gap-1 text-2xs text-danger whitespace-nowrap"
                  >
                    <Repeat className="w-3 h-3" />
                    {t('loss.quarantine_chronic_short', { count: group.chronicStrikes })}
                  </span>
                )}
              </span>
              <span className="shrink-0 flex items-center gap-1 text-xs leading-5 text-dark-200 whitespace-nowrap">
                <When items={group.items} now={now} />
                {expandable && (
                  <ChevronDown className={`w-3.5 h-3.5 text-dark-500 transition-transform ${open ? 'rotate-180' : ''}`} />
                )}
              </span>
            </span>
            <span className="block text-xs text-dark-400"><Facts group={group} /></span>
            {refusals(group.items).map(([reason, relays]) => (
              <span key={reason} className="flex items-center gap-1.5 text-xs text-danger">
                <AlertTriangle className="w-3.5 h-3.5 shrink-0" />
                {t('loss.quarantine_blocked_on', {
                  where: t('loss.quarantine_relays_loc', { count: relayNames(relays).length }),
                  reason: refusalText(reason, t),
                })}
              </span>
            ))}
            {partialReasons(group.items).map(reason => (
              <span key={reason} className="block text-xs text-warning">
                {t('loss.quarantine_partial', { reason: refusalText(reason, t) })}
              </span>
            ))}
          </span>
        </button>
        <RowMenu label={t('loss.quarantine_actions')} actions={actions} busy={releasing} />
      </div>
      {open && expandable && (
        <div className="ml-[2.125rem] mr-4 mb-3 pl-3 border-l-2 border-dark-800 space-y-2">
          {group.chronicStrikes !== undefined && (
            <p className="text-xs text-danger">
              {t('loss.quarantine_chronic', { count: group.chronicStrikes })}. {t('loss.quarantine_chronic_hint')}
            </p>
          )}
          {groupBySituation(group.items).map(relays => (
            <Situation
              key={`${relays[0].target}@${relays[0].server_id}`}
              relays={relays}
              sustainedSeconds={sustainedSeconds}
              now={now}
            />
          ))}
        </div>
      )}
    </li>
  )
}
