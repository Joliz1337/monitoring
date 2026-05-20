import { motion } from 'framer-motion'
import { HelpCircle } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Tooltip } from '../ui/Tooltip'
import { useFAQStore } from '../../stores/faqStore'
import type { FAQScreen } from './faq.types'

interface FAQIconProps {
  screen: FAQScreen
  size?: 'sm' | 'md'
  className?: string
}

export default function FAQIcon({ screen, size = 'md', className = '' }: FAQIconProps) {
  const { t } = useTranslation()
  const open = useFAQStore((s) => s.open)

  const sizeClasses = size === 'sm'
    ? 'p-1 [&_svg]:w-3.5 [&_svg]:h-3.5'
    : 'p-1.5 [&_svg]:w-4 [&_svg]:h-4'

  return (
    <Tooltip label={t('faq.help_article')} position="top">
      <motion.button
        type="button"
        onClick={() => open(screen)}
        className={`${sizeClasses} rounded-lg text-amber-300/70 hover:text-amber-200
                    hover:bg-amber-500/10 border border-transparent hover:border-amber-500/20
                    transition-colors ${className}`}
        whileHover={{ scale: 1.08 }}
        whileTap={{ scale: 0.92 }}
        aria-label={t('faq.help_article')}
      >
        <HelpCircle />
      </motion.button>
    </Tooltip>
  )
}
