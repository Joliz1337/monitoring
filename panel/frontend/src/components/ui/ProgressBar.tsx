interface ProgressBarProps {
  value: number
  max?: number
  size?: 'sm' | 'md' | 'lg'
  color?: 'default' | 'success' | 'warning' | 'danger' | 'accent'
  showLabel?: boolean
  label?: string
  className?: string
  animated?: boolean
}

const sizeClasses = {
  sm: 'h-1.5',
  md: 'h-2',
  lg: 'h-3',
}

function getColorByValue(value: number): { bg: string; glow: string } {
  if (value < 60) return {
    bg: 'bg-gradient-to-r from-success to-emerald-400',
    glow: 'shadow-success/30'
  }
  if (value < 85) return {
    bg: 'bg-gradient-to-r from-warning to-amber-400',
    glow: 'shadow-warning/30'
  }
  return {
    bg: 'bg-gradient-to-r from-danger to-red-400',
    glow: 'shadow-danger/30'
  }
}

const colorMap: Record<string, { bg: string; glow: string }> = {
  default: { bg: 'bg-dark-500', glow: '' },
  success: { bg: 'bg-gradient-to-r from-success to-emerald-400', glow: 'shadow-success/30' },
  warning: { bg: 'bg-gradient-to-r from-warning to-amber-400', glow: 'shadow-warning/30' },
  danger: { bg: 'bg-gradient-to-r from-danger to-red-400', glow: 'shadow-danger/30' },
  accent: { bg: 'bg-gradient-to-r from-accent-500 to-accent-400', glow: 'shadow-accent-500/30' },
}

export default function ProgressBar({
  value,
  max = 100,
  size = 'md',
  color,
  showLabel = false,
  label,
  className = '',
  animated = false,
}: ProgressBarProps) {
  const percent = Math.min(100, Math.max(0, (value / max) * 100))
  const colorStyles = color ? colorMap[color] : getColorByValue(percent)

  const fillClasses = [
    'pb-fill',
    colorStyles.bg,
    animated ? `shadow-lg ${colorStyles.glow}` : '',
    animated && percent > 0 ? 'pb-fill-shimmer' : '',
    animated && percent >= 80 ? 'pb-fill-pulse' : '',
  ].filter(Boolean).join(' ')

  return (
    <div className={className}>
      {(showLabel || label) && (
        <div className="flex justify-between items-center mb-1.5">
          <span className="text-xs text-dark-400">{label}</span>
          {showLabel && (
            <span className="text-xs font-mono text-dark-300">
              {percent.toFixed(1)}%
            </span>
          )}
        </div>
      )}
      <div className={`pb-track bg-dark-800/60 ${sizeClasses[size]}`}>
        <div className={fillClasses} style={{ width: `${percent}%` }} />
      </div>
    </div>
  )
}
