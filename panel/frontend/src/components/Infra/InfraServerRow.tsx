import { motion } from 'framer-motion'
import { X } from 'lucide-react'
import { useNavigate, useParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { formatPercent, formatBitsPerSec } from '../../utils/format'
import type { ServerMetrics } from '../../api/client'

interface InfraServerRowProps {
  server: {
    id: number
    name: string
    url: string
    status: 'online' | 'offline' | 'loading' | 'error'
    metrics?: ServerMetrics | null
  }
  onRemove?: () => void
}

const STATUS_DOT: Record<string, string> = {
  online: 'bg-success shadow-[0_0_8px_theme(colors.success)]',
  offline: 'bg-danger shadow-[0_0_8px_theme(colors.danger)]',
  loading: 'bg-dark-400',
  error: 'bg-warning shadow-[0_0_8px_theme(colors.warning)]',
}

function parseHost(url: string): string {
  const match = url.match(/^https?:\/\/([^:/]+)/)
  return match?.[1] ?? url
}

export default function InfraServerRow({ server, onRemove }: InfraServerRowProps) {
  const navigate = useNavigate()
  const { uid } = useParams()
  const { t } = useTranslation()

  const cpu = server.metrics?.cpu?.usage_percent
  const ram = server.metrics?.memory?.ram?.percent
  const rx = server.metrics?.network?.total?.rx_bytes_per_sec ?? 0
  const tx = server.metrics?.network?.total?.tx_bytes_per_sec ?? 0
  const ip = parseHost(server.url)
  const isOnline = server.status === 'online'

  return (
    <motion.div
      initial={{ opacity: 0, y: -4 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: -4 }}
      className="group flex items-center gap-3 px-3 py-2 rounded-lg hover:bg-dark-700/50 cursor-pointer transition-colors"
      onClick={() => navigate(`/${uid}/server/${server.id}`)}
    >
      {/* Status dot */}
      <span className={`w-2 h-2 rounded-full shrink-0 ${STATUS_DOT[server.status] || STATUS_DOT.loading}`} />

      {/* Name + IP */}
      <div className="flex items-center gap-2 min-w-0 flex-1">
        <span className="text-sm font-medium text-dark-100 truncate">{server.name}</span>
        <span className="text-xs text-dark-400 shrink-0">{ip}</span>
      </div>

      {/* Metrics (only when online) */}
      {isOnline && cpu != null && ram != null && (
        <div className="hidden sm:flex items-center gap-3 text-xs text-dark-300 shrink-0">
          <span>CPU {formatPercent(cpu, 0)}</span>
          <span>RAM {formatPercent(ram, 0)}</span>
          {(rx > 0 || tx > 0) && (
            <span className="font-mono font-medium text-dark-200">
              <span className="text-accent-400">↓</span>{formatBitsPerSec(rx, 0)}{' '}
              <span className="text-accent-400">↑</span>{formatBitsPerSec(tx, 0)}
            </span>
          )}
        </div>
      )}

      {/* Remove button */}
      {onRemove && (
        <button
          className="opacity-0 group-hover:opacity-100 p-1 rounded hover:bg-dark-600 text-dark-400 hover:text-danger transition-all"
          onClick={e => { e.stopPropagation(); onRemove() }}
          title={t('infra.remove_server')}
        >
          <X className="w-3.5 h-3.5" />
        </button>
      )}
    </motion.div>
  )
}
