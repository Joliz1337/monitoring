import { motion } from 'framer-motion'

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
  
  return (
    <div className={className}>
      {(showLabel || label) && (
        <div className="flex justify-between items-center mb-1.5">
          <span className="text-xs text-dark-400">{label}</span>
          {showLabel && (
            <motion.span 
              className="text-xs font-mono text-dark-300"
              key={percent.toFixed(1)}
              initial={{ opacity: 0, y: -5 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.2 }}
            >
              {percent.toFixed(1)}%
            </motion.span>
          )}
        </div>
      )}
      <div className={`
        w-full bg-dark-800/60 rounded-full overflow-hidden 
        ${sizeClasses[size]}
        backdrop-blur-sm
      `}>
        <motion.div
          className={`
            h-full rounded-full relative overflow-hidden
            ${colorStyles.bg}
            ${animated ? `shadow-lg ${colorStyles.glow}` : ''}
          `}
          initial={{ width: 0 }}
          animate={{ width: `${percent}%` }}
          transition={{ 
            duration: 0.6, 
            ease: [0.4, 0, 0.2, 1],
            delay: animated ? 0.1 : 0
          }}
        >
          {/* Shimmer effect */}
          {animated && percent > 0 && (
            <motion.div
              className="absolute inset-0 bg-gradient-to-r from-transparent via-white/20 to-transparent"
              animate={{
                x: ['-100%', '200%'],
              }}
              transition={{
                duration: 2,
                repeat: Infinity,
                ease: 'linear',
              }}
            />
          )}
          
          {/* Pulse effect for high values */}
          {percent >= 80 && animated && (
            <motion.div
              className="absolute inset-0 bg-white/10"
              animate={{
                opacity: [0, 0.3, 0],
              }}
              transition={{
                duration: 1.5,
                repeat: Infinity,
                ease: 'easeInOut',
              }}
            />
          )}
        </motion.div>
      </div>
    </div>
  )
}
