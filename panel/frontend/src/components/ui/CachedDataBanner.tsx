import { motion } from 'framer-motion'
import { Database, Clock, WifiOff } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { formatTimeAgo } from '../../utils/format'

interface CachedDataBannerProps {
  cachedAt: Date | null
  className?: string
  compact?: boolean
}

export default function CachedDataBanner({ cachedAt, className = '', compact = false }: CachedDataBannerProps) {
  const { t } = useTranslation()

  if (compact) {
    return (
      <motion.div
        initial={{ opacity: 0, scale: 0.9 }}
        animate={{ opacity: 1, scale: 1 }}
        className={`inline-flex items-center gap-1.5 px-2 py-1 bg-warning/10 border border-warning/30 rounded-lg ${className}`}
      >
        <Database className="w-3.5 h-3.5 text-warning" />
        <span className="text-xs text-warning font-medium">
          {t('cache.cached')}
        </span>
        {cachedAt && (
          <span className="text-xs text-warning/70">
            {formatTimeAgo(cachedAt)}
          </span>
        )}
      </motion.div>
    )
  }

  return (
    <motion.div
      initial={{ opacity: 0, y: -10 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: -10 }}
      className={`flex items-center gap-3 p-4 bg-warning/10 border border-warning/30 rounded-xl mb-6 ${className}`}
    >
      <div className="flex-shrink-0 p-2 bg-warning/20 rounded-lg">
        <WifiOff className="w-5 h-5 text-warning" />
      </div>
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2">
          <Database className="w-4 h-4 text-warning" />
          <span className="text-warning font-medium">
            {t('cache.showing_cached')}
          </span>
        </div>
        {cachedAt && (
          <div className="flex items-center gap-1.5 mt-1 text-warning/70 text-sm">
            <Clock className="w-3.5 h-3.5" />
            <span>
              {t('cache.last_update', { time: formatTimeAgo(cachedAt) })}
            </span>
          </div>
        )}
      </div>
    </motion.div>
  )
}

/**
 * Small indicator badge for cards/headers
 */
export function CachedIndicator({ cachedAt, className = '' }: { cachedAt?: Date | null; className?: string }) {
  const { t } = useTranslation()

  return (
    <motion.div
      initial={{ opacity: 0, scale: 0.8 }}
      animate={{ opacity: 1, scale: 1 }}
      className={`inline-flex items-center gap-1 px-1.5 py-0.5 bg-warning/15 border border-warning/25 rounded text-[10px] text-warning ${className}`}
      title={cachedAt ? t('cache.last_update', { time: formatTimeAgo(cachedAt) }) : t('cache.cached')}
    >
      <Database className="w-2.5 h-2.5" />
      <span className="font-medium">{t('cache.cached')}</span>
    </motion.div>
  )
}
