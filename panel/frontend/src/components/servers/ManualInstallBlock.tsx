import { useState } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { ChevronDown, Check, Copy, Hourglass, Loader2, Terminal } from 'lucide-react'
import { useTranslation } from 'react-i18next'

interface Props {
  open: boolean
  onToggle: () => void
  command: string | null
  loading: boolean
  onWait: () => void
  disabled: boolean
}

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

// Полуавтоматическая установка: команду со всеми выбранными опциями оператор
// запускает на сервере сам, панель ждёт ноду и доделывает остальное через её API
export default function ManualInstallBlock({ open, onToggle, command, loading, onWait, disabled }: Props) {
  const { t } = useTranslation()
  const [copied, setCopied] = useState(false)

  const handleCopy = async () => {
    if (!command) return
    await copyText(command)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  return (
    <div className="rounded-lg border border-dark-700/50 bg-dark-900/40 mt-2">
      <button
        type="button"
        onClick={onToggle}
        className="w-full flex items-center gap-2 px-3 py-2.5 text-left"
      >
        <Terminal className="w-4 h-4 text-dark-400" />
        <span className="text-xs font-medium text-dark-200">{t('servers.deploy_manual_title')}</span>
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
              <p className="text-xs text-dark-400">{t('servers.deploy_manual_hint')}</p>

              {loading && !command ? (
                <div className="flex items-center gap-2 text-dark-400 text-xs">
                  <Loader2 className="w-3.5 h-3.5 animate-spin" />
                  {t('common.loading')}
                </div>
              ) : command ? (
                <textarea
                  readOnly
                  value={command}
                  className="input font-mono text-[11px] break-all resize-none w-full min-h-[88px]"
                  onClick={(e) => (e.target as HTMLTextAreaElement).select()}
                />
              ) : (
                <p className="text-xs text-danger">{t('servers.deploy_manual_command_failed')}</p>
              )}

              <p className="text-xs text-dark-400">{t('servers.deploy_manual_wait_hint')}</p>

              <div className="flex flex-wrap gap-2">
                <button
                  type="button"
                  onClick={handleCopy}
                  disabled={!command}
                  className={`btn text-xs ${copied ? 'bg-success/20 text-success border-success/30' : 'btn-secondary'}`}
                >
                  {copied
                    ? <><Check className="w-3.5 h-3.5" />{t('servers.copied')}</>
                    : <><Copy className="w-3.5 h-3.5" />{t('servers.deploy_manual_copy')}</>}
                </button>
                <button
                  type="button"
                  onClick={onWait}
                  disabled={disabled}
                  className="btn btn-primary text-xs"
                >
                  <Hourglass className="w-3.5 h-3.5" />
                  {t('servers.deploy_manual_wait_btn')}
                </button>
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}
