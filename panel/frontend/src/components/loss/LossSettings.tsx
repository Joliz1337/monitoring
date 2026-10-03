import { useEffect, useState, type FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { ChevronDown, ChevronRight, Loader2, Plus, Settings2, X } from 'lucide-react'
import type { LossExcludedTarget, LossExclusions, Server } from '../../api/client'
import { Checkbox } from '../ui/Checkbox'
import { ServerSelector } from '../ssh/ServerSelector'

interface Props {
  servers: Server[]
  exclusions: LossExclusions | null
  loadFailed: boolean
  onSave: (next: LossExclusions) => Promise<boolean>
}

function sameIds(a: number[], b: number[]): boolean {
  if (a.length !== b.length) return false
  const sortedB = [...b].sort((x, y) => x - y)
  return [...a].sort((x, y) => x - y).every((id, i) => id === sortedB[i])
}

function sameTargets(a: LossExcludedTarget[], b: LossExcludedTarget[]): boolean {
  return a.length === b.length && a.every((item, i) => item.target === b[i].target && item.total_only === b[i].total_only)
}

export default function LossSettings({ servers, exclusions, loadFailed, onSave }: Props) {
  const { t } = useTranslation()
  const [open, setOpen] = useState(false)
  const [serverDraft, setServerDraft] = useState<number[]>([])
  const [targetsDraft, setTargetsDraft] = useState<LossExcludedTarget[]>([])
  const [newTargets, setNewTargets] = useState('')
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    if (!exclusions) return
    setServerDraft(exclusions.excluded_server_ids)
    setTargetsDraft(exclusions.excluded_targets)
  }, [exclusions])

  const dirty = exclusions !== null && (
    !sameIds(exclusions.excluded_server_ids, serverDraft)
    || !sameTargets(exclusions.excluded_targets, targetsDraft)
  )
  const excludedCount = exclusions ? exclusions.excluded_server_ids.length + exclusions.excluded_targets.length : 0

  // Вставленный списком текст разбирается по пробелам, запятым и строкам; адрес
  // проверяет панель при сохранении
  const addTargets = (e: FormEvent) => {
    e.preventDefault()
    const known = new Set(targetsDraft.map(item => item.target))
    const added = [...new Set(newTargets.split(/[\s,]+/).filter(Boolean))].filter(target => !known.has(target))
    setTargetsDraft(prev => [...prev, ...added.map(target => ({ target, total_only: false }))])
    setNewTargets('')
  }

  const setTotalOnly = (target: string, totalOnly: boolean) => {
    setTargetsDraft(prev => prev.map(item => (item.target === target ? { ...item, total_only: totalOnly } : item)))
  }

  const removeTarget = (target: string) => {
    setTargetsDraft(prev => prev.filter(item => item.target !== target))
  }

  const save = async () => {
    setSaving(true)
    await onSave({ excluded_server_ids: serverDraft, excluded_targets: targetsDraft })
    setSaving(false)
  }

  return (
    <div className="bg-dark-900/50 rounded-xl border border-dark-800/50">
      <button onClick={() => setOpen(v => !v)} className="w-full flex items-center gap-2 px-4 py-3 text-left">
        {open ? <ChevronDown className="w-4 h-4 text-dark-500" /> : <ChevronRight className="w-4 h-4 text-dark-500" />}
        <Settings2 className="w-4 h-4 text-dark-400" />
        <span className="text-sm font-medium text-dark-200">{t('loss.settings_title')}</span>
        {excludedCount > 0 && (
          <span className="text-xs text-dark-500">{t('loss.excluded_count', { count: excludedCount })}</span>
        )}
      </button>
      {open && (
        <div className="border-t border-dark-800/50 p-4 space-y-5">
          {loadFailed ? (
            <p className="text-sm text-danger">{t('loss.settings_load_failed')}</p>
          ) : exclusions === null ? (
            <Loader2 className="w-5 h-5 text-accent-400 animate-spin" />
          ) : (
            <>
              <div className="space-y-3">
                <div>
                  <h3 className="text-sm text-dark-200">{t('loss.excluded_title')}</h3>
                  <p className="text-xs text-dark-500 mt-1">{t('loss.excluded_hint')}</p>
                </div>
                <ServerSelector servers={servers} selectedIds={serverDraft} onChange={setServerDraft} />
              </div>
              <div className="space-y-3">
                <div>
                  <h3 className="text-sm text-dark-200">{t('loss.excluded_targets_title')}</h3>
                  <p className="text-xs text-dark-500 mt-1">{t('loss.excluded_targets_hint')}</p>
                </div>
                {targetsDraft.length > 0 && (
                  <div className="space-y-1.5">
                    {targetsDraft.map(item => (
                      <div
                        key={item.target}
                        className="flex items-center gap-3 flex-wrap px-3 py-2 rounded-lg bg-dark-800/50 border border-dark-700/50"
                      >
                        <span className="font-mono text-sm text-dark-100 flex-1 min-w-[10rem] break-all">{item.target}</span>
                        <label className="flex items-center gap-2 text-xs text-dark-400 cursor-pointer" title={t('loss.total_only_hint')}>
                          <Checkbox checked={item.total_only} onChange={e => setTotalOnly(item.target, e.target.checked)} />
                          {t('loss.total_only')}
                        </label>
                        <button
                          onClick={() => removeTarget(item.target)}
                          title={t('loss.excluded_target_remove')}
                          className="p-1.5 rounded-md text-dark-500 hover:text-danger hover:bg-danger/10 transition-colors"
                        >
                          <X className="w-4 h-4" />
                        </button>
                      </div>
                    ))}
                  </div>
                )}
                <form onSubmit={addTargets} className="flex gap-2">
                  <input
                    type="text"
                    value={newTargets}
                    onChange={e => setNewTargets(e.target.value)}
                    placeholder="62.50.146.225:8443"
                    className="input font-mono text-sm flex-1 min-w-0"
                  />
                  <button
                    type="submit"
                    disabled={!newTargets.trim()}
                    className="flex items-center gap-1.5 px-3 py-2 rounded-lg text-sm text-accent-400 hover:bg-accent-500/10 transition-colors disabled:opacity-40"
                  >
                    <Plus className="w-4 h-4" /> {t('loss.excluded_target_add')}
                  </button>
                </form>
              </div>
              <div className="flex justify-end">
                <button
                  onClick={save}
                  disabled={!dirty || saving}
                  className="flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium bg-accent-600 hover:bg-accent-500 text-white transition-colors disabled:opacity-40"
                >
                  {saving && <Loader2 className="w-4 h-4 animate-spin" />}
                  {t('common.save')}
                </button>
              </div>
            </>
          )}
        </div>
      )}
    </div>
  )
}
