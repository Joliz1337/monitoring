import { memo, useMemo, useState } from 'react'
import { Cpu, MemoryStick, ArrowDownToLine, ArrowUpFromLine } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import type { ServerWithMetrics } from '../../stores/serversStore'
import { formatBytes, formatBitsPerSecLocalized } from '../../utils/format'
import { summarizeLoad } from '../../utils/fleetLoad'
import { Tooltip } from '../ui/Tooltip'
import LoadHistoryPanel, { type LoadMetric } from './LoadHistoryPanel'

interface StatTileProps {
  icon: React.ReactNode
  iconBg: string
  label: string
  value: string
  sub?: string
  metric: LoadMetric
  isActive: boolean
  onSelect: (metric: LoadMetric) => void
}

function StatTile({ icon, iconBg, label, value, sub, metric, isActive, onSelect }: StatTileProps) {
  return (
    <button
      type="button"
      onClick={() => onSelect(metric)}
      aria-expanded={isActive}
      className={`bg-dark-900/50 border rounded-xl px-4 py-3 flex items-center gap-3 min-w-0 text-left transition-colors ${
        isActive ? 'border-accent-500/60 bg-dark-800/60' : 'border-dark-800/50 hover:border-dark-700'
      }`}
    >
      <div className={`w-9 h-9 rounded-lg ${iconBg} flex items-center justify-center flex-shrink-0`}>
        {icon}
      </div>
      <div className="min-w-0 flex-1">
        <div className="text-xs text-dark-400">{label}</div>
        <Tooltip label={sub ? `${value} · ${sub}` : value}>
          <div className="text-sm font-mono text-dark-100 truncate">
            {value}
            {sub && <span className="text-dark-500"> · {sub}</span>}
          </div>
        </Tooltip>
      </div>
    </button>
  )
}

function FleetSummaryInner({ servers }: { servers: ServerWithMetrics[] }) {
  const { t } = useTranslation()
  // Приём и отдача — одна метрика на двух плитках: график у них общий, поэтому
  // подсвечиваются обе, и клик по любой из подсвеченных сворачивает блок
  const [expanded, setExpanded] = useState<LoadMetric | null>(null)

  const totals = useMemo(() => summarizeLoad(servers), [servers])

  if (!totals) return null

  const toggle = (target: LoadMetric) => setExpanded(current => (current === target ? null : target))

  return (
    <div className="mb-6 fade-in">
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <StatTile
          icon={<Cpu className="w-4 h-4 text-accent-400" />}
          iconBg="bg-accent-500/15"
          label={t('common.cpu')}
          value={`${totals.cpuPercent.toFixed(0)}%`}
          sub={t('common.cores_count', { count: totals.cores })}
          metric="cpu"
          isActive={expanded === 'cpu'}
          onSelect={toggle}
        />
        <StatTile
          icon={<MemoryStick className="w-4 h-4 text-purple" />}
          iconBg="bg-purple/15"
          label={t('common.ram')}
          value={`${formatBytes(totals.ramUsed, 0)} / ${formatBytes(totals.ramTotal, 0)}`}
          sub={`${totals.ramPercent.toFixed(0)}%`}
          metric="memory"
          isActive={expanded === 'memory'}
          onSelect={toggle}
        />
        <StatTile
          icon={<ArrowDownToLine className="w-4 h-4 text-success" />}
          iconBg="bg-success/15"
          label={t('common.download')}
          value={formatBitsPerSecLocalized(totals.rx, t)}
          metric="network"
          isActive={expanded === 'network'}
          onSelect={toggle}
        />
        <StatTile
          icon={<ArrowUpFromLine className="w-4 h-4 text-accent-400" />}
          iconBg="bg-accent-500/15"
          label={t('common.upload')}
          value={formatBitsPerSecLocalized(totals.tx, t)}
          metric="network"
          isActive={expanded === 'network'}
          onSelect={toggle}
        />
      </div>

      <LoadHistoryPanel
        metric={expanded}
        onlineCount={totals.count}
        periodKey="dashboard.fleet-period"
        className="mt-3 bg-dark-900/50 border-dark-800/50"
      />
    </div>
  )
}

const FleetSummary = memo(FleetSummaryInner)
export default FleetSummary
