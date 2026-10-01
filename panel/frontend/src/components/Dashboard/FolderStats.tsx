import { memo, useMemo, type ReactNode } from 'react'
import { Cpu, MemoryStick, Network, Wifi, WifiOff } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import type { ServerWithMetrics } from '../../stores/serversStore'
import { summarizeLoad } from '../../utils/fleetLoad'
import { formatBytes, formatBitsPerSecLocalized } from '../../utils/format'
import { Tooltip } from '../ui/Tooltip'

const WARNING_PERCENT = 50
const DANGER_PERCENT = 80

function loadTone(percent: number): string {
  if (percent >= DANGER_PERCENT) return 'text-danger'
  if (percent >= WARNING_PERCENT) return 'text-warning'
  return 'text-dark-200'
}

function Badge({ tooltip, children }: { tooltip: ReactNode; children: ReactNode }) {
  return (
    <Tooltip label={tooltip}>
      <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md bg-dark-800/60 border border-dark-700/40 text-xs font-mono whitespace-nowrap">
        {children}
      </span>
    </Tooltip>
  )
}

function FolderStatusCountsInner({ servers }: { servers: ServerWithMetrics[] }) {
  const { t } = useTranslation()
  const { online, offline } = useMemo(() => {
    let onlineCount = 0
    let offlineCount = 0
    for (const s of servers) {
      if (s.status === 'online') onlineCount++
      else if (s.status === 'offline') offlineCount++
    }
    return { online: onlineCount, offline: offlineCount }
  }, [servers])

  const onlineTone = online > 0 ? 'text-success' : 'text-dark-500'

  return (
    <span className="flex items-center gap-1 flex-shrink-0">
      <Badge tooltip={t('common.online')}>
        <Wifi className={`w-3 h-3 ${onlineTone}`} />
        <span className={onlineTone}>{online}</span>
      </Badge>
      {offline > 0 && (
        <Badge tooltip={t('common.offline')}>
          <WifiOff className="w-3 h-3 text-danger" />
          <span className="text-danger">{offline}</span>
        </Badge>
      )}
    </span>
  )
}

function FolderLoadBadgesInner({ servers, className = '' }: { servers: ServerWithMetrics[]; className?: string }) {
  const { t } = useTranslation()
  const load = useMemo(() => summarizeLoad(servers), [servers])
  if (!load) return null

  const scope = <div className="text-dark-400">{t('dashboard.fleet_chart_scope', { count: load.count })}</div>

  return (
    <span className={`flex flex-wrap items-center gap-1 ${className}`}>
      <Badge tooltip={<>{t('common.cpu')} · {t('common.cores_count', { count: load.cores })}{scope}</>}>
        <Cpu className="w-3 h-3 text-accent-400" />
        <span className={loadTone(load.cpuPercent)}>{load.cpuPercent.toFixed(0)}%</span>
      </Badge>
      <Badge tooltip={<>{t('common.ram')} · {formatBytes(load.ramUsed, 0)} / {formatBytes(load.ramTotal, 0)}{scope}</>}>
        <MemoryStick className="w-3 h-3 text-purple" />
        <span className={loadTone(load.ramPercent)}>{load.ramPercent.toFixed(0)}%</span>
      </Badge>
      <Badge tooltip={<>{t('common.download')} / {t('common.upload')}{scope}</>}>
        <Network className="w-3 h-3 text-dark-400" />
        <span className="text-success">↓ {formatBitsPerSecLocalized(load.rx, t)}</span>
        <span className="text-accent-400 ml-1">↑ {formatBitsPerSecLocalized(load.tx, t)}</span>
      </Badge>
    </span>
  )
}

export const FolderStatusCounts = memo(FolderStatusCountsInner)
export const FolderLoadBadges = memo(FolderLoadBadgesInner)
