import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { AlertTriangle, CheckCircle2, ChevronDown, ChevronRight, Loader2, X } from 'lucide-react'
import type { BackendEditBody, BackendEditJob } from '../../api/client'

export interface RunningCheck {
  target: string
  nodes: number
}

interface Props {
  jobs: BackendEditJob[]
  check: RunningCheck | null
  onDismiss: (jobId: string) => void
}

function describeEdit(edit: BackendEditBody, t: (key: string, options?: Record<string, unknown>) => string): string {
  const source = edit.all_ports ? edit.ip : `${edit.ip}:${edit.port}`
  if (edit.action === 'delete') return t('loss.job_edit_delete', { source })
  const target = `${edit.new_ip ?? edit.ip}${edit.new_port && edit.new_port !== edit.port ? `:${edit.new_port}` : ''}`
  return `${source} → ${target}`
}

function JobCard({ job, onDismiss }: { job: BackendEditJob; onDismiss: (id: string) => void }) {
  const { t } = useTranslation()
  const [open, setOpen] = useState(false)
  const totals = job.profiles.reduce(
    (acc, p) => ({
      total: acc.total + p.total,
      synced: acc.synced + p.synced,
      pending: acc.pending + p.pending,
      failed: acc.failed + p.failed + p.denied,
    }),
    { total: 0, synced: 0, pending: 0, failed: 0 },
  )
  const changedRules = job.items.reduce((sum, item) => sum + item.changed, 0)
  const running = job.stage === 'editing' || job.stage === 'rollout'
  const donePct = totals.total ? Math.round(((totals.synced + totals.failed) / totals.total) * 100) : 0

  let icon = <Loader2 className="w-4 h-4 text-accent-400 animate-spin" />
  let title = t('loss.job_editing', { count: job.edits.length })
  let tone = 'border-accent-500/30 bg-accent-500/5'
  if (job.stage === 'rollout') {
    title = t('loss.job_rollout', { synced: totals.synced, total: totals.total })
  } else if (job.stage === 'done') {
    icon = totals.failed || job.failures.length
      ? <AlertTriangle className="w-4 h-4 text-warning" />
      : <CheckCircle2 className="w-4 h-4 text-success" />
    title = t('loss.job_done', { rules: changedRules, profiles: job.profiles.length })
    tone = totals.failed || job.failures.length ? 'border-warning/30 bg-warning/5' : 'border-success/30 bg-success/5'
  } else if (job.stage === 'failed') {
    icon = <AlertTriangle className="w-4 h-4 text-danger" />
    title = t('loss.job_failed', { error: job.error ?? '' })
    tone = 'border-danger/30 bg-danger/5'
  }

  return (
    <div className={`rounded-xl border ${tone}`}>
      <div className="flex items-center gap-3 px-4 py-2.5">
        {icon}
        <button onClick={() => setOpen(v => !v)} className="flex-1 min-w-0 text-left">
          <div className="flex items-center gap-2 text-sm text-dark-100">
            {open ? <ChevronDown className="w-3.5 h-3.5 text-dark-500" /> : <ChevronRight className="w-3.5 h-3.5 text-dark-500" />}
            <span className="truncate">{title}</span>
          </div>
          <div className="text-xs text-dark-400 mt-0.5 pl-5">
            {job.current && t('loss.job_current', { name: job.current })}
            {job.current && ' · '}
            {totals.total > 0 && t('loss.job_counts', { pending: totals.pending, failed: totals.failed })}
            {!running && totals.pending > 0 && ` · ${t('loss.job_pending_hint')}`}
          </div>
        </button>
        {!running && (
          <button onClick={() => onDismiss(job.id)} className="text-dark-500 hover:text-dark-200">
            <X className="w-4 h-4" />
          </button>
        )}
      </div>

      {job.stage === 'rollout' && totals.total > 0 && (
        <div className="mx-4 mb-2.5 h-1.5 rounded-full bg-dark-800 overflow-hidden">
          <div className="h-full bg-accent-500 transition-all duration-500" style={{ width: `${donePct}%` }} />
        </div>
      )}

      {open && (
        <div className="border-t border-dark-800/60 px-4 py-2.5 space-y-2 text-xs">
          {job.profiles.length > 0 && (
            <div className="space-y-1">
              {job.profiles.map(p => (
                <div key={`${p.kind}-${p.profile_id}`} className="flex flex-wrap gap-x-3 text-dark-300">
                  <span className="text-dark-500">{t(`loss.kind_${p.kind}`)}</span>
                  <span className="text-dark-200">{p.profile_name}</span>
                  <span>{t('loss.job_profile_counts', { synced: p.synced, total: p.total, pending: p.pending, failed: p.failed + p.denied })}</span>
                </div>
              ))}
            </div>
          )}
          {job.failures.length > 0 && (
            <div className="text-warning">{t('loss.job_failures', { names: job.failures.join(', ') })}</div>
          )}
          <div className="space-y-0.5">
            {job.edits.map((edit, index) => (
              <div key={index} className="flex gap-3 text-dark-400">
                <span className="font-mono text-dark-300">{describeEdit(edit, t)}</span>
                {job.items[index] && (
                  <span>{t('loss.job_item', { changed: job.items[index].changed, skipped: job.items[index].skipped })}</span>
                )}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

export default function ActivityStrip({ jobs, check, onDismiss }: Props) {
  const { t } = useTranslation()
  if (!check && jobs.length === 0) return null
  return (
    <div className="space-y-2">
      {check && (
        <div className="flex items-center gap-3 rounded-xl border border-accent-500/30 bg-accent-500/5 px-4 py-2.5 text-sm text-dark-100">
          <Loader2 className="w-4 h-4 text-accent-400 animate-spin" />
          {t('loss.job_check', { target: check.target, count: check.nodes })}
        </div>
      )}
      {jobs.map(job => <JobCard key={job.id} job={job} onDismiss={onDismiss} />)}
    </div>
  )
}
