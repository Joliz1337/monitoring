import { useEffect, useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { ShieldAlert, Loader2, Check, Copy, X } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import { serversApi } from '../api/client'

interface Props {
  onMigrated: () => void
}

interface ManualResult {
  manual: { id: number; name: string }[]
  token: string
}

export default function MigrationBanner({ onMigrated }: Props) {
  const { t } = useTranslation()
  const [needs, setNeeds] = useState<number | null>(null)
  const [running, setRunning] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const [manualResult, setManualResult] = useState<ManualResult | null>(null)
  const [tokenCopied, setTokenCopied] = useState(false)

  const refresh = async () => {
    try {
      const { data } = await serversApi.migrationStatus()
      setNeeds(data.needs_migration)
    } catch {
      setNeeds(0)
    }
  }

  useEffect(() => {
    refresh()
  }, [])

  if (needs === null || needs === 0) return null

  const handleMigrate = async () => {
    setRunning(true)
    try {
      const { data } = await serversApi.migrateAll()
      const auto = data.auto_migrated.length
      const failed = data.failed.length
      const manual = data.manual_required

      if (auto > 0) {
        toast.success(t('servers.migration_auto_done', { count: auto }))
      }
      if (failed > 0) {
        toast.error(t('servers.migration_failed_count', { count: failed }))
      }

      if (manual.length > 0 && data.token) {
        setManualResult({ manual, token: data.token })
      }

      onMigrated()
      await refresh()
    } catch {
      toast.error(t('servers.migration_failed'))
    } finally {
      setRunning(false)
    }
  }

  const handleConfirmManual = async () => {
    if (!manualResult) return
    setConfirming(true)
    const results = await Promise.allSettled(
      manualResult.manual.map(s => serversApi.confirmMigration(s.id))
    )
    const ok = results.filter(r => r.status === 'fulfilled').length
    const fail = manualResult.manual.length - ok
    if (ok > 0) toast.success(t('servers.migration_auto_done', { count: ok }))
    if (fail > 0) toast.error(t('servers.migration_confirm_failed', { count: fail }))
    setConfirming(false)
    setManualResult(null)
    onMigrated()
    await refresh()
  }

  const handleCopyToken = async () => {
    if (!manualResult) return
    try {
      await navigator.clipboard.writeText(manualResult.token)
    } catch {
      const ta = document.createElement('textarea')
      ta.value = manualResult.token
      document.body.appendChild(ta)
      ta.select()
      document.execCommand('copy')
      document.body.removeChild(ta)
    }
    setTokenCopied(true)
    setTimeout(() => setTokenCopied(false), 2000)
  }

  return (
    <>
      <motion.div
        className="mb-6 p-4 rounded-xl bg-warning/5 border border-warning/30 flex items-center justify-between gap-4"
        initial={{ opacity: 0, y: -10 }}
        animate={{ opacity: 1, y: 0 }}
      >
        <div className="flex items-center gap-3 min-w-0">
          <ShieldAlert className="w-5 h-5 text-warning flex-shrink-0" />
          <div className="min-w-0">
            <h3 className="text-sm font-semibold text-dark-100">
              {t('servers.migration_banner_title', { count: needs })}
            </h3>
            <p className="text-xs text-dark-400 truncate">
              {t('servers.migration_banner_subtitle')}
            </p>
          </div>
        </div>
        <motion.button
          onClick={handleMigrate}
          disabled={running}
          className="btn btn-primary text-sm flex-shrink-0"
          whileHover={{ scale: running ? 1 : 1.02 }}
          whileTap={{ scale: running ? 1 : 0.98 }}
        >
          {running ? (
            <>
              <Loader2 className="w-4 h-4 animate-spin" />
              {t('servers.migration_in_progress')}
            </>
          ) : (
            t('servers.migration_button')
          )}
        </motion.button>
      </motion.div>

      <AnimatePresence>
        {manualResult && (
          <motion.div
            className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm p-4"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            onClick={() => setManualResult(null)}
          >
            <motion.div
              className="card max-w-xl w-full"
              initial={{ scale: 0.95, opacity: 0 }}
              animate={{ scale: 1, opacity: 1 }}
              exit={{ scale: 0.95, opacity: 0 }}
              onClick={(e) => e.stopPropagation()}
            >
              <div className="flex items-center justify-between mb-4">
                <h2 className="text-lg font-semibold text-dark-100 flex items-center gap-2">
                  <ShieldAlert className="w-5 h-5 text-warning" />
                  {t('servers.migration_manual_title')}
                </h2>
                <button
                  onClick={() => setManualResult(null)}
                  className="p-2 hover:bg-dark-700 rounded-xl text-dark-400 transition-colors"
                >
                  <X className="w-5 h-5" />
                </button>
              </div>
              <p className="text-sm text-dark-300 mb-3">{t('servers.migration_manual_hint')}</p>
              <ul className="text-sm text-dark-200 mb-4 list-disc list-inside">
                {manualResult.manual.map(s => (
                  <li key={s.id}>{s.name}</li>
                ))}
              </ul>
              <textarea
                readOnly
                value={manualResult.token}
                className="input font-mono text-xs break-all resize-none w-full min-h-[88px] mb-3"
                onClick={(e) => (e.target as HTMLTextAreaElement).select()}
              />
              <div className="flex flex-wrap gap-2">
                <motion.button
                  onClick={handleCopyToken}
                  className={`btn text-sm ${tokenCopied ? 'bg-success/20 text-success border-success/30' : 'btn-secondary'}`}
                  whileHover={{ scale: 1.02 }}
                  whileTap={{ scale: 0.98 }}
                >
                  {tokenCopied ? (
                    <><Check className="w-4 h-4" />{t('servers.copied')}</>
                  ) : (
                    <><Copy className="w-4 h-4" />{t('servers.copy_installer_token')}</>
                  )}
                </motion.button>
                <motion.button
                  onClick={handleConfirmManual}
                  disabled={confirming}
                  className="btn btn-primary text-sm"
                  whileHover={{ scale: confirming ? 1 : 1.02 }}
                  whileTap={{ scale: confirming ? 1 : 0.98 }}
                >
                  {confirming ? (
                    <><Loader2 className="w-4 h-4 animate-spin" />{t('servers.migration_in_progress')}</>
                  ) : (
                    t('servers.migration_confirm_done')
                  )}
                </motion.button>
              </div>
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>
    </>
  )
}
