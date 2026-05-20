import { motion } from 'framer-motion'
import { useTranslation } from 'react-i18next'

interface PeriodOption {
  value: string
  label: string
}

interface PeriodSelectorProps {
  value: string
  onChange: (period: string) => void
  className?: string
  options?: PeriodOption[]
}

export default function PeriodSelector({ value, onChange, className = '', options }: PeriodSelectorProps) {
  const { t } = useTranslation()

  const defaultPeriods = [
    { value: '1h', label: t('period.1h') },
    { value: '24h', label: t('period.24h') },
    { value: '7d', label: t('period.7d') },
    { value: '30d', label: t('period.30d') },
    { value: '365d', label: t('period.365d') },
  ]

  const periods = options || defaultPeriods

  return (
    <div className={`flex items-center bg-dark-800/60 backdrop-blur-sm rounded-xl p-1 border border-dark-700/50 ${className}`}>
      {periods.map((period) => (
        <motion.button
          key={period.value}
          onClick={() => onChange(period.value)}
          className={`relative px-3 py-1.5 text-sm font-medium rounded-lg transition-colors ${
            value === period.value
              ? 'text-white'
              : 'text-dark-400 hover:text-dark-200'
          }`}
          whileHover={{ scale: 1.02 }}
          whileTap={{ scale: 0.98 }}
        >
          {value === period.value && (
            <motion.div
              className="absolute inset-0 bg-gradient-to-r from-accent-500 to-accent-600 rounded-lg shadow-lg shadow-accent-500/20"
              layoutId="periodIndicator"
              initial={false}
              transition={{
                type: 'spring',
                stiffness: 400,
                damping: 30
              }}
            />
          )}
          <span className="relative z-10">{period.label}</span>
        </motion.button>
      ))}
    </div>
  )
}
