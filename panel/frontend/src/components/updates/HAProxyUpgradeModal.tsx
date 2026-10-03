import { useEffect, useRef, useState } from 'react'
import { motion } from 'framer-motion'
import { ArrowRight, ArrowUpCircle, CheckCircle2, Loader2, RotateCcw, X, XCircle } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import { haproxyUpgradeApi, haproxyUpgradeStreamUrl, nodeImageApi, type RemnawaveInstallEvent } from '../../api/client'
import { streamNdjsonGet } from '../../utils/ndjsonStream'
import { cleanInstallLogLine } from '../../utils/installLog'
import { Checkbox } from '../ui/Checkbox'
import SshCredsFields, { SSH_CREDS_DEFAULTS, hasSshSecret, toDeliveryCreds, type SshCredsValue } from '../servers/SshCredsFields'

export interface HAProxyUpgradeTarget {
  id: number
  name: string
  version: string | null
  targetVersion: string | null
  hasSshCreds: boolean
}

interface Props {
  targets: HAProxyUpgradeTarget[]
  /** Идущее или недавно завершённое обновление одного сервера — окно сразу открывается на его логе */
  jobId: string | null
  onStarted: () => void
  onClose: () => void
}

// 2.8.16-0ubuntu0.24.04.3 → 2.8.16: хвост пакета в карточке только мешает
export const shortHAProxyVersion = (version: string | null) => version?.split('-')[0] ?? null

export default function HAProxyUpgradeModal({ targets, jobId: initialJobId, onStarted, onClose }: Props) {
  const { t } = useTranslation()
  const [jobId, setJobId] = useState<string | null>(initialJobId)
  const [starting, setStarting] = useState(false)
  const [log, setLog] = useState<string[]>([])
  const [result, setResult] = useState<'success' | 'error' | null>(null)
  // Сервер за ТСПУ: обновление по SSH, пакеты качаются через панель
  const [viaPanel, setViaPanel] = useState(false)
  const [sshCreds, setSshCreds] = useState<SshCredsValue>(SSH_CREDS_DEFAULTS)
  const [saveSsh, setSaveSsh] = useState(false)
  const logEndRef = useRef<HTMLDivElement | null>(null)
  const isSingle = targets.length === 1
  const withoutCreds = targets.filter(target => !target.hasSshCreds)
  const formReady = hasSshSecret(sshCreds)
  // Без сохранённого доступа сервер обновится по SSH, только если доступ введён в форме
  const runnable = viaPanel ? targets.filter(target => target.hasSshCreds || formReady) : targets

  // Стрим переигрывает весь лог задачи, поэтому повторное открытие окна показывает его целиком
  useEffect(() => {
    if (!jobId) return
    const controller = new AbortController()
    setLog([])
    setResult(null)
    streamNdjsonGet<RemnawaveInstallEvent>(
      haproxyUpgradeStreamUrl(jobId),
      (ev) => {
        if (ev.type === 'log') setLog(prev => [...prev, cleanInstallLogLine(ev.line)])
        else if (ev.type === 'error') setLog(prev => [...prev, '✗ ' + ev.message])
        else if (ev.type === 'done') setResult(ev.status)
      },
      controller.signal,
    ).catch((err: Error) => {
      if (controller.signal.aborted) return
      setLog(prev => [...prev, '✗ ' + (err.message || t('updates.haproxy_failed'))])
    })
    return () => controller.abort()
  }, [jobId, t])

  useEffect(() => {
    logEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [log])

  const startOne = (target: HAProxyUpgradeTarget) => {
    if (!viaPanel) return haproxyUpgradeApi.start(target.id)
    // Пустое ssh — бэк возьмёт сохранённый у сервера доступ
    return haproxyUpgradeApi.start(target.id, {
      via_panel: true,
      ssh: target.hasSshCreds ? {} : toDeliveryCreds(sshCreds),
    })
  }

  const handleStart = async () => {
    const skipped = targets.filter(target => !runnable.includes(target))
    if (skipped.length > 0) {
      toast.warning(t('updates.haproxy_skipped_no_creds', { names: skipped.map(s => s.name).join(', ') }))
    }
    setStarting(true)
    if (viaPanel && formReady && saveSsh) {
      const creds = toDeliveryCreds(sshCreds)
      await Promise.allSettled(
        runnable.filter(target => !target.hasSshCreds).map(target => nodeImageApi.setSettings(target.id, creds)),
      )
    }
    const results = await Promise.allSettled(runnable.map(startOne))
    setStarting(false)

    const failures = results.flatMap((res, i) =>
      res.status === 'rejected' ? [{ name: runnable[i].name, reason: res.reason }] : [],
    )
    for (const { name, reason } of failures) {
      toast.error(`${name}: ${reason?.response?.data?.detail || reason?.message || t('updates.haproxy_failed')}`)
    }
    const started = results.length - failures.length
    if (started === 0) return
    onStarted()

    const first = results[0]
    if (isSingle && first.status === 'fulfilled') {
      setJobId(first.value.data.job_id)
      return
    }
    toast.success(t('updates.haproxy_started', { count: started }))
    onClose()
  }

  // Обратно к форме запуска: упавшая задача висит на карточке ещё 10 минут и иначе загораживает кнопку обновления
  const handleRetry = () => {
    setJobId(null)
    setResult(null)
  }

  const running = jobId !== null && result === null

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
      <motion.div
        className="card w-full max-w-2xl max-h-[90vh] flex flex-col"
        initial={{ opacity: 0, scale: 0.96 }}
        animate={{ opacity: 1, scale: 1 }}
      >
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold text-dark-100 flex items-center gap-2">
            <ArrowUpCircle className="w-5 h-5 text-accent-500" />
            {t('updates.haproxy_title')}
            {isSingle && <> — <span className="text-dark-300">{targets[0].name}</span></>}
          </h2>
          <button onClick={onClose} className="text-dark-500 hover:text-dark-200">
            <X className="w-5 h-5" />
          </button>
        </div>

        {jobId ? (
          <div className="flex-1 min-h-[160px] max-h-[360px] overflow-y-auto bg-dark-900/60 border border-dark-700/40 rounded-lg p-3 font-mono text-xs text-dark-300 mb-4">
            {log.map((line, i) => (
              <div key={i} className="whitespace-pre-wrap break-all">{line}</div>
            ))}
            <div ref={logEndRef} />
          </div>
        ) : (
          <div className="flex-1 min-h-0 overflow-y-auto">
            <p className="text-sm text-dark-400 mb-3">{t('updates.haproxy_desc')}</p>
            <div className="mb-4 max-h-56 overflow-y-auto rounded-lg border border-dark-700/40 divide-y divide-dark-700/40">
              {targets.map(target => (
                <div key={target.id} className="flex items-center justify-between gap-3 px-3 py-2 text-sm">
                  <span className="text-dark-200 truncate">{target.name}</span>
                  <span className="flex items-center gap-1.5 font-mono text-xs flex-shrink-0">
                    <span className="text-dark-400">{shortHAProxyVersion(target.version) ?? t('updates.unknown')}</span>
                    <ArrowRight className="w-3.5 h-3.5 text-dark-500" />
                    <span className="text-accent-400">{target.targetVersion}</span>
                  </span>
                </div>
              ))}
            </div>
            <div className="mb-4 space-y-3">
              <div>
                <label className="flex items-center gap-2.5 cursor-pointer">
                  <Checkbox checked={viaPanel} onChange={e => setViaPanel(e.target.checked)} disabled={starting} />
                  <span className="text-sm text-dark-200">{t('servers.deploy_via_panel')}</span>
                </label>
                <p className="text-xs text-dark-500 mt-1 ml-6">{t('updates.haproxy_via_panel_hint')}</p>
              </div>
              {viaPanel && (withoutCreds.length === 0 ? (
                <p className="text-xs text-dark-400">{t('updates.haproxy_ssh_stored')}</p>
              ) : (
                <div className="space-y-3">
                  <p className="text-xs text-warning">
                    {isSingle
                      ? t('updates.haproxy_ssh_missing_single')
                      : t('updates.haproxy_ssh_missing', { names: withoutCreds.map(target => target.name).join(', ') })}
                  </p>
                  <SshCredsFields
                    value={sshCreds}
                    onChange={patch => setSshCreds(prev => ({ ...prev, ...patch }))}
                    disabled={starting}
                    showHost={isSingle}
                  />
                  <label className="flex items-center gap-2.5 cursor-pointer">
                    <Checkbox checked={saveSsh} onChange={e => setSaveSsh(e.target.checked)} />
                    <span className="text-sm text-dark-300">{t('imageDelivery.save_creds')}</span>
                  </label>
                </div>
              ))}
            </div>
          </div>
        )}

        <div className="flex items-center justify-between gap-3 mt-auto">
          <div className="text-sm min-w-0">
            {running && (
              <span className="flex items-center gap-1.5 text-dark-400">
                <Loader2 className="w-4 h-4 animate-spin shrink-0" />{t('updates.haproxy_background_hint')}
              </span>
            )}
            {result === 'success' && (
              <span className="flex items-center gap-1.5 text-success"><CheckCircle2 className="w-4 h-4" />{t('updates.haproxy_done')}</span>
            )}
            {result === 'error' && (
              <span className="flex items-center gap-1.5 text-danger"><XCircle className="w-4 h-4" />{t('updates.haproxy_failed')}</span>
            )}
          </div>
          <div className="flex gap-2 shrink-0">
            <button onClick={onClose} className="btn btn-secondary">
              {t('common.close')}
            </button>
            {!jobId && (
              <button onClick={handleStart} className="btn btn-primary" disabled={starting || runnable.length === 0}>
                {starting ? <Loader2 className="w-4 h-4 animate-spin" /> : <ArrowUpCircle className="w-4 h-4" />}
                {t('updates.haproxy_start')}
              </button>
            )}
            {result === 'error' && (
              <button onClick={handleRetry} className="btn btn-primary">
                <RotateCcw className="w-4 h-4" />
                {t('updates.haproxy_retry')}
              </button>
            )}
          </div>
        </div>
      </motion.div>
    </div>
  )
}
