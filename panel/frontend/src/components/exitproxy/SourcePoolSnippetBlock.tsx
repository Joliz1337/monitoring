import { useEffect, useState } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { ChevronDown, Loader2, Shuffle } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { sourcePoolApi, type SourcePoolSnippet } from '../../api/client'
import { CopyField } from './SnippetBlock'

// Кусок конфига Xray для пула исходящих адресов: метки одинаковы на всех нодах,
// поэтому блок общий и живёт рядом с конфигом exit-прокси
export default function SourcePoolSnippetBlock() {
  const { t } = useTranslation()
  const [open, setOpen] = useState(false)
  const [snippet, setSnippet] = useState<SourcePoolSnippet | null>(null)
  const [loading, setLoading] = useState(false)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    if (!open || snippet || loading) return
    setLoading(true)
    sourcePoolApi.getSnippet()
      .then(res => setSnippet(res.data))
      .catch(() => setFailed(true))
      .finally(() => setLoading(false))
  }, [open, snippet, loading])

  return (
    <div className="rounded-lg border border-dark-700/50 bg-dark-900/40">
      <button type="button" onClick={() => setOpen(o => !o)} className="w-full flex items-center gap-2 px-3 py-2.5 text-left">
        <Shuffle className="w-4 h-4 text-dark-400" />
        <span className="text-xs font-medium text-dark-200">{t('source_pool.snippet_title')}</span>
        <ChevronDown className={`w-4 h-4 text-dark-500 ml-auto transition-transform ${open ? 'rotate-180' : ''}`} />
      </button>

      <AnimatePresence>
        {open && (
          <motion.div
            className="overflow-hidden"
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.15 }}
          >
            <div className="px-3 pb-3 space-y-3">
              <p className="text-xs text-dark-400">{t('source_pool.snippet_hint')}</p>
              {loading ? (
                <div className="flex items-center gap-2 text-dark-400 text-xs">
                  <Loader2 className="w-3.5 h-3.5 animate-spin" />
                  {t('common.loading')}
                </div>
              ) : snippet ? (
                <>
                  <CopyField label={t('source_pool.snippet_outbounds')} value={snippet.outbounds_json} rows={12} />
                  <CopyField label={t('source_pool.snippet_routing')} value={snippet.routing_json} rows={12} />
                  <CopyField label={t('source_pool.snippet_text')} value={snippet.text} rows={10} />
                </>
              ) : failed ? (
                <p className="text-xs text-danger">{t('source_pool.snippet_failed')}</p>
              ) : null}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}
