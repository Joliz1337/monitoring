import { Loader2, AlertCircle } from 'lucide-react'
import { useTranslation } from 'react-i18next'

interface StatusBadgeProps {
  status: 'online' | 'offline' | 'loading' | 'error'
  showLabel?: boolean
  size?: 'sm' | 'md'
}

const config = {
  online: {
    className: 'bg-success/10 text-success border-success/20',
    dotClass: 'bg-success',
  },
  offline: {
    className: 'bg-danger/10 text-danger border-danger/20',
    dotClass: 'bg-danger',
  },
  loading: {
    className: 'bg-dark-700/50 text-dark-400 border-dark-600',
    dotClass: 'bg-dark-400',
  },
  error: {
    className: 'bg-warning/10 text-warning border-warning/20',
    dotClass: 'bg-warning',
  },
}

export default function StatusBadge({ status, showLabel = true, size = 'md' }: StatusBadgeProps) {
  const { t } = useTranslation()
  const { className, dotClass } = config[status]

  const labels = {
    online: t('common.online'),
    offline: t('common.offline'),
    loading: t('common.loading'),
    error: t('common.error'),
  }

  const sizeClasses = {
    sm: 'px-2 py-0.5 text-[10px]',
    md: 'px-2.5 py-1 text-xs',
  }

  const dotSizes = {
    sm: 'w-1.5 h-1.5',
    md: 'w-2 h-2',
  }

  const hasGlow = status === 'online' || status === 'error' || status === 'offline'

  return (
    <div
      className={`inline-flex items-center gap-2 rounded-lg border font-medium transition-colors duration-300 ${className} ${sizeClasses[size]}`}
    >
      <div className="relative">
        <span
          className={`block rounded-full ${dotClass} ${dotSizes[size]} ${status === 'offline' ? 'status-blink' : ''}`}
          style={{ boxShadow: hasGlow ? '0 0 8px currentColor' : 'none' }}
        />
        {status === 'online' && (
          <>
            <span className={`status-ping ${dotClass}`} />
            <span className={`status-ping status-ping-delay ${dotClass}`} />
          </>
        )}
      </div>

      {status === 'loading' ? (
        <Loader2 className={`${size === 'sm' ? 'w-2.5 h-2.5' : 'w-3 h-3'} icon-spin`} />
      ) : showLabel ? (
        <span>{labels[status]}</span>
      ) : status === 'error' ? (
        <AlertCircle className={size === 'sm' ? 'w-2.5 h-2.5' : 'w-3 h-3'} />
      ) : null}
    </div>
  )
}
