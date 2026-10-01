import { useCallback, useEffect, useRef, useState } from 'react'
import { motion } from 'framer-motion'
import { useTranslation } from 'react-i18next'
import { AlertTriangle, CheckCircle2, Loader2, RotateCcw, Route, X, XCircle } from 'lucide-react'
import { lossApi, type PathTrace, type TraceHop } from '../../api/client'
import { Tooltip } from '../ui/Tooltip'
import { LOSS_BAD_PCT, LOSS_WARN_PCT } from '../ui/LossProbeBadge'

const POLL_INTERVAL_MS = 1_000

interface Props {
  serverId: number
  serverName: string
  target: string
  onClose: () => void
}

function lossColor(lossPct: number): string {
  if (lossPct > LOSS_BAD_PCT) return 'bg-danger'
  if (lossPct > LOSS_WARN_PCT) return 'bg-warning'
  return 'bg-success'
}

function errorCode(err: unknown): string {
  const detail = (err as { response?: { data?: { detail?: unknown } } }).response?.data?.detail
  return typeof detail === 'string' ? detail : 'unreachable'
}

function hopLabel(hop: TraceHop | undefined): string {
  if (!hop) return ''
  return hop.owner ?? hop.as_name ?? hop.asn ?? hop.host ?? `#${hop.hop}`
}

export default function TraceModal({ serverId, serverName, target, onClose }: Props) {
  const { t } = useTranslation()
  const [trace, setTrace] = useState<PathTrace | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [runId, setRunId] = useState(0)
  const traceIdRef = useRef<string | null>(null)

  const start = useCallback(async () => {
    setTrace(null)
    setError(null)
    traceIdRef.current = null
    try {
      const { data } = await lossApi.startTrace(serverId, target)
      traceIdRef.current = data.trace_id
      setRunId(id => id + 1)
    } catch (err) {
      setError(errorCode(err))
    }
  }, [serverId, target])

  useEffect(() => { start() }, [start])

  // Опрос: пока трасса идёт — раз в секунду, окно заполняется по ходу
  useEffect(() => {
    if (!traceIdRef.current) return
    const traceId = traceIdRef.current
    let stopped = false
    let timer: ReturnType<typeof setTimeout>
    const poll = async () => {
      try {
        const { data } = await lossApi.getTrace(serverId, traceId)
        if (stopped) return
        setTrace(data)
        if (data.state === 'running') timer = setTimeout(poll, POLL_INTERVAL_MS)
      } catch (err) {
        if (!stopped) setError(errorCode(err))
      }
    }
    poll()
    return () => {
      stopped = true
      clearTimeout(timer)
    }
  }, [runId, serverId])

  const running = !error && (!trace || trace.state === 'running')
  const analysis = trace?.analysis
  const hopByNumber = new Map((trace?.hops ?? []).map(hop => [hop.hop, hop]))
  const problemHops = new Set(analysis?.problem_hops ?? [])
  const hasVerdict = !!analysis && !analysis.preliminary && (analysis.verdict === 'clean' || analysis.verdict === 'loss_from')

  const verdict = () => {
    if (error) return { tone: 'danger', icon: <XCircle className="w-5 h-5" />, text: t(`loss.trace_error_${error}`) }
    if (trace?.state === 'failed') {
      return { tone: 'danger', icon: <XCircle className="w-5 h-5" />, text: t('loss.trace_failed', { error: trace.error ?? '' }) }
    }
    if (!analysis || analysis.verdict === 'waiting' || analysis.preliminary) {
      return { tone: 'muted', icon: <Loader2 className="w-5 h-5 animate-spin" />, text: t('loss.trace_collecting') }
    }
    switch (analysis.verdict) {
      case 'clean':
        return { tone: 'success', icon: <CheckCircle2 className="w-5 h-5" />, text: t('loss.trace_clean', { loss: analysis.dest_loss ?? 0 }) }
      case 'loss_from': {
        const start = hopByNumber.get(analysis.start_hop ?? -1)
        const prev = hopByNumber.get(analysis.prev_hop ?? -1)
        return {
          tone: 'danger',
          icon: <AlertTriangle className="w-5 h-5" />,
          text: prev
            ? t('loss.trace_loss_between', { from: hopLabel(prev), fromHop: prev.hop, to: hopLabel(start), toHop: start?.hop, loss: analysis.dest_loss })
            : t('loss.trace_loss_from', { to: hopLabel(start), toHop: start?.hop, loss: analysis.dest_loss }),
        }
      }
      case 'broken_after': {
        const last = hopByNumber.get(analysis.last_hop ?? -1)
        return { tone: 'warning', icon: <AlertTriangle className="w-5 h-5" />, text: t('loss.trace_broken', { name: hopLabel(last), hop: last?.hop }) }
      }
      default:
        return { tone: 'muted', icon: <AlertTriangle className="w-5 h-5" />, text: t('loss.trace_no_replies') }
    }
  }
  const banner = verdict()
  const toneCls: Record<string, string> = {
    danger: 'border-danger/30 bg-danger/10 text-danger',
    warning: 'border-warning/30 bg-warning/10 text-warning',
    success: 'border-success/30 bg-success/10 text-success',
    muted: 'border-dark-700 bg-dark-800/60 text-dark-300',
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" onClick={onClose}>
      <motion.div
        className="card w-full max-w-4xl max-h-[90vh] flex flex-col"
        initial={{ opacity: 0, scale: 0.96 }}
        animate={{ opacity: 1, scale: 1 }}
        onClick={e => e.stopPropagation()}
      >
        <div className="flex items-start justify-between gap-3 mb-3">
          <div>
            <h2 className="text-lg font-semibold text-dark-100 flex items-center gap-2">
              <Route className="w-5 h-5 text-accent-500" />
              {t('loss.trace_title')}
            </h2>
            <p className="text-sm text-dark-400 mt-0.5">
              {serverName} → <span className="font-mono text-dark-200">{target}</span>
            </p>
          </div>
          <div className="flex items-center gap-3">
            {trace && (
              <span className="text-xs text-dark-400 whitespace-nowrap">
                {running ? t('loss.trace_progress', { done: trace.rounds_done, total: trace.rounds_total }) : t('loss.trace_finished')}
              </span>
            )}
            {!running && (
              <Tooltip label={t('loss.trace_again')}>
                <button onClick={start} className="text-dark-400 hover:text-dark-100">
                  <RotateCcw className="w-4 h-4" />
                </button>
              </Tooltip>
            )}
            <button onClick={onClose} className="text-dark-500 hover:text-dark-200">
              <X className="w-5 h-5" />
            </button>
          </div>
        </div>

        {trace && running && (
          <div className="h-1 rounded-full bg-dark-800 overflow-hidden mb-3">
            <div
              className="h-full bg-accent-500 transition-all duration-700"
              style={{ width: `${Math.round((trace.rounds_done / Math.max(trace.rounds_total, 1)) * 100)}%` }}
            />
          </div>
        )}

        <div className={`flex items-start gap-3 rounded-xl border px-4 py-3 mb-4 ${toneCls[banner.tone]}`}>
          <span className="mt-0.5 shrink-0">{banner.icon}</span>
          <p className="text-sm leading-relaxed">{banner.text}</p>
        </div>

        <div className="overflow-y-auto pr-1">
          {(trace?.hops ?? []).map((hop, index, all) => {
            const silent = !hop.host || hop.received === 0
            const inProblem = problemHops.has(hop.hop)
            const isStart = analysis?.verdict === 'loss_from' && analysis.start_hop === hop.hop
            // «Шум» роутера — только когда вывод уже есть: до него потери на узле ещё могут оказаться началом проблемы
            const noisy = hasVerdict && !silent && !inProblem && hop.loss_pct > LOSS_WARN_PCT
            return (
              <div key={hop.hop} className="flex gap-3">
                <div className="flex flex-col items-center w-8 shrink-0">
                  <div className={`w-7 h-7 rounded-full flex items-center justify-center text-xs font-mono border ${
                    inProblem ? 'border-danger/60 bg-danger/15 text-danger'
                      : silent ? 'border-dark-700 bg-dark-800 text-dark-500'
                        : 'border-accent-500/40 bg-accent-500/10 text-accent-300'
                  }`}>
                    {hop.hop}
                  </div>
                  {index < all.length - 1 && (
                    <div className={`w-0.5 flex-1 min-h-[14px] ${problemHops.has(all[index + 1].hop) ? 'bg-danger/50' : 'bg-dark-700'}`} />
                  )}
                </div>
                <div className={`flex-1 min-w-0 mb-2 rounded-lg px-3 py-2 ${
                  inProblem ? 'bg-danger/5 border border-danger/20' : 'border border-transparent'
                } ${silent ? 'opacity-50' : ''}`}>
                  {silent ? (
                    <div className="text-xs text-dark-500 py-1">{t('loss.trace_silent')}</div>
                  ) : (
                    <div className="grid grid-cols-1 md:grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)_minmax(0,0.8fr)] gap-x-4 gap-y-1 items-center">
                      <div className="min-w-0">
                        <div className="flex items-center gap-2 flex-wrap">
                          <span className="font-mono text-sm text-dark-100">{hop.host}</span>
                          {hop.owner && (
                            <span className="px-1.5 py-0.5 rounded text-[10px] bg-accent-500/10 text-accent-300 border border-accent-500/20">{hop.owner}</span>
                          )}
                          {isStart && <span className="text-xs text-danger font-medium">{t('loss.trace_starts_here')}</span>}
                        </div>
                        <div className="text-xs text-dark-500 truncate" title={hop.as_name ?? undefined}>
                          {hop.asn ?? t('loss.trace_asn_unknown')}{hop.as_name ? ` · ${hop.as_name}` : ''}
                        </div>
                      </div>
                      <div className="flex items-center gap-2">
                        <div className="flex-1 h-2 rounded-full bg-dark-800 overflow-hidden">
                          <div className={`h-full ${noisy ? 'bg-dark-500' : lossColor(hop.loss_pct)}`} style={{ width: `${Math.max(hop.loss_pct, 2)}%` }} />
                        </div>
                        {noisy ? (
                          <Tooltip label={t('loss.trace_noise_hint')} maxWidth={280}>
                            <span className="text-xs text-dark-400 w-12 text-right underline decoration-dotted">{hop.loss_pct}%</span>
                          </Tooltip>
                        ) : (
                          <span className={`text-xs w-12 text-right ${hop.loss_pct > LOSS_BAD_PCT ? 'text-danger' : hop.loss_pct > LOSS_WARN_PCT ? 'text-warning' : 'text-dark-300'}`}>
                            {hop.loss_pct}%
                          </span>
                        )}
                      </div>
                      <div className="text-xs text-dark-400 font-mono whitespace-nowrap">
                        {hop.avg_ms != null
                          ? <>{hop.avg_ms} {t('loss_probe.ms')} <span className="text-dark-600">({hop.best_ms ?? '—'}–{hop.worst_ms ?? '—'})</span></>
                          : '—'}
                      </div>
                    </div>
                  )}
                </div>
              </div>
            )
          })}
          {!trace && !error && (
            <div className="flex items-center justify-center py-10">
              <Loader2 className="w-6 h-6 text-accent-400 animate-spin" />
            </div>
          )}
        </div>

        <p className="text-xs text-dark-500 pt-3">{t('loss.trace_hint')}</p>
      </motion.div>
    </div>
  )
}
