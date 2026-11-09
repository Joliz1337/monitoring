import { memo, useMemo, type ReactNode } from 'react'
import { Cpu, LineChart, MemoryStick, Network, PowerOff, Wifi, WifiOff } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import type { ServerWithMetrics } from '../../stores/serversStore'
import { summarizeLoad } from '../../utils/fleetLoad'
import { formatBytes, formatBitsPerSecLocalized } from '../../utils/format'
import { Tooltip } from '../ui/Tooltip'
import LoadHistoryPanel, { type LoadMetric } from './LoadHistoryPanel'

const WARNING_PERCENT = 50
const DANGER_PERCENT = 80

function loadTone(percent: number): string {
  if (percent >= DANGER_PERCENT) return 'text-danger'
  if (percent >= WARNING_PERCENT) return 'text-warning'
  return 'text-dark-200'
}

const BADGE_CLASS = 'inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md border text-xs font-mono whitespace-nowrap'

function Badge({ tooltip, children }: { tooltip: ReactNode; children: ReactNode }) {
  return (
    <Tooltip label={tooltip}>
      <span className={`${BADGE_CLASS} bg-dark-800/60 border-dark-700/40`}>{children}</span>
    </Tooltip>
  )
}

function ChartBadge({ tooltip, isActive, onClick, children }: {
  tooltip: ReactNode
  isActive: boolean
  onClick: () => void
  children: ReactNode
}) {
  return (
    <Tooltip label={tooltip}>
      <button
        type="button"
        onClick={onClick}
        aria-expanded={isActive}
        className={`${BADGE_CLASS} transition-colors ${
          isActive ? 'bg-dark-800/80 border-accent-500/60' : 'bg-dark-800/60 border-dark-700/40 hover:border-dark-600'
        }`}
      >
        {children}
      </button>
    </Tooltip>
  )
}

function FolderStatusCountsInner({ servers, disabled = 0 }: { servers: ServerWithMetrics[]; disabled?: number }) {
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
        <Wifi className={`w-3.5 h-3.5 ${onlineTone}`} />
        <span className={onlineTone}>{online}</span>
      </Badge>
      {offline > 0 && (
        <Badge tooltip={t('common.offline')}>
          <WifiOff className="w-3.5 h-3.5 text-danger" />
          <span className="text-danger">{offline}</span>
        </Badge>
      )}
      {disabled > 0 && (
        <Badge tooltip={t('common.disabled')}>
          <PowerOff className="w-3.5 h-3.5 text-dark-400" />
          <span className="text-dark-400">{disabled}</span>
        </Badge>
      )}
    </span>
  )
}

interface FolderChartProps {
  servers: ServerWithMetrics[]
  /** Открытый график папки, null — закрыт */
  chart: LoadMetric | null
  onChartChange: (metric: LoadMetric | null) => void
}

function FolderLoadBadgesInner({ servers, chart, onChartChange, className = '' }: FolderChartProps & { className?: string }) {
  const { t } = useTranslation()
  const load = useMemo(() => summarizeLoad(servers), [servers])
  if (!load) return null

  const scope = <div className="text-dark-400">{t('dashboard.fleet_chart_scope', { count: load.count })}</div>
  const hint = <div className="text-dark-500">{t('dashboard.folder_chart_hint')}</div>
  const toggle = (metric: LoadMetric) => onChartChange(chart === metric ? null : metric)

  return (
    <span className={`flex flex-wrap items-center gap-1 ${className}`}>
      <ChartBadge
        tooltip={<>{t('common.cpu')} · {t('common.cores_count', { count: load.cores })}{scope}{hint}</>}
        isActive={chart === 'cpu'}
        onClick={() => toggle('cpu')}
      >
        <Cpu className="w-3.5 h-3.5 text-accent-400" />
        <span className={loadTone(load.cpuPercent)}>{load.cpuPercent.toFixed(0)}%</span>
      </ChartBadge>
      <ChartBadge
        tooltip={<>{t('common.ram')} · {formatBytes(load.ramUsed, 0)} / {formatBytes(load.ramTotal, 0)}{scope}{hint}</>}
        isActive={chart === 'memory'}
        onClick={() => toggle('memory')}
      >
        <MemoryStick className="w-3.5 h-3.5 text-purple" />
        <span className={loadTone(load.ramPercent)}>{load.ramPercent.toFixed(0)}%</span>
      </ChartBadge>
      <ChartBadge
        tooltip={<>{t('common.download')} / {t('common.upload')}{scope}{hint}</>}
        isActive={chart === 'network'}
        onClick={() => toggle('network')}
      >
        <Network className="w-3.5 h-3.5 text-dark-400" />
        <span className="text-success">↓ {formatBitsPerSecLocalized(load.rx, t)}</span>
        <span className="text-accent-400 ml-1">↑ {formatBitsPerSecLocalized(load.tx, t)}</span>
      </ChartBadge>
    </span>
  )
}

/** Явная кнопка графика: по бейджам загрузки не видно, что они нажимаются */
function FolderChartButtonInner({ servers, chart, onChartChange }: FolderChartProps) {
  const { t } = useTranslation()
  const load = useMemo(() => summarizeLoad(servers), [servers])
  if (!load) return null

  const isOpen = chart !== null
  const label = t('dashboard.folder_chart_button')

  return (
    <Tooltip label={label}>
      <button
        type="button"
        onClick={() => onChartChange(isOpen ? null : 'cpu')}
        aria-expanded={isOpen}
        aria-label={label}
        className={`p-1.5 transition rounded-lg ${
          isOpen ? 'text-accent-400 bg-accent-500/10' : 'text-dark-500 hover:text-dark-300 hover:bg-dark-800/50'
        }`}
      >
        <LineChart className="w-4 h-4" />
      </button>
    </Tooltip>
  )
}

/** История нагрузки папки — тот же график, что под плитками парка */
function FolderLoadChartInner({ servers, folder, chart, onChartChange }: FolderChartProps & { folder: string }) {
  const load = useMemo(() => summarizeLoad(servers), [servers])
  // Ни одного сервера онлайн — нет и кнопок, которыми график закрывается
  if (!load) return null

  return (
    <LoadHistoryPanel
      metric={chart}
      onMetricChange={onChartChange}
      folder={folder}
      onlineCount={load.count}
      periodKey="dashboard.folder-period"
      className="mx-3 mb-3 bg-dark-800/30 border-dark-700/40"
    />
  )
}

export const FolderStatusCounts = memo(FolderStatusCountsInner)
export const FolderLoadBadges = memo(FolderLoadBadgesInner)
export const FolderChartButton = memo(FolderChartButtonInner)
export const FolderLoadChart = memo(FolderLoadChartInner)
