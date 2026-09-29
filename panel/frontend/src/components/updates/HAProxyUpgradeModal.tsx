import { useEffect, useRef, useState } from 'react'
import { motion } from 'framer-motion'
import { ArrowRight, ArrowUpCircle, CheckCircle2, Loader2, X, XCircle } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import { haproxyUpgradeApi, haproxyUpgradeStreamUrl, type RemnawaveInstallEvent } from '../../api/client'
import { streamNdjsonGet } from '../../utils/ndjsonStream'

export interface HAProxyUpgradeTarget {
  id: number
  name: string
  version: string | null
  targetBranch: string | null
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
  const logEndRef = useRef<HTMLDivElement | null>(null)
  const isSingle = targets.length === 1

  // Стрим переигрывает весь лог задачи, поэтому повторное открытие окна показывает его целиком
  useEffect(() => {
    if (!jobId) return
    const controller = new AbortController()
    setLog([])
    setResult(null)
    streamNdjsonGet<RemnawaveInstallEvent>(
      haproxyUpgradeStreamUrl(jobId),
      (ev) => {
        if (ev.type === 'log') setLog(prev => [...prev, ev.line])
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

  const handleStart = async () => {
    setStarting(true)
    const results = await Promise.allSettled(targets.map(target => haproxyUpgradeApi.start(target.id)))
    setStarting(false)

    const failures = results.flatMap((res, i) =>
      res.status === 'rejected' ? [{ name: targets[i].name, reason: res.reason }] : [],
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
          <>
            <p className="text-sm text-dark-400 mb-3">{t('updates.haproxy_desc')}</p>
            <div className="mb-4 max-h-56 overflow-y-auto rounded-lg border border-dark-700/40 divide-y divide-dark-700/40">
              {targets.map(target => (
                <div key={target.id} className="flex items-center justify-between gap-3 px-3 py-2 text-sm">
                  <span className="text-dark-200 truncate">{target.name}</span>
                  <span className="flex items-center gap-1.5 font-mono text-xs flex-shrink-0">
                    <span className="text-dark-400">{shortHAProxyVersion(target.version) ?? t('updates.unknown')}</span>
                    <ArrowRight className="w-3 h-3 text-dark-500" />
                    <span className="text-accent-400">{target.targetBranch}</span>
                  </span>
                </div>
              ))}
            </div>
          </>
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
              <button onClick={handleStart} className="btn btn-primary" disabled={starting || targets.length === 0}>
                {starting ? <Loader2 className="w-4 h-4 animate-spin" /> : <ArrowUpCircle className="w-4 h-4" />}
                {t('updates.haproxy_start')}
              </button>
            )}
          </div>
        </div>
      </motion.div>
    </div>
  )
}
