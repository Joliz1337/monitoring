import { Fragment, useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { motion } from 'framer-motion'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import { ChevronDown, ChevronRight, ListChecks, Loader2, Radar, Search, Trash2 } from 'lucide-react'
import { lossApi, type BackendEditJob, type LossCheckResult, type LossTarget } from '../api/client'
import { useServersStore } from '../stores/serversStore'
import { useAutoRefresh } from '../hooks/useAutoRefresh'
import LossProbeBadge, { LOSS_WARN_PCT } from '../components/ui/LossProbeBadge'
import { Checkbox } from '../components/ui/Checkbox'
import { ServerSelector } from '../components/ssh/ServerSelector'
import BackendEditModal from '../components/loss/BackendEditModal'
import BatchEditModal, { type BatchMode } from '../components/loss/BatchEditModal'
import ActivityStrip, { type RunningCheck } from '../components/loss/ActivityStrip'
import { readStorage, writeStorage } from '../utils/storage'

// Нода обновляет окно каждые 2 с, панель собирает метрики раз в ~10 с
const REFRESH_INTERVAL_MS = 10_000
// Пока задание идёт — полоска обновляется часто, иначе только подхватывает новые
const JOBS_ACTIVE_INTERVAL_MS = 2_000
const JOBS_IDLE_INTERVAL_MS = 15_000
// Завершённое задание висит в полоске, пока его не закроют, но не дольше этого
const FINISHED_JOB_VISIBLE_SEC = 30 * 60
const DISMISSED_JOBS_KEY = 'loss_dismissed_jobs'
const DISMISSED_JOBS_KEEP = 50

function readDismissed(): Set<string> {
  try {
    return new Set(JSON.parse(readStorage(DISMISSED_JOBS_KEY) ?? '[]'))
  } catch {
    return new Set()
  }
}

const inputCls = 'w-full px-3 py-2 bg-dark-800 border border-dark-700 rounded-lg text-sm text-dark-100 placeholder-dark-500 focus:outline-none focus:border-accent-500/50'

function isLossy(lossPct: number): boolean {
  return lossPct > LOSS_WARN_PCT
}

export default function Loss() {
  const { t } = useTranslation()
  const { servers, fetchServers } = useServersStore()
  const [targets, setTargets] = useState<LossTarget[] | null>(null)
  const [showAll, setShowAll] = useState(false)
  const [expanded, setExpanded] = useState<Set<string>>(new Set())

  const [checkTarget, setCheckTarget] = useState('')
  // null — выбор ещё не трогали: по умолчанию проверяют все релеи
  const [selectedIds, setSelectedIds] = useState<number[] | null>(null)
  const [checking, setChecking] = useState(false)
  const [checkResult, setCheckResult] = useState<{ ip: string; port: number; results: LossCheckResult[] } | null>(null)
  const checkBlockRef = useRef<HTMLDivElement>(null)
  const [editing, setEditing] = useState<LossTarget | null>(null)
  const [batch, setBatch] = useState<{ mode: BatchMode; targets: LossTarget[] } | null>(null)
  const [picked, setPicked] = useState<Set<string>>(new Set())
  const [jobs, setJobs] = useState<BackendEditJob[]>([])
  const [dismissed, setDismissed] = useState<Set<string>>(readDismissed)
  const [runningCheck, setRunningCheck] = useState<RunningCheck | null>(null)

  const fetchOverview = useCallback(async () => {
    try {
      const { data } = await lossApi.overview()
      setTargets(data.targets)
    } catch {
      setTargets(prev => prev ?? [])
    }
  }, [])

  useAutoRefresh(fetchOverview, { customInterval: REFRESH_INTERVAL_MS })

  const fetchJobs = useCallback(async () => {
    try {
      const { data } = await lossApi.jobs()
      setJobs(data.jobs)
    } catch { /* полоска покажет прошлое состояние до следующего опроса */ }
  }, [])

  const anyJobRunning = jobs.some(job => job.stage === 'editing' || job.stage === 'rollout')
  useAutoRefresh(fetchJobs, { customInterval: anyJobRunning ? JOBS_ACTIVE_INTERVAL_MS : JOBS_IDLE_INTERVAL_MS })

  const visibleJobs = useMemo(() => {
    const now = Date.now() / 1000
    return jobs.filter(job =>
      job.stage === 'editing' || job.stage === 'rollout'
      || (!dismissed.has(job.id) && now - (job.finished_at ?? job.created_at) < FINISHED_JOB_VISIBLE_SEC))
  }, [jobs, dismissed])

  const dismissJob = (id: string) => {
    setDismissed(prev => {
      const next = new Set([...prev, id])
      writeStorage(DISMISSED_JOBS_KEY, JSON.stringify([...next].slice(-DISMISSED_JOBS_KEEP)))
      return next
    })
  }

  const onJobStarted = (job: BackendEditJob) => {
    setJobs(prev => [job, ...prev.filter(existing => existing.id !== job.id)])
    setPicked(new Set())
  }

  useEffect(() => {
    fetchServers()
  }, [fetchServers])

  const activeServers = useMemo(() => servers.filter(s => s.is_active), [servers])

  const relayIds = useMemo(() => {
    const ids = new Set<number>()
    for (const target of targets ?? []) for (const relay of target.relays) ids.add(relay.server_id)
    return [...ids]
  }, [targets])

  const effectiveSelection = selectedIds ?? relayIds
  const visible = useMemo(
    () => (targets ?? []).filter(target => showAll || isLossy(target.worst_loss)),
    [targets, showAll],
  )
  // Правка профилей — только IPv4: DNAT других адресов не держит
  const editable = useMemo(() => visible.filter(target => target.ip.includes('.')), [visible])
  const pickedTargets = useMemo(() => (targets ?? []).filter(target => picked.has(target.target)), [targets, picked])
  const allPicked = editable.length > 0 && editable.every(target => picked.has(target.target))

  const togglePicked = (key: string) => {
    setPicked(prev => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  const togglePickAll = () => {
    setPicked(allPicked ? new Set() : new Set(editable.map(target => target.target)))
  }

  const toggleExpanded = (key: string) => {
    setExpanded(prev => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  const prefillCheck = (target: string) => {
    setCheckTarget(target)
    checkBlockRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }

  const runCheck = async (e: FormEvent) => {
    e.preventDefault()
    if (!checkTarget.trim() || effectiveSelection.length === 0) return
    setChecking(true)
    setRunningCheck({ target: checkTarget.trim(), nodes: effectiveSelection.length })
    try {
      const { data } = await lossApi.check(checkTarget.trim(), effectiveSelection)
      setCheckResult(data)
    } catch (err) {
      const detail = (err as { response?: { data?: { detail?: string } } }).response?.data?.detail
      toast.error(detail ? `${t('loss.check_failed')}: ${detail}` : t('loss.check_failed'))
    } finally {
      setChecking(false)
      setRunningCheck(null)
    }
  }

  const levelName = (level: number) => t(`loss.level_${level}`)

  return (
    <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} className={`space-y-6 ${picked.size > 0 ? 'pb-24' : ''}`}>
      <div className="flex items-center gap-3">
        <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-accent-500/20 to-accent-600/20 flex items-center justify-center border border-accent-500/20">
          <Radar className="w-5 h-5 text-accent-400" />
        </div>
        <div>
          <h1 className="text-2xl font-bold text-dark-100">{t('loss.title')}</h1>
          <p className="text-sm text-dark-400">{t('loss.subtitle')}</p>
        </div>
      </div>

      <ActivityStrip jobs={visibleJobs} check={runningCheck} onDismiss={dismissJob} />

      <div className="bg-dark-900/50 rounded-xl border border-dark-800/50">
        <div className="flex items-center justify-between gap-3 px-4 py-3 border-b border-dark-800/50 flex-wrap">
          <h2 className="text-sm font-medium text-dark-200">{t('loss.overview_title')}</h2>
          <div className="flex items-center gap-4 flex-wrap">
            {targets && targets.length > 0 && (
              <label className="flex items-center gap-2 text-xs text-dark-400 cursor-pointer">
                <input type="checkbox" checked={showAll} onChange={e => setShowAll(e.target.checked)} className="accent-accent-500" />
                {t('loss.show_all', { count: targets.length })}
              </label>
            )}
            <button
              onClick={() => setBatch({ mode: 'replace', targets: pickedTargets })}
              className="flex items-center gap-1.5 px-2.5 py-1 rounded-lg text-xs text-accent-400 hover:bg-accent-500/10 transition-colors"
            >
              <ListChecks className="w-3.5 h-3.5" /> {t('loss.batch_replace_open')}
            </button>
          </div>
        </div>

        {targets === null ? (
          <div className="flex items-center justify-center py-12">
            <Loader2 className="w-6 h-6 text-accent-400 animate-spin" />
          </div>
        ) : targets.length === 0 ? (
          <p className="text-center text-sm text-dark-500 py-10 px-4">{t('loss.no_data')}</p>
        ) : visible.length === 0 ? (
          <p className="text-center text-sm text-dark-500 py-10 px-4">{t('loss.all_clean', { count: targets.length })}</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs text-dark-500">
                  <th className="pl-4 py-2 w-8">
                    {editable.length > 0 && (
                      <Checkbox
                        checked={allPicked}
                        indeterminate={!allPicked && editable.some(target => picked.has(target.target))}
                        onChange={togglePickAll}
                      />
                    )}
                  </th>
                  <th className="px-4 py-2 font-medium">{t('loss.col_address')}</th>
                  <th className="px-4 py-2 font-medium">{t('loss.col_worst')}</th>
                  <th className="px-4 py-2 font-medium">{t('loss.col_relays')}</th>
                  <th className="px-4 py-2 font-medium">{t('loss.col_episode')}</th>
                  <th className="px-4 py-2" />
                </tr>
              </thead>
              <tbody>
                {visible.map(target => {
                  const isOpen = expanded.has(target.target)
                  const lossyCount = target.relays.filter(relay => isLossy(relay.loss_pct)).length
                  return (
                    <Fragment key={target.target}>
                      <tr
                        className="border-t border-dark-800/50 hover:bg-dark-800/30 cursor-pointer"
                        onClick={e => {
                          if ((e.target as HTMLElement).closest('button, input, label')) return
                          toggleExpanded(target.target)
                        }}
                      >
                        <td className="pl-4 py-2">
                          {target.ip.includes('.') && (
                            <Checkbox checked={picked.has(target.target)} onChange={() => togglePicked(target.target)} onClick={e => e.stopPropagation()} />
                          )}
                        </td>
                        <td className="px-4 py-2 whitespace-nowrap">
                          <div className="flex items-center gap-2">
                            {isOpen ? <ChevronDown className="w-4 h-4 text-dark-500" /> : <ChevronRight className="w-4 h-4 text-dark-500" />}
                            <div>
                              <div className="font-mono text-dark-100">{target.target}</div>
                              <div className="text-xs text-dark-500">{target.owner ?? t('loss.owner_unknown')}</div>
                            </div>
                          </div>
                        </td>
                        <td className="px-4 py-2"><LossProbeBadge probe={target.relays[0]} /></td>
                        <td className="px-4 py-2 text-dark-300 whitespace-nowrap">
                          {t('loss.relays_lossy', { lossy: lossyCount, total: target.relays.length })}
                        </td>
                        <td className="px-4 py-2 text-xs whitespace-nowrap">
                          {target.episode
                            ? <span className="px-2 py-0.5 rounded border bg-warning/10 text-warning border-warning/20">{levelName(target.episode.level)}</span>
                            : <span className="text-dark-500">—</span>}
                        </td>
                        <td className="px-4 py-2 text-right whitespace-nowrap">
                          {target.ip.includes('.') && (
                            <button
                              onClick={() => setEditing(target)}
                              className="px-2.5 py-1 rounded-lg text-xs text-accent-400 hover:bg-accent-500/10 transition-colors"
                            >
                              {t('loss.edit_open')}
                            </button>
                          )}
                          <button
                            onClick={() => prefillCheck(target.target)}
                            className="px-2.5 py-1 rounded-lg text-xs text-accent-400 hover:bg-accent-500/10 transition-colors"
                          >
                            {t('loss.check_this')}
                          </button>
                        </td>
                      </tr>
                      {isOpen && target.relays.map(relay => (
                        <tr key={`${target.target}@${relay.server_id}`} className="bg-dark-900/30 text-xs">
                          <td />
                          <td className="px-4 py-1.5 pl-10 text-dark-300">{relay.name}</td>
                          <td className="px-4 py-1.5"><LossProbeBadge probe={relay} /></td>
                          <td className="px-4 py-1.5 text-dark-500" colSpan={3}>{t('loss.samples', { count: relay.samples })}</td>
                        </tr>
                      ))}
                    </Fragment>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div ref={checkBlockRef} className="bg-dark-900/50 rounded-xl border border-dark-800/50 p-4 space-y-4">
        <div>
          <h2 className="text-sm font-medium text-dark-200">{t('loss.check_title')}</h2>
          <p className="text-xs text-dark-500 mt-1">{t('loss.check_hint')}</p>
        </div>
        <form onSubmit={runCheck} className="flex flex-col sm:flex-row gap-3">
          <input
            type="text"
            value={checkTarget}
            onChange={e => setCheckTarget(e.target.value)}
            placeholder="62.50.146.225:8443"
            className={`${inputCls} font-mono sm:max-w-xs`}
          />
          <button
            type="submit"
            disabled={checking || !checkTarget.trim() || effectiveSelection.length === 0}
            className="flex items-center justify-center gap-2 px-4 py-2 rounded-lg text-sm font-medium bg-accent-600 hover:bg-accent-500 text-white transition-colors disabled:opacity-40"
          >
            {checking ? <Loader2 className="w-4 h-4 animate-spin" /> : <Search className="w-4 h-4" />}
            {t('loss.check_run', { count: effectiveSelection.length })}
          </button>
        </form>

        <ServerSelector servers={activeServers} selectedIds={effectiveSelection} onChange={setSelectedIds} />

        {checkResult && (
          <div className="overflow-x-auto rounded-lg border border-dark-800/50">
            <div className="px-4 py-2 text-xs text-dark-400 border-b border-dark-800/50">
              {t('loss.check_result_for', { target: `${checkResult.ip}:${checkResult.port}` })}
            </div>
            <table className="w-full text-sm">
              <tbody>
                {checkResult.results.map(result => (
                  <tr key={result.server_id} className="border-t border-dark-800/30 first:border-t-0">
                    <td className="px-4 py-2 text-dark-200">{result.name}</td>
                    <td className="px-4 py-2">
                      {result.status === 'ok' && result.loss_pct != null
                        ? <LossProbeBadge probe={{ loss_pct: result.loss_pct, rtt_ms: result.rtt_ms ?? null, samples: result.samples ?? 0 }} />
                        : <span className="text-xs text-dark-500">{t(`loss.status_${result.status}`)}</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* fixed, а не sticky: длинную страницу прокручивает окно, sticky внутри <main> не прилипал.
          По центру области страницы — на десктопе слева статичное меню шириной 18rem */}
      {picked.size > 0 && (
        <div className="fixed bottom-6 z-40 left-1/2 -translate-x-1/2 lg:left-[calc(50%+9rem)] w-[calc(100vw-2rem)] lg:w-[calc(100vw-22rem)] max-w-4xl flex items-center gap-3 flex-wrap rounded-xl border border-accent-500/30 bg-dark-900/95 backdrop-blur px-4 py-3 shadow-2xl shadow-black/50">
          <span className="text-sm text-dark-200">{t('loss.picked', { count: picked.size })}</span>
          <button
            onClick={() => setBatch({ mode: 'delete', targets: pickedTargets })}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm text-danger hover:bg-danger/10 transition-colors"
          >
            <Trash2 className="w-4 h-4" /> {t('loss.batch_delete_open')}
          </button>
          <button
            onClick={() => setBatch({ mode: 'replace', targets: pickedTargets })}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm text-accent-400 hover:bg-accent-500/10 transition-colors"
          >
            <ListChecks className="w-4 h-4" /> {t('loss.batch_replace_open')}
          </button>
          <button onClick={() => setPicked(new Set())} className="ml-auto text-xs text-dark-400 hover:text-dark-200">
            {t('loss.picked_clear')}
          </button>
        </div>
      )}

      {editing && (
        <BackendEditModal ip={editing.ip} port={editing.port} onClose={() => setEditing(null)} onJobStarted={onJobStarted} />
      )}
      {batch && (
        <BatchEditModal mode={batch.mode} targets={batch.targets} onClose={() => setBatch(null)} onJobStarted={onJobStarted} />
      )}
    </motion.div>
  )
}
