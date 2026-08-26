import { useCallback, useState, type ReactNode } from 'react'
import { Server, Cpu, MemoryStick, HardDrive, Loader2, type LucideIcon } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { systemApi, type PanelServerStats } from '../../api/client'
import { formatBytes } from '../../utils/format'
import { useAutoRefresh } from '../../hooks/useAutoRefresh'
import ProgressBar from '../ui/ProgressBar'
import { SettingsSection } from './SettingsSection'

const STATS_POLL_MS = 5000

interface StatTileProps {
  icon: LucideIcon
  title: string
  meta: string
  percent: number
  label: string
  footer: ReactNode
}

function StatTile({ icon: Icon, title, meta, percent, label, footer }: StatTileProps) {
  return (
    <div className="p-4 bg-dark-800/50 rounded-xl border border-dark-700/50">
      <div className="flex items-center gap-2 mb-3">
        <Icon className="w-4 h-4 text-dark-400" />
        <span className="text-sm text-dark-300">{title}</span>
        <span className="text-xs text-dark-500 ml-auto">{meta}</span>
      </div>
      <ProgressBar value={percent} label={label} showLabel />
      <div className="flex items-center justify-between gap-2 text-xs text-dark-500 mt-2">{footer}</div>
    </div>
  )
}

export function PanelHostStatsCard() {
  const { t } = useTranslation()
  const [stats, setStats] = useState<PanelServerStats | null>(null)
  const [loading, setLoading] = useState(true)

  const fetchStats = useCallback(async () => {
    try {
      const response = await systemApi.getServerStats()
      setStats(response.data)
    } catch (err) {
      console.error('Failed to fetch server stats:', err)
    } finally {
      setLoading(false)
    }
  }, [])

  useAutoRefresh(fetchStats, { customInterval: STATS_POLL_MS })

  return (
    <SettingsSection icon={Server} title={t('settings.server_stats')} description={t('settings.server_stats_desc')}>
      {loading ? (
        <div className="flex items-center justify-center py-4">
          <Loader2 className="w-5 h-5 text-accent-500 animate-spin" />
        </div>
      ) : stats ? (
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
          <StatTile
            icon={Cpu}
            title="CPU"
            meta={`${stats.cpu.cores} ${t('settings.cores')}`}
            percent={stats.cpu.percent}
            label={t('settings.usage')}
            footer={<span>{t('settings.load_avg')}: {stats.cpu.load_avg_1.toFixed(2)} / {stats.cpu.load_avg_5.toFixed(2)} / {stats.cpu.load_avg_15.toFixed(2)}</span>}
          />
          <StatTile
            icon={MemoryStick}
            title="RAM"
            meta={formatBytes(stats.memory.total)}
            percent={stats.memory.percent}
            label={`${t('settings.used')}: ${formatBytes(stats.memory.used)}`}
            footer={
              <>
                <span>{t('settings.available')}: {formatBytes(stats.memory.available)}</span>
                {stats.memory.swap_total > 0 && (
                  <span>Swap: {formatBytes(stats.memory.swap_used)} / {formatBytes(stats.memory.swap_total)}</span>
                )}
              </>
            }
          />
          <StatTile
            icon={HardDrive}
            title={t('settings.disk')}
            meta={formatBytes(stats.disk.total)}
            percent={stats.disk.percent}
            label={`${t('settings.used')}: ${formatBytes(stats.disk.used)}`}
            footer={<span>{t('settings.free')}: {formatBytes(stats.disk.free)}</span>}
          />
        </div>
      ) : (
        <div className="text-sm text-dark-400 text-center py-4">{t('settings.stats_unavailable')}</div>
      )}
    </SettingsSection>
  )
}
