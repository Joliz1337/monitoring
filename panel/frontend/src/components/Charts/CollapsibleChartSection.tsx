import { useEffect, useRef, useState, type ReactNode } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { ChevronDown } from 'lucide-react'
import ChartLoadingOverlay from './ChartLoadingOverlay'

interface CollapsibleChartSectionProps {
  icon: ReactNode
  title: string
  subtitle?: string
  isLoading?: boolean
  className?: string
  /** Вызывается при раскрытии/сворачивании и со значением false при размонтировании */
  onExpandedChange?: (expanded: boolean) => void
  children: ReactNode
}

export default function CollapsibleChartSection({
  icon,
  title,
  subtitle,
  isLoading = false,
  className = '',
  onExpandedChange,
  children,
}: CollapsibleChartSectionProps) {
  const [isExpanded, setIsExpanded] = useState(false)

  // Родитель узнаёт о сворачивании при уходе со страницы или смене периода,
  // а не только по клику — иначе он продолжал бы запрашивать содержимое закрытого блока
  const onExpandedChangeRef = useRef(onExpandedChange)
  onExpandedChangeRef.current = onExpandedChange
  useEffect(() => () => onExpandedChangeRef.current?.(false), [])

  const toggle = () => {
    const next = !isExpanded
    setIsExpanded(next)
    onExpandedChange?.(next)
  }

  return (
    <div className={className}>
      <motion.button
        onClick={toggle}
        className="w-full flex items-center justify-between p-3 bg-dark-800/50 hover:bg-dark-800 rounded-xl transition-colors group"
        whileHover={{ scale: 1.005 }}
        whileTap={{ scale: 0.995 }}
      >
        <div className="flex items-center gap-2 text-dark-300">
          {icon}
          <span className="text-sm font-medium">{title}</span>
          {subtitle && <span className="text-xs text-dark-500">{subtitle}</span>}
        </div>
        <motion.div animate={{ rotate: isExpanded ? 180 : 0 }} transition={{ duration: 0.2 }}>
          <ChevronDown className="w-4 h-4 text-dark-400 group-hover:text-dark-300" />
        </motion.div>
      </motion.button>

      <AnimatePresence>
        {isExpanded && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.3 }}
            className="overflow-hidden"
          >
            <div className={`mt-3 p-4 bg-dark-800/30 rounded-xl relative transition-opacity duration-200 ${isLoading ? 'opacity-60' : ''}`}>
              {children}
              <ChartLoadingOverlay visible={isLoading} />
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}
