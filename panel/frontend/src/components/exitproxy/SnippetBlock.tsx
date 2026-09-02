import { useEffect, useState } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { Check, ChevronDown, Copy, Loader2, Terminal } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { useExitProxyStore } from '../../stores/exitProxyStore'

async function copyText(value: string) {
  try {
    await navigator.clipboard.writeText(value)
  } catch {
    const ta = document.createElement('textarea')
    ta.value = value
    document.body.appendChild(ta)
    ta.select()
    document.execCommand('copy')
    document.body.removeChild(ta)
  }
}

function CopyField({ label, value, rows }: { label: string; value: string; rows: number }) {
  const { t } = useTranslation()
  const [copied, setCopied] = useState(false)

  const handleCopy = async () => {
    await copyText(value)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between">
        <span className="text-xs font-medium text-dark-300">{label}</span>
        <button
          type="button"
          onClick={handleCopy}
          className={`btn text-xs py-1 ${copied ? 'bg-success/20 text-success border-success/30' : 'btn-secondary'}`}
        >
          {copied ? <><Check className="w-3.5 h-3.5" />{t('exit_proxy.copied')}</> : <><Copy className="w-3.5 h-3.5" />{t('exit_proxy.copy')}</>}
        </button>
      </div>
      <textarea
        readOnly
        value={value}
        rows={rows}
        className="input font-mono text-[11px] resize-none w-full"
        onClick={(e) => (e.target as HTMLTextAreaElement).select()}
      />
    </div>
  )
}

// Кусок конфига Xray для Remnawave: порт один на всех нодах, поэтому блок общий
export default function SnippetBlock() {
  const { t } = useTranslation()
  const [open, setOpen] = useState(false)
  const snippet = useExitProxyStore(s => s.snippet)
  const fetchSnippet = useExitProxyStore(s => s.fetchSnippet)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    if (!open || snippet) return
    setLoading(true)
    fetchSnippet().finally(() => setLoading(false))
  }, [open, snippet, fetchSnippet])

  return (
    <div className="rounded-lg border border-dark-700/50 bg-dark-900/40">
      <button type="button" onClick={() => setOpen(o => !o)} className="w-full flex items-center gap-2 px-3 py-2.5 text-left">
        <Terminal className="w-4 h-4 text-dark-400" />
        <span className="text-xs font-medium text-dark-200">{t('exit_proxy.snippet_title')}</span>
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
              <p className="text-xs text-dark-400">{t('exit_proxy.snippet_hint')}</p>
              {loading && !snippet ? (
                <div className="flex items-center gap-2 text-dark-400 text-xs">
                  <Loader2 className="w-3.5 h-3.5 animate-spin" />
                  {t('common.loading')}
                </div>
              ) : snippet ? (
                <>
                  <CopyField label={t('exit_proxy.snippet_outbound')} value={snippet.outbound_json} rows={9} />
                  <CopyField label={t('exit_proxy.snippet_rules')} value={snippet.rules_json} rows={12} />
                  <CopyField label={t('exit_proxy.snippet_text')} value={snippet.text} rows={8} />
                </>
              ) : (
                <p className="text-xs text-danger">{t('exit_proxy.snippet_failed')}</p>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}
