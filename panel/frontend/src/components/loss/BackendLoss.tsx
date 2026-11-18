import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { AlertTriangle } from 'lucide-react'
import { lossApi, type LossProbeStats, type LossWay } from '../../api/client'
import LossProbeBadge from '../ui/LossProbeBadge'
import { RelayWaysCard } from './RelayWays'
import { verdictText, waysVerdict } from './waysVerdict'

const ALL_WAYS_REFRESH_MS = 15_000
const NO_WAYS = new Map<string, LossWay[]>()

/** Потери с каждого IP релея по всем его бэкендам: один запрос на всю таблицу, пока открыта
 *  статистика. Ключ — адрес бэкенда, как его печатает HAProxy */
export function useAllWays(serverId: number, enabled: boolean): Map<string, LossWay[]> {
  const [ways, setWays] = useState(NO_WAYS)

  useEffect(() => {
    if (!enabled) return
    let cancelled = false
    let timer: number | undefined
    const load = async () => {
      try {
        const { data } = await lossApi.allWays(serverId)
        if (cancelled) return
        setWays(new Map(data.targets.map(entry => [entry.target, entry.ways])))
        // Нода без запроса или с закрытым разделом не изменится, пока её не обновят: спрашивать снова незачем
        if (data.status === 'unsupported' || data.status === 'denied') return
      } catch {
        if (cancelled) return
      }
      timer = window.setTimeout(load, ALL_WAYS_REFRESH_MS)
    }
    load()
    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
  }, [serverId, enabled])

  return ways
}

interface Props {
  serverId: number
  serverName: string
  // Адрес бэкенда из статистики HAProxy; у строки без адреса сравнивать нечего
  addr?: string | null
  probe?: LossProbeStats | null
  ways?: LossWay[]
}

/** Потери бэкенда в таблице статистики HAProxy. Наведение открывает разбивку по IP релея, а значок
 *  рядом виден сразу: с выхода правила плохо, а с другого адреса нормально */
export default function BackendLoss({ serverId, serverName, addr, probe, ways }: Props) {
  const { t } = useTranslation()
  if (!addr) return <LossProbeBadge probe={probe} />
  const verdict = ways ? waysVerdict(ways) : null
  return (
    <span className="inline-flex items-center gap-2 flex-wrap">
      <RelayWaysCard serverId={serverId} serverName={serverName} target={addr}>
        <span className="cursor-help"><LossProbeBadge probe={probe} bare /></span>
      </RelayWaysCard>
      {verdict?.kind === 'exit' && (
        <span
          title={verdictText(verdict, t)}
          className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded border text-2xs font-medium whitespace-nowrap bg-warning/10 text-warning border-warning/20"
        >
          <AlertTriangle className="w-3 h-3" />
          {t(verdict.silent ? 'loss.ways_mark_answers' : 'loss.ways_mark_better')}
        </span>
      )}
    </span>
  )
}
