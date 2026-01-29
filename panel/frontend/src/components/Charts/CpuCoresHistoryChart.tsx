import { useState, useMemo } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { useTranslation } from 'react-i18next'
import { ChevronDown, Cpu } from 'lucide-react'
import MultiLineChart from './MultiLineChart'

interface HistoryDataPoint {
  timestamp: string
  per_cpu_percent?: number[]
}

interface CpuCoresHistoryChartProps {
  history: HistoryDataPoint[]
  period: string
  coreCount: number  // Current core count from live metrics
  isLoading?: boolean
  className?: string
}

// Generate distinct colors for CPU cores
const CORE_COLORS = [
  '#22d3ee', // cyan
  '#10b981', // green
  '#f59e0b', // amber
  '#ef4444', // red
  '#8b5cf6', // violet
  '#ec4899', // pink
  '#06b6d4', // cyan-500
  '#14b8a6', // teal
  '#f97316', // orange
  '#a855f7', // purple
  '#3b82f6', // blue
  '#84cc16', // lime
  '#eab308', // yellow
  '#e11d48', // rose
  '#6366f1', // indigo
  '#0ea5e9', // sky
]

function getCoreColor(index: number): string {
  return CORE_COLORS[index % CORE_COLORS.length]
}

export default function CpuCoresHistoryChart({
  history,
  period,
  coreCount,
  isLoading = false,
  className = ''
}: CpuCoresHistoryChartProps) {
  const { t } = useTranslation()
  const [isExpanded, setIsExpanded] = useState(false)
  
  // Build series for MultiLineChart
  const series = useMemo(() => {
    if (coreCount === 0) return []
    
    return Array.from({ length: coreCount }, (_, coreIndex) => ({
      name: `${t('cpu_chart.core')} ${coreIndex}`,
      data: history
        .filter(h => h.per_cpu_percent && h.per_cpu_percent.length > coreIndex)
        .map(h => ({
          timestamp: h.timestamp,
          value: h.per_cpu_percent![coreIndex]
        })),
      color: getCoreColor(coreIndex)
    }))
  }, [history, coreCount, t])
  
  // Don't show if no cores
  if (!coreCount || coreCount === 0) {
    return null
  }
  
  return (
    <div className={className}>
      <motion.button
        onClick={() => setIsExpanded(!isExpanded)}
        className="w-full flex items-center justify-between p-3 bg-dark-800/50 hover:bg-dark-800 rounded-xl transition-colors group"
        whileHover={{ scale: 1.005 }}
        whileTap={{ scale: 0.995 }}
      >
        <div className="flex items-center gap-2 text-dark-300">
          <Cpu className="w-4 h-4 text-accent-500" />
          <span className="text-sm font-medium">
            {t('cpu_chart.cores_history')}
          </span>
          <span className="text-xs text-dark-500">
            ({coreCount} {t('cpu_chart.cores')})
          </span>
        </div>
        <motion.div
          animate={{ rotate: isExpanded ? 180 : 0 }}
          transition={{ duration: 0.2 }}
        >
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
              <MultiLineChart
                series={series}
                height={300}
                unit="%"
                period={period}
                smoothing={0.2}
              />
              
              {/* Loading overlay */}
              <AnimatePresence>
                {isLoading && (
                  <motion.div 
                    className="absolute inset-0 flex items-center justify-center bg-dark-900/50 backdrop-blur-sm rounded-xl"
                    initial={{ opacity: 0 }}
                    animate={{ opacity: 1 }}
                    exit={{ opacity: 0 }}
                  >
                    <div className="relative">
                      <motion.div
                        className="w-8 h-8 border-2 border-accent-500/30 rounded-full"
                        animate={{ rotate: 360 }}
                        transition={{ duration: 1.5, repeat: Infinity, ease: 'linear' }}
                      />
                      <motion.div
                        className="absolute inset-0 w-8 h-8 border-2 border-transparent border-t-accent-500 rounded-full"
                        animate={{ rotate: 360 }}
                        transition={{ duration: 1, repeat: Infinity, ease: 'linear' }}
                      />
                    </div>
                  </motion.div>
                )}
              </AnimatePresence>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}
