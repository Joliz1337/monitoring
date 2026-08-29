import { useCallback, useEffect, useRef, useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { Shield, RefreshCw, Loader2, AlertTriangle, CheckCircle2, XCircle, Terminal } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import { systemApi, type PanelCertificateInfo } from '../../api/client'
import { Tooltip } from '../ui/Tooltip'
import { SettingsSection } from './SettingsSection'

interface RenewalResult {
  success: boolean
  message: string
  output?: string | null
  completedAt?: string | null
}

type RenewalPhase = 'idle' | 'starting' | 'running' | 'nginx_restarting' | 'done'

const POLL_INTERVAL_MS = 2000
// nginx перезапускается ~30-60 с; даём до ~2 минут ошибок соединения, прежде чем сдаться
const MAX_CONNECTION_ERRORS = 60
const HARD_TIMEOUT_MS = 5 * 60 * 1000

export function PanelCertificateCard() {
  const { t } = useTranslation()
  const [certInfo, setCertInfo] = useState<PanelCertificateInfo | null>(null)
  const [loading, setLoading] = useState(true)
  const [renewing, setRenewing] = useState(false)
  const [result, setResult] = useState<RenewalResult | null>(null)
  const [showOutput, setShowOutput] = useState(false)
  const [phase, setPhase] = useState<RenewalPhase>('idle')
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const hardTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const renewingRef = useRef(false)
  const connectionErrorsRef = useRef(0)

  const fetchCertInfo = useCallback(async () => {
    try {
      const response = await systemApi.getCertificate()
      setCertInfo(response.data)
    } catch (err) {
      console.error('Failed to fetch certificate info:', err)
    } finally {
      setLoading(false)
    }
  }, [])

  const stopPolling = useCallback(() => {
    if (pollRef.current) clearInterval(pollRef.current)
    if (hardTimeoutRef.current) clearTimeout(hardTimeoutRef.current)
    pollRef.current = null
    hardTimeoutRef.current = null
  }, [])

  const finishRenewal = useCallback((res: RenewalResult) => {
    stopPolling()
    renewingRef.current = false
    setPhase('done')
    setRenewing(false)
    setResult(res)
    if (!res.success && res.output) setShowOutput(true)
    if (res.success) {
      toast.success(t('settings.ssl_renew_success'))
    } else {
      toast.error(res.message)
    }
  }, [stopPolling, t])

  const startPolling = useCallback(() => {
    stopPolling()
    renewingRef.current = true
    connectionErrorsRef.current = 0
    setRenewing(true)
    setResult(null)
    setShowOutput(false)

    const poll = async () => {
      if (!renewingRef.current) return
      try {
        const { data: status } = await systemApi.getCertRenewalStatus()
        if (connectionErrorsRef.current > 0) {
          connectionErrorsRef.current = 0
          setPhase('running')
        }
        if (status.in_progress) return

        if (status.last_result === 'success') {
          fetchCertInfo()
          finishRenewal({ success: true, message: t('settings.ssl_renew_success'), output: status.output, completedAt: status.completed_at })
        } else if (status.last_result === 'not_due') {
          finishRenewal({ success: false, message: t('settings.ssl_not_due'), output: status.output, completedAt: status.completed_at })
        } else {
          finishRenewal({ success: false, message: status.last_error || t('settings.ssl_renew_error'), output: status.output, completedAt: status.completed_at })
        }
      } catch {
        // Ошибка соединения — скорее всего перезапускается nginx
        connectionErrorsRef.current++
        setPhase('nginx_restarting')
        if (connectionErrorsRef.current >= MAX_CONNECTION_ERRORS) {
          finishRenewal({ success: false, message: t('settings.ssl_connection_timeout') })
        }
      }
    }

    pollRef.current = setInterval(poll, POLL_INTERVAL_MS)
    setTimeout(poll, 500)
    hardTimeoutRef.current = setTimeout(() => {
      if (renewingRef.current) finishRenewal({ success: false, message: t('settings.ssl_timeout') })
    }, HARD_TIMEOUT_MS)
  }, [stopPolling, fetchCertInfo, finishRenewal, t])

  const handleRenew = async () => {
    if (renewingRef.current) return
    renewingRef.current = true
    setRenewing(true)
    setResult(null)
    setPhase('starting')
    try {
      await systemApi.renewCertificate()
      setPhase('running')
      startPolling()
    } catch (err: any) {
      finishRenewal({ success: false, message: err.response?.data?.detail || t('settings.ssl_renew_error') })
    }
  }

  useEffect(() => {
    fetchCertInfo()
    // Продление, запущенное до размонтирования карточки (смена вкладки), подхватывается заново
    systemApi.getCertRenewalStatus()
      .then(({ data }) => {
        if (data.in_progress) {
          setPhase('running')
          startPolling()
        }
      })
      .catch(() => {})
    return () => {
      stopPolling()
      renewingRef.current = false
    }
  }, [fetchCertInfo, startPolling, stopPolling])

  const daysBadgeClass = certInfo?.days_left !== undefined && certInfo.days_left <= 7
    ? 'bg-danger/20 text-danger'
    : certInfo?.days_left !== undefined && certInfo.days_left <= 30
      ? 'bg-warning/20 text-warning'
      : 'bg-success/20 text-success'

  return (
    <SettingsSection icon={Shield} title={t('settings.ssl_certificate')} description={t('settings.ssl_certificate_desc')}>
      {loading ? (
        <div className="flex items-center justify-center py-4">
          <Loader2 className="w-5 h-5 text-accent-500 animate-spin" />
        </div>
      ) : certInfo?.error ? (
        <div className="flex items-center gap-3 p-4 bg-dark-800/50 rounded-xl border border-dark-700/50">
          <AlertTriangle className="w-5 h-5 text-warning flex-shrink-0" />
          <div>
            <p className="text-sm text-dark-300">
              {certInfo.error === 'Domain not configured'
                ? t('settings.ssl_not_configured')
                : certInfo.error === 'Certificate not found'
                  ? t('settings.ssl_not_found')
                  : t('settings.ssl_error')}
            </p>
            {certInfo.domain && <p className="text-xs text-dark-500 mt-1">{certInfo.domain}</p>}
          </div>
        </div>
      ) : certInfo ? (
        <div className="space-y-4">
          <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3 p-4 bg-dark-800/50 rounded-xl border border-dark-700/50">
            <div className="space-y-1.5 min-w-0">
              <div className="flex items-center gap-2 text-sm">
                <span className="text-dark-400">{t('settings.ssl_domain')}:</span>
                <span className="text-dark-200 font-mono truncate">{certInfo.domain}</span>
              </div>
              <div className="flex items-center gap-2 text-sm">
                <span className="text-dark-400">{t('settings.ssl_expires')}:</span>
                <span className="text-dark-200">
                  {certInfo.expiry_date && new Date(certInfo.expiry_date).toLocaleDateString()}
                </span>
              </div>
            </div>

            <div className="flex items-center gap-3 shrink-0">
              {certInfo.expired ? (
                <span className="px-3 py-1.5 text-sm font-medium bg-danger/20 text-danger rounded-lg">
                  {t('settings.ssl_expired')}
                </span>
              ) : certInfo.days_left !== undefined && (
                <span className={`px-3 py-1.5 text-sm font-medium rounded-lg ${daysBadgeClass}`}>
                  {t('settings.ssl_days_left', { days: certInfo.days_left })}
                </span>
              )}

              {/* Сертификатом владеет wildcard-модуль: standalone-продление отсюда запрещено бэкендом */}
              {certInfo.managed_by_wildcard ? (
                <Tooltip label={t('settings.ssl_managed_by_wildcard_hint')}>
                  <span className="px-3 py-1.5 text-sm font-medium bg-accent-500/10 text-accent-400 rounded-lg cursor-default">
                    {t('settings.ssl_managed_by_wildcard')}
                  </span>
                </Tooltip>
              ) : (
                <button onClick={handleRenew} disabled={renewing} className="btn btn-secondary text-sm">
                  {renewing ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />}
                  {renewing ? t('settings.ssl_renewing') : t('settings.ssl_renew')}
                </button>
              )}
            </div>
          </div>

          <AnimatePresence mode="wait">
            {renewing && phase === 'starting' && (
              <motion.div
                key="starting"
                className="flex items-center gap-3 p-4 rounded-xl bg-accent-500/10 border border-accent-500/20"
                initial={{ opacity: 0, y: -10 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -10 }}
              >
                <Loader2 className="w-5 h-5 animate-spin text-accent-400" />
                <div className="text-sm font-medium text-accent-400">{t('settings.ssl_starting')}</div>
              </motion.div>
            )}

            {renewing && phase === 'running' && (
              <motion.div
                key="running"
                className="flex items-center gap-3 p-4 rounded-xl bg-accent-500/10 border border-accent-500/20"
                initial={{ opacity: 0, y: -10 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -10 }}
              >
                <Loader2 className="w-5 h-5 animate-spin text-accent-400" />
                <div className="flex-1">
                  <div className="text-sm font-medium text-accent-400">{t('settings.ssl_renewing_status')}</div>
                  <div className="text-xs text-dark-400 mt-1">{t('settings.ssl_renewing_desc')}</div>
                </div>
              </motion.div>
            )}

            {renewing && phase === 'nginx_restarting' && (
              <motion.div
                key="nginx"
                className="flex items-center gap-3 p-4 rounded-xl bg-warning/10 border border-warning/20"
                initial={{ opacity: 0, y: -10 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -10 }}
              >
                <RefreshCw className="w-5 h-5 animate-spin text-warning" />
                <div className="flex-1">
                  <div className="text-sm font-medium text-warning">{t('settings.ssl_nginx_restarting')}</div>
                  <div className="text-xs text-dark-400 mt-1">{t('settings.ssl_nginx_restarting_desc')}</div>
                </div>
              </motion.div>
            )}
          </AnimatePresence>

          <AnimatePresence>
            {result && !renewing && (
              <motion.div
                className="space-y-3"
                initial={{ opacity: 0, y: -10 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -10 }}
              >
                <div className={`flex items-start gap-3 p-4 rounded-xl text-sm ${
                  result.success
                    ? 'bg-success/10 text-success border border-success/20'
                    : 'bg-danger/10 text-danger border border-danger/20'
                }`}>
                  {result.success
                    ? <CheckCircle2 className="w-5 h-5 flex-shrink-0 mt-0.5" />
                    : <XCircle className="w-5 h-5 flex-shrink-0 mt-0.5" />}
                  <div className="flex-1 min-w-0">
                    <div className="font-medium">{result.message}</div>
                    {result.completedAt && (
                      <div className="text-xs opacity-70 mt-1">
                        {t('settings.ssl_completed_at')}: {new Date(result.completedAt).toLocaleString()}
                      </div>
                    )}
                  </div>
                  {result.output && (
                    <button
                      onClick={() => setShowOutput(!showOutput)}
                      className="flex items-center gap-1 text-xs px-2 py-1 rounded-lg bg-dark-800/50 hover:bg-dark-700/50 transition-colors"
                    >
                      <Terminal className="w-3.5 h-3.5" />
                      {showOutput ? t('common.hide') : t('common.show_log')}
                    </button>
                  )}
                </div>

                <AnimatePresence>
                  {showOutput && result.output && (
                    <motion.div
                      initial={{ opacity: 0, height: 0 }}
                      animate={{ opacity: 1, height: 'auto' }}
                      exit={{ opacity: 0, height: 0 }}
                      className="overflow-hidden"
                    >
                      <div className="bg-dark-900 border border-dark-700 rounded-xl p-4">
                        <div className="flex items-center gap-2 text-xs text-dark-400 mb-2">
                          <Terminal className="w-3.5 h-3.5" />
                          {t('settings.ssl_renewal_log')}
                        </div>
                        <pre className="text-xs text-dark-300 whitespace-pre-wrap font-mono max-h-48 overflow-auto">
                          {result.output}
                        </pre>
                      </div>
                    </motion.div>
                  )}
                </AnimatePresence>
              </motion.div>
            )}
          </AnimatePresence>
        </div>
      ) : null}
    </SettingsSection>
  )
}
