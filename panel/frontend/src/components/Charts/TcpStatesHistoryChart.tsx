import { useState, useMemo } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { useTranslation } from 'react-i18next'
import { ChevronDown, Network } from 'lucide-react'
import MultiLineChart from './MultiLineChart'

interface HistoryDataPoint {
  timestamp: string
  tcp_established?: number | null
  tcp_listen?: number | null
  tcp_time_wait?: number | null
  tcp_close_wait?: number | null
  tcp_syn_sent?: number | null
  tcp_syn_recv?: number | null
  tcp_fin_wait?: number | null
}

interface TcpStatesHistoryChartProps {
  history: HistoryDataPoint[]
  period: string
  isLoading?: boolean
  className?: string
}

const TCP_STATE_CONFIG = [
  { key: 'tcp_established', color: '#10b981' },  // green
  { key: 'tcp_listen', color: '#22d3ee' },        // cyan
  { key: 'tcp_time_wait', color: '#f59e0b' },     // amber
  { key: 'tcp_close_wait', color: '#ef4444' },     // red
  { key: 'tcp_syn_sent', color: '#8b5cf6' },       // violet
  { key: 'tcp_syn_recv', color: '#ec4899' },       // pink
  { key: 'tcp_fin_wait', color: '#f97316' },       // orange
] as const

export default function TcpStatesHistoryChart({
  history,
  period,
  isLoading = false,
  className = ''
}: TcpStatesHistoryChartProps) {
  const { t } = useTranslation()
  const [isExpanded, setIsExpanded] = useState(false)

  const hasTcpData = useMemo(() => {
    return history.some(h => h.tcp_established != null || h.tcp_listen != null)
  }, [history])

  const series = useMemo(() => {
    if (!hasTcpData) return []

    return TCP_STATE_CONFIG.map(({ key, color }) => {
      const k = key as keyof HistoryDataPoint
      return {
        name: t(`tcp_chart.${key}`),
        data: history
          .filter(h => h[k] != null)
          .map(h => ({
            timestamp: h.timestamp,
            value: (h[k] as number) || 0
          })),
        color
      }
    }).filter(s => s.data.length > 0 && s.data.some(d => d.value > 0))
  }, [history, hasTcpData, t])

  if (!hasTcpData || series.length === 0) return null

  return (
    <div className={className}>
      <motion.button
        onClick={() => setIsExpanded(!isExpanded)}
        className="w-full flex items-center justify-between p-3 bg-dark-800/50 hover:bg-dark-800 rounded-xl transition-colors group"
        whileHover={{ scale: 1.005 }}
        whileTap={{ scale: 0.995 }}
      >
        <div className="flex items-center gap-2 text-dark-300">
          <Network className="w-4 h-4 text-accent-500" />
          <span className="text-sm font-medium">
            {t('tcp_chart.states_history')}
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
                period={period}
                smoothing={0.2}
              />

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
