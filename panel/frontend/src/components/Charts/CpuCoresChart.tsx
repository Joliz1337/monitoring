import { useMemo } from 'react'
import { useTranslation } from 'react-i18next'

interface CpuCoresChartProps {
  perCpuPercent: number[]
  className?: string
}

function getColorForPercent(percent: number): string {
  if (percent >= 80) return 'bg-danger'
  if (percent >= 50) return 'bg-warning'
  return 'bg-success'
}

function getTextColorForPercent(percent: number): string {
  if (percent >= 80) return 'text-danger'
  if (percent >= 50) return 'text-warning'
  return 'text-success'
}

export default function CpuCoresChart({ perCpuPercent, className = '' }: CpuCoresChartProps) {
  const { t } = useTranslation()
  
  const cores = useMemo(() => {
    return perCpuPercent.map((percent, index) => ({
      id: index,
      percent: Math.round(percent * 10) / 10,
    }))
  }, [perCpuPercent])
  
  // Calculate average
  const avgPercent = useMemo(() => {
    if (cores.length === 0) return 0
    return Math.round(cores.reduce((sum, c) => sum + c.percent, 0) / cores.length * 10) / 10
  }, [cores])
  
  // Group cores into rows for better display with many cores
  const coresPerRow = cores.length > 16 ? 16 : cores.length > 8 ? 8 : cores.length
  
  return (
    <div className={className}>
      {/* Header with average */}
      <div className="flex items-center justify-between mb-3">
        <span className="text-sm text-dark-400">{t('cpu_chart.per_core_usage')}</span>
        <span className={`text-sm font-mono ${getTextColorForPercent(avgPercent)}`}>
          {t('cpu_chart.avg')}: {avgPercent.toFixed(1)}%
        </span>
      </div>
      
      {/* Cores grid */}
      <div 
        className="grid gap-1.5"
        style={{ 
          gridTemplateColumns: `repeat(${Math.min(coresPerRow, cores.length)}, 1fr)` 
        }}
      >
        {cores.map((core) => (
          <div
            key={core.id}
            className="relative group"
            title={`${t('cpu_chart.core')} ${core.id}: ${core.percent}%`}
          >
            {/* Bar background */}
            <div className="h-8 bg-dark-800 rounded overflow-hidden">
              {/* Filled portion */}
              <div
                className={`h-full transition-all duration-300 ${getColorForPercent(core.percent)}`}
                style={{ 
                  width: `${core.percent}%`,
                  opacity: 0.8
                }}
              />
            </div>
            
            {/* Core percent label */}
            <div className="absolute inset-0 flex items-center justify-center overflow-hidden">
              <span 
                className={`font-mono ${getTextColorForPercent(core.percent)} opacity-80 group-hover:opacity-100 transition-opacity whitespace-nowrap`}
                style={{ fontSize: cores.length > 16 ? '9px' : cores.length > 8 ? '10px' : '12px' }}
              >
                {core.percent.toFixed(0)}
              </span>
            </div>
            
            {/* Hover tooltip */}
            <div className="absolute -top-8 left-1/2 -translate-x-1/2 px-2 py-1 bg-dark-900 rounded text-xs font-mono text-dark-200 opacity-0 group-hover:opacity-100 transition-opacity whitespace-nowrap pointer-events-none z-10 shadow-lg border border-dark-700">
              {t('cpu_chart.core')} {core.id}: {core.percent}%
            </div>
          </div>
        ))}
      </div>
      
      {/* Legend */}
      <div className="flex items-center justify-center gap-4 mt-3 text-xs text-dark-500">
        <div className="flex items-center gap-1.5">
          <div className="w-3 h-3 rounded bg-success opacity-80" />
          <span>0-50%</span>
        </div>
        <div className="flex items-center gap-1.5">
          <div className="w-3 h-3 rounded bg-warning opacity-80" />
          <span>50-80%</span>
        </div>
        <div className="flex items-center gap-1.5">
          <div className="w-3 h-3 rounded bg-danger opacity-80" />
          <span>80%+</span>
        </div>
      </div>
    </div>
  )
}
