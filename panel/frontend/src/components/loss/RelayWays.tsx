import { useEffect, useState, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { lossApi, type LossWay, type LossWaysResult } from '../../api/client'
import { HoverCard } from '../ui/HoverCard'
import { LOSS_BAD_PCT, LOSS_WARN_PCT } from '../ui/LossProbeBadge'

const WAYS_REFRESH_MS = 10_000
const SECONDS_IN_MINUTE = 60

interface RelayTarget {
  serverId: number
  serverName: string
  // ip:порт адреса назначения
  target: string
}

function lossCls(lossPct: number): string {
  if (lossPct > LOSS_BAD_PCT) return 'text-danger'
  return lossPct > LOSS_WARN_PCT ? 'text-warning' : 'text-success'
}

function WayRow({ way }: { way: LossWay }) {
  const { t } = useTranslation()
  return (
    <li className="flex items-baseline gap-x-3 gap-y-0.5 flex-wrap">
      <span className={way.used ? 'text-accent-400' : 'text-dark-600'}>{way.used ? '●' : '○'}</span>
      <span className="font-mono text-dark-100">{way.local_ip ?? t('loss.ways_default_address')}</span>
      <span className="text-dark-500">{t(way.used ? 'loss.ways_used' : 'loss.ways_unused')}</span>
      <span className={`ml-auto ${lossCls(way.loss_pct)}`}>{t('loss.ways_loss', { pct: way.loss_pct })}</span>
      {way.answers !== null && (
        <span className={way.answers ? 'text-success' : 'text-danger'}>
          {t(way.answers ? 'loss.ways_answers_yes' : 'loss.ways_answers_no')}
        </span>
      )}
      <span className="text-dark-500">
        {t('loss.ways_span', { minutes: Math.max(1, Math.round(way.span_sec / SECONDS_IN_MINUTE)) })}
      </span>
    </li>
  )
}

function Body({ result }: { result: LossWaysResult | null }) {
  const { t } = useTranslation()
  if (result === null) return <p className="text-dark-500">{t('loss.ways_loading')}</p>
  if (result.status !== 'ok') return <p className="text-warning">{t(`loss.ways_${result.status}`)}</p>
  if (result.ways.length === 0) return <p className="text-dark-500">{t('loss.ways_empty')}</p>
  return (
    <>
      <ul className="space-y-1.5">
        {result.ways.map(way => <WayRow key={`${way.used}:${way.local_ip}`} way={way} />)}
      </ul>
      <p className="text-dark-500 mt-2">{t('loss.ways_hint')}</p>
    </>
  )
}

/** Потери до адреса назначения с каждого IP релея. Данные читает сам — при открытии
 *  карточки и дальше раз в десять секунд, пока она открыта */
export default function RelayWays({ serverId, serverName, target }: RelayTarget) {
  const { t } = useTranslation()
  const [result, setResult] = useState<LossWaysResult | null>(null)

  useEffect(() => {
    let cancelled = false
    const load = async () => {
      try {
        const { data } = await lossApi.ways(serverId, target)
        if (!cancelled) setResult(data)
      } catch {
        // Оператору всё равно, кто не ответил — панель или нода: данных нет
        if (!cancelled) setResult({ status: 'unreachable', ways: [] })
      }
    }
    load()
    const timer = window.setInterval(load, WAYS_REFRESH_MS)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [serverId, target])

  return (
    <div className="w-[520px] max-w-[calc(100vw-16px)] p-3 text-xs">
      <p className="text-dark-200 font-medium mb-2">{t('loss.ways_title', { target, relay: serverName })}</p>
      <Body result={result} />
    </div>
  )
}

/** Всплывающая карточка вокруг метки: нода спрашивается, только когда карточку открыли.
 *  Встаёт сбоку от метки: под ней легла бы поверх таких же меток соседних строк, и от
 *  релея к релею нельзя было бы перейти, не отводя мышь в сторону */
export function RelayWaysCard({ children, ...relay }: RelayTarget & { children: ReactNode }) {
  return (
    <HoverCard side="beside" content={() => <RelayWays {...relay} />}>
      {children}
    </HoverCard>
  )
}
