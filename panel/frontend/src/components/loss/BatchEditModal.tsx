import { useEffect, useMemo, useState } from 'react'
import { motion } from 'framer-motion'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import { ArrowRight, ListChecks, Loader2, Plus, Trash2, X } from 'lucide-react'
import {
  lossApi,
  type BackendEditBody,
  type BackendEditJob,
  type BackendEditProfile,
  type LossSuggestion,
  type LossTarget,
} from '../../api/client'
import LossProbeBadge from '../ui/LossProbeBadge'
import { Checkbox } from '../ui/Checkbox'
import { IPV4_RE, PREVIEW_DEBOUNCE_MS, inputCls, outcomeText } from './editShared'

export type BatchMode = 'delete' | 'replace'

interface Props {
  mode: BatchMode
  targets: LossTarget[]
  onClose: () => void
  onJobStarted: (job: BackendEditJob) => void
}

interface ReplaceRow {
  old: string
  next: string
}

const DEFAULT_PORT = 443

function uniqueIps(targets: LossTarget[]): string[] {
  return [...new Set(targets.map(target => target.ip))]
}

export default function BatchEditModal({ mode, targets, onClose, onJobStarted }: Props) {
  const { t } = useTranslation()
  const [allPorts, setAllPorts] = useState(true)
  const [rows, setRows] = useState<ReplaceRow[]>(() => {
    const ips = uniqueIps(targets)
    return ips.length ? ips.map(ip => ({ old: ip, next: '' })) : [{ old: '', next: '' }]
  })
  const [preview, setPreview] = useState<BackendEditProfile[][] | null>(null)
  const [suggestions, setSuggestions] = useState<Record<string, LossSuggestion[]>>({})
  const [loading, setLoading] = useState(false)
  const [applying, setApplying] = useState(false)

  const portOf = useMemo(() => {
    const ports = new Map<string, number>()
    for (const target of targets) if (!ports.has(target.ip)) ports.set(target.ip, target.port)
    return ports
  }, [targets])

  // Правка на каждую строку; индекс строки — чтобы показать предпросмотр под ней
  const plan = useMemo<{ edits: BackendEditBody[]; rowOfEdit: number[] }>(() => {
    if (mode === 'delete') {
      const list = allPorts
        ? uniqueIps(targets).map(ip => ({ ip, port: portOf.get(ip) ?? DEFAULT_PORT }))
        : targets.map(target => ({ ip: target.ip, port: target.port }))
      return {
        edits: list.map(({ ip, port }) => ({ ip, port, all_ports: allPorts, action: 'delete', new_ip: null, new_port: null })),
        rowOfEdit: list.map((_, index) => index),
      }
    }
    const edits: BackendEditBody[] = []
    const rowOfEdit: number[] = []
    rows.forEach((row, index) => {
      const old = row.old.trim()
      const next = row.next.trim()
      if (!IPV4_RE.test(old) || !IPV4_RE.test(next) || old === next) return
      edits.push({ ip: old, port: portOf.get(old) ?? DEFAULT_PORT, all_ports: true, action: 'replace', new_ip: next, new_port: null })
      rowOfEdit.push(index)
    })
    return { edits, rowOfEdit }
  }, [mode, allPorts, targets, rows, portOf])

  // Подсказки нужны и для строк без нового IP — просим их отдельным предпросмотром
  const suggestionIps = useMemo(
    () => (mode === 'replace' ? rows.map(row => row.old.trim()).filter(ip => IPV4_RE.test(ip)) : []),
    [mode, rows],
  )

  useEffect(() => {
    const probe: BackendEditBody[] = mode === 'replace'
      ? [...new Set(suggestionIps)].map(ip => ({ ip, port: portOf.get(ip) ?? DEFAULT_PORT, all_ports: true, action: 'replace', new_ip: ip, new_port: null }))
      : []
    const edits = plan.edits.length ? plan.edits : probe
    if (!edits.length) {
      setPreview(plan.edits.length ? null : [])
      return
    }
    const timer = setTimeout(async () => {
      setLoading(true)
      try {
        const [main, hints] = await Promise.all([
          plan.edits.length ? lossApi.backendsPreview(plan.edits) : Promise.resolve(null),
          probe.length ? lossApi.backendsPreview(probe) : Promise.resolve(null),
        ])
        setPreview(main ? main.data.items : [])
        setSuggestions(hints ? hints.data.suggestions : (main ? main.data.suggestions : {}))
      } catch {
        setPreview(null)
      } finally {
        setLoading(false)
      }
    }, PREVIEW_DEBOUNCE_MS)
    return () => clearTimeout(timer)
  }, [plan, suggestionIps, mode, portOf])

  const affected = (preview ?? []).flat().flatMap(p => p.rules).filter(r => r.outcome !== 'skipped').length

  const updateRow = (index: number, patch: Partial<ReplaceRow>) =>
    setRows(prev => prev.map((row, i) => (i === index ? { ...row, ...patch } : row)))

  const apply = async () => {
    setApplying(true)
    try {
      const { data } = await lossApi.backendsApply(plan.edits)
      onJobStarted(data.job)
      toast.success(t('loss.job_started'))
      onClose()
    } catch (err) {
      const detail = (err as { response?: { data?: { detail?: string } } }).response?.data?.detail
      toast.error(typeof detail === 'string' ? `${t('loss.edit_failed')}: ${detail}` : t('loss.edit_failed'))
    } finally {
      setApplying(false)
    }
  }

  const previewFor = (rowIndex: number) => {
    const editIndex = plan.rowOfEdit.indexOf(rowIndex)
    if (editIndex === -1 || !preview) return null
    const profiles = preview[editIndex] ?? []
    if (!profiles.length) return <p className="text-xs text-dark-500">{t('loss.preview_empty')}</p>
    return (
      <ul className="space-y-0.5">
        {profiles.flatMap(profile => profile.rules.map(rule => (
          <li key={`${profile.kind}-${profile.profile_id}-${rule.rule}`} className="text-xs flex flex-wrap gap-x-2">
            <span className="text-dark-500">{t(`loss.kind_${profile.kind}`)}</span>
            <span className="text-dark-300">{profile.profile_name}</span>
            <span className="font-mono text-dark-300">{rule.rule}</span>
            <span className={rule.outcome === 'skipped' ? 'text-warning' : 'text-dark-400'}>
              {outcomeText(rule, mode, false, t)}
            </span>
          </li>
        )))}
      </ul>
    )
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" onClick={onClose}>
      <motion.div
        className="card w-full max-w-3xl max-h-[90vh] flex flex-col"
        initial={{ opacity: 0, scale: 0.96 }}
        animate={{ opacity: 1, scale: 1 }}
        onClick={e => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-lg font-semibold text-dark-100 flex items-center gap-2">
            {mode === 'delete' ? <Trash2 className="w-5 h-5 text-danger" /> : <ListChecks className="w-5 h-5 text-accent-500" />}
            {t(mode === 'delete' ? 'loss.batch_delete_title' : 'loss.batch_replace_title')}
          </h2>
          <button onClick={onClose} className="text-dark-500 hover:text-dark-200">
            <X className="w-5 h-5" />
          </button>
        </div>

        <div className="overflow-y-auto space-y-3 pr-1">
          <p className="text-xs text-dark-400">{t(mode === 'delete' ? 'loss.batch_delete_hint' : 'loss.batch_replace_hint')}</p>

          {mode === 'delete' && (
            <label className="flex items-center gap-2 text-xs text-dark-300 cursor-pointer">
              <Checkbox checked={allPorts} onChange={e => setAllPorts(e.target.checked)} />
              {t('loss.batch_all_ports')}
            </label>
          )}

          {mode === 'delete'
            ? plan.edits.map((edit, index) => (
              <div key={`${edit.ip}:${edit.port}`} className="rounded-lg border border-dark-800/60 px-3 py-2 space-y-1">
                <div className="font-mono text-sm text-dark-100">{allPorts ? edit.ip : `${edit.ip}:${edit.port}`}</div>
                {previewFor(index)}
              </div>
            ))
            : rows.map((row, index) => {
              const hints = suggestions[row.old.trim()] ?? []
              return (
                <div key={index} className="rounded-lg border border-dark-800/60 px-3 py-2 space-y-2">
                  <div className="flex items-center gap-2">
                    <input
                      value={row.old}
                      onChange={e => updateRow(index, { old: e.target.value })}
                      placeholder={t('loss.batch_old_placeholder')}
                      className={inputCls}
                    />
                    <ArrowRight className="w-4 h-4 text-dark-500 shrink-0" />
                    <input
                      value={row.next}
                      onChange={e => updateRow(index, { next: e.target.value })}
                      placeholder={t('loss.batch_new_placeholder')}
                      className={inputCls}
                    />
                    <button
                      onClick={() => setRows(prev => prev.filter((_, i) => i !== index))}
                      disabled={rows.length === 1}
                      className="p-2 text-dark-500 hover:text-danger disabled:opacity-30"
                    >
                      <X className="w-4 h-4" />
                    </button>
                  </div>
                  {hints.length > 0 && (
                    <div className="flex flex-wrap gap-1.5">
                      {hints.map(hint => (
                        <button
                          key={hint.ip}
                          onClick={() => updateRow(index, { next: hint.ip })}
                          className={`flex items-center gap-1.5 px-2 py-0.5 rounded-lg border text-xs font-mono ${
                            row.next.trim() === hint.ip ? 'border-accent-500/50 bg-accent-500/10' : 'border-dark-700 hover:border-dark-500'
                          }`}
                        >
                          <span className="text-dark-200">{hint.ip}</span>
                          {hint.worst_loss != null
                            ? <LossProbeBadge probe={{ loss_pct: hint.worst_loss, rtt_ms: null, samples: 0 }} />
                            : <span className="text-dark-500 font-sans">{t('loss.suggestion_unknown')}</span>}
                        </button>
                      ))}
                    </div>
                  )}
                  {previewFor(index)}
                </div>
              )
            })}

          {mode === 'replace' && (
            <button
              onClick={() => setRows(prev => [...prev, { old: '', next: '' }])}
              className="flex items-center gap-1.5 text-xs text-accent-400 hover:text-accent-300"
            >
              <Plus className="w-3.5 h-3.5" /> {t('loss.batch_add_row')}
            </button>
          )}

          <p className="text-xs text-dark-500">{t('loss.edit_not_touched')}</p>
        </div>

        <div className="flex items-center justify-end gap-2 pt-4">
          {loading && <Loader2 className="w-4 h-4 text-dark-500 animate-spin mr-auto" />}
          <button onClick={onClose} className="px-4 py-2 rounded-lg text-sm text-dark-300 hover:bg-dark-800">
            {t('common.cancel')}
          </button>
          <button
            onClick={apply}
            disabled={applying || loading || plan.edits.length === 0 || affected === 0}
            className={`flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium text-white transition-colors disabled:opacity-40 ${
              mode === 'delete' ? 'bg-danger/80 hover:bg-danger' : 'bg-accent-600 hover:bg-accent-500'
            }`}
          >
            {applying && <Loader2 className="w-4 h-4 animate-spin" />}
            {t('loss.edit_apply', { count: affected })}
          </button>
        </div>
      </motion.div>
    </div>
  )
}
