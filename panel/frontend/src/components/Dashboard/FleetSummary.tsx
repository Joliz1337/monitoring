import { memo, useMemo } from 'react'
import { Cpu, MemoryStick, ArrowDownToLine, ArrowUpFromLine } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import type { ServerWithMetrics } from '../../stores/serversStore'
import { formatBytes, formatBitsPerSecLocalized } from '../../utils/format'

interface StatTileProps {
  icon: React.ReactNode
  iconBg: string
  label: string
  value: string
  sub?: string
}

function StatTile({ icon, iconBg, label, value, sub }: StatTileProps) {
  return (
    <div className="bg-dark-900/50 border border-dark-800/50 rounded-xl px-4 py-3 flex items-center gap-3 min-w-0">
      <div className={`w-9 h-9 rounded-lg ${iconBg} flex items-center justify-center flex-shrink-0`}>
        {icon}
      </div>
      <div className="min-w-0 flex-1">
        <div className="text-xs text-dark-400">{label}</div>
        <div className="text-sm font-mono text-dark-100 truncate" title={sub ? `${value} · ${sub}` : value}>
          {value}
          {sub && <span className="text-dark-500"> · {sub}</span>}
        </div>
      </div>
    </div>
  )
}

function FleetSummaryInner({ servers }: { servers: ServerWithMetrics[] }) {
  const { t } = useTranslation()

  const totals = useMemo(() => {
    let count = 0
    let cores = 0
    let cpuWeighted = 0
    let ramUsed = 0
    let ramTotal = 0
    let rx = 0
    let tx = 0

    for (const s of servers) {
      if (!s.is_active || s.status !== 'online' || !s.metrics) continue
      const m = s.metrics
      count++
      const serverCores = m.cpu.cores_logical > 0 ? m.cpu.cores_logical : 1
      cores += serverCores
      cpuWeighted += (m.cpu.usage_percent || 0) * serverCores
      ramUsed += m.memory.ram.used || 0
      ramTotal += m.memory.ram.total || 0
      rx += m.network.total?.rx_bytes_per_sec || 0
      tx += m.network.total?.tx_bytes_per_sec || 0
    }

    if (count === 0) return null

    return {
      cores,
      cpuPercent: cpuWeighted / cores,
      ramUsed,
      ramTotal,
      ramPercent: ramTotal > 0 ? (ramUsed / ramTotal) * 100 : 0,
      rx,
      tx,
    }
  }, [servers])

  if (!totals) return null

  return (
    <div className="grid grid-cols-2 lg:grid-cols-4 gap-3 mb-6 fade-in">
      <StatTile
        icon={<Cpu className="w-4 h-4 text-accent-400" />}
        iconBg="bg-accent-500/15"
        label={t('common.cpu')}
        value={`${totals.cpuPercent.toFixed(0)}%`}
        sub={t('common.cores_count', { count: totals.cores })}
      />
      <StatTile
        icon={<MemoryStick className="w-4 h-4 text-purple" />}
        iconBg="bg-purple/15"
        label={t('common.ram')}
        value={`${formatBytes(totals.ramUsed, 0)} / ${formatBytes(totals.ramTotal, 0)}`}
        sub={`${totals.ramPercent.toFixed(0)}%`}
      />
      <StatTile
        icon={<ArrowDownToLine className="w-4 h-4 text-success" />}
        iconBg="bg-success/15"
        label={t('common.download')}
        value={formatBitsPerSecLocalized(totals.rx, t)}
      />
      <StatTile
        icon={<ArrowUpFromLine className="w-4 h-4 text-accent-400" />}
        iconBg="bg-accent-500/15"
        label={t('common.upload')}
        value={formatBitsPerSecLocalized(totals.tx, t)}
      />
    </div>
  )
}

const FleetSummary = memo(FleetSummaryInner)
export default FleetSummary
