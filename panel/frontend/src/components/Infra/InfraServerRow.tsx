import { useEffect, useRef } from 'react'
import { motion } from 'framer-motion'
import { X } from 'lucide-react'
import { useNavigate, useParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { Tooltip } from '../ui/Tooltip'
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
  highlighted?: boolean
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

// Узлы дерева раскрываются анимацией высоты (~0.3 с). Пока высота растёт, scroll anchoring
// браузера удерживает видимые карточки на месте и сбивает плавную прокрутку — ждём, пока раскладка устоится
const SCROLL_AFTER_EXPAND_MS = 350

function isFullyInViewport(element: HTMLElement): boolean {
  const { top, bottom } = element.getBoundingClientRect()
  return top >= 0 && bottom <= window.innerHeight
}

export default function InfraServerRow({ server, highlighted = false, onRemove }: InfraServerRowProps) {
  const navigate = useNavigate()
  const { uid } = useParams()
  const { t } = useTranslation()
  const rowRef = useRef<HTMLDivElement>(null)

  // Срабатывает и при монтировании: строка появляется, когда дерево раскрыло путь к серверу
  useEffect(() => {
    if (!highlighted) return
    const timer = setTimeout(() => {
      const row = rowRef.current
      if (!row || isFullyInViewport(row)) return
      row.scrollIntoView({ behavior: 'smooth', block: 'center' })
    }, SCROLL_AFTER_EXPAND_MS)
    return () => clearTimeout(timer)
  }, [highlighted])

  const cpu = server.metrics?.cpu?.usage_percent
  const ram = server.metrics?.memory?.ram?.percent
  const rx = server.metrics?.network?.total?.rx_bytes_per_sec ?? 0
  const tx = server.metrics?.network?.total?.tx_bytes_per_sec ?? 0
  const ip = parseHost(server.url)
  const isOnline = server.status === 'online'

  return (
    <motion.div
      ref={rowRef}
      initial={{ opacity: 0, y: -4 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: -4 }}
      className={`group flex items-center gap-3 px-3 py-2 rounded-lg cursor-pointer transition-colors ${
        highlighted
          ? 'bg-accent-500/10 ring-1 ring-inset ring-accent-500/40 hover:bg-accent-500/15'
          : 'hover:bg-dark-700/50'
      }`}
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
        <Tooltip label={t('infra.remove_server')}>
          <button
            className="opacity-60 group-hover:opacity-100 p-1.5 rounded-lg hover:bg-dark-600 text-dark-400 hover:text-danger transition-all"
            onClick={e => { e.stopPropagation(); onRemove() }}
          >
            <X className="w-4 h-4" />
          </button>
        </Tooltip>
      )}
    </motion.div>
  )
}
