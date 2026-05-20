import { motion } from 'framer-motion'
import { Check, X, Loader2, AlertCircle } from 'lucide-react'
import { useTranslation } from 'react-i18next'

interface StatusBadgeProps {
  status: 'online' | 'offline' | 'loading' | 'error'
  showLabel?: boolean
  size?: 'sm' | 'md'
}

const config = {
  online: {
    icon: Check,
    className: 'bg-success/10 text-success border-success/20',
    dotClass: 'bg-success',
    glowClass: 'shadow-success/40',
  },
  offline: {
    icon: X,
    className: 'bg-danger/10 text-danger border-danger/20',
    dotClass: 'bg-danger',
    glowClass: 'shadow-danger/40',
  },
  loading: {
    icon: Loader2,
    className: 'bg-dark-700/50 text-dark-400 border-dark-600',
    dotClass: 'bg-dark-400',
    glowClass: '',
  },
  error: {
    icon: AlertCircle,
    className: 'bg-warning/10 text-warning border-warning/20',
    dotClass: 'bg-warning',
    glowClass: 'shadow-warning/40',
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
    md: 'px-2.5 py-1 text-xs'
  }

  const dotSizes = {
    sm: 'w-1.5 h-1.5',
    md: 'w-2 h-2'
  }

  return (
    <motion.div 
      initial={{ opacity: 0, scale: 0.8 }}
      animate={{ opacity: 1, scale: 1 }}
      transition={{ duration: 0.3 }}
      className={`
        inline-flex items-center gap-2 rounded-lg border font-medium
        backdrop-blur-sm transition-all duration-300
        ${className} ${sizeClasses[size]}
      `}
    >
      <div className="relative">
        <motion.span 
          className={`block rounded-full ${dotClass} ${dotSizes[size]}`}
          style={{
            boxShadow: status === 'online' || status === 'error' || status === 'offline' 
              ? `0 0 8px currentColor` 
              : 'none'
          }}
        />
        {status === 'online' && (
          <>
            <motion.span
              className={`absolute inset-0 rounded-full ${dotClass}`}
              animate={{
                scale: [1, 2, 2],
                opacity: [0.7, 0.3, 0],
              }}
              transition={{
                duration: 1.5,
                repeat: Infinity,
                ease: 'easeOut',
              }}
            />
            <motion.span
              className={`absolute inset-0 rounded-full ${dotClass}`}
              animate={{
                scale: [1, 1.5, 1.5],
                opacity: [0.5, 0.2, 0],
              }}
              transition={{
                duration: 1.5,
                repeat: Infinity,
                ease: 'easeOut',
                delay: 0.5,
              }}
            />
          </>
        )}
        {status === 'offline' && (
          <motion.span
            className={`absolute inset-0 rounded-full ${dotClass}`}
            animate={{
              opacity: [1, 0.4, 1],
            }}
            transition={{
              duration: 2,
              repeat: Infinity,
              ease: 'easeInOut',
            }}
          />
        )}
      </div>
      
      {status === 'loading' ? (
        <motion.div
          animate={{ rotate: 360 }}
          transition={{ duration: 1, repeat: Infinity, ease: 'linear' }}
        >
          <Loader2 className={size === 'sm' ? 'w-2.5 h-2.5' : 'w-3 h-3'} />
        </motion.div>
      ) : showLabel ? (
        <motion.span
          initial={{ opacity: 0, x: -5 }}
          animate={{ opacity: 1, x: 0 }}
          transition={{ delay: 0.1 }}
        >
          {labels[status]}
        </motion.span>
      ) : null}
    </motion.div>
  )
}
