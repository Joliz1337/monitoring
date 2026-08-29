import { motion, AnimatePresence } from 'framer-motion'

interface ChartLoadingOverlayProps {
  visible: boolean
  /** Скругление подложки — под карточку (rounded-2xl) или блок аккордеона (rounded-xl) */
  className?: string
}

/** Полупрозрачная заглушка со спиннером поверх графика; родитель должен быть position: relative */
export default function ChartLoadingOverlay({ visible, className = 'rounded-xl' }: ChartLoadingOverlayProps) {
  return (
    <AnimatePresence>
      {visible && (
        <motion.div
          className={`absolute inset-0 flex items-center justify-center bg-dark-900/50 backdrop-blur-sm ${className}`}
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
  )
}
