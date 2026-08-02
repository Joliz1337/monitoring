import { useEffect, useMemo } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { X, HelpCircle, BookOpen } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { useFAQStore } from '../../stores/faqStore'
import { getFAQContent } from '../../data/faq/registry'
import type { FAQLang } from './faq.types'
import { Markdown } from './markdown'

const resolveLang = (raw: string | undefined): FAQLang => {
  const code = (raw ?? 'ru').split('-')[0].toLowerCase()
  return code === 'en' ? 'en' : 'ru'
}

export default function FAQDrawer() {
  const { t, i18n } = useTranslation()
  const { isOpen, screen, close } = useFAQStore()

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && isOpen) close()
    }
    document.addEventListener('keydown', handler)
    return () => document.removeEventListener('keydown', handler)
  }, [isOpen, close])

  const lang = resolveLang(i18n.language)

  const content = useMemo(() => {
    if (!screen) return null
    return getFAQContent(screen, lang)
  }, [screen, lang])

  return (
    <AnimatePresence>
      {isOpen && (
        <>
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.2 }}
            className="fixed inset-0 bg-black/50 backdrop-blur-sm z-[65]"
            onClick={close}
          />

          <motion.div
            initial={{ x: '100%' }}
            animate={{ x: 0 }}
            exit={{ x: '100%' }}
            transition={{ type: 'spring', damping: 26, stiffness: 280 }}
            className="fixed right-0 top-0 bottom-0 z-[65] w-full max-w-2xl
                       bg-dark-900/95 backdrop-blur-xl border-l border-dark-800/60
                       flex flex-col shadow-2xl"
          >
            <div className="flex items-center justify-between px-6 py-4 border-b border-dark-800/60">
              <div className="flex items-center gap-3">
                <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-amber-500/25 to-orange-500/20
                                flex items-center justify-center border border-amber-500/30
                                shadow-lg shadow-amber-500/10">
                  <HelpCircle className="w-5 h-5 text-amber-300" />
                </div>
                <div>
                  <h2 className="text-lg font-semibold text-dark-50">{t('faq.title')}</h2>
                  {screen && (
                    <p className="text-xs text-dark-500 font-mono">{screen}</p>
                  )}
                </div>
              </div>
              <motion.button
                onClick={close}
                className="p-2 rounded-lg hover:bg-dark-800 text-dark-400 hover:text-dark-100 transition-colors"
                whileHover={{ scale: 1.08 }}
                whileTap={{ scale: 0.92 }}
                aria-label={t('faq.close')}
              >
                <X className="w-5 h-5" />
              </motion.button>
            </div>

            <div className="flex-1 overflow-y-auto px-6 py-6">
              {content ? (
                <Markdown source={content} />
              ) : (
                <div className="flex flex-col items-center justify-center h-full text-dark-500 gap-3">
                  <BookOpen className="w-10 h-10 opacity-40" />
                  <p className="text-sm">{t('faq.not_found')}</p>
                </div>
              )}
            </div>
          </motion.div>
        </>
      )}
    </AnimatePresence>
  )
}
