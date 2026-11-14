import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { ChevronDown, ChevronRight, Loader2, Settings2 } from 'lucide-react'
import type { LossExcludedTarget, LossSettingsData, QuarantineSettingsData, Server } from '../../api/client'
import { Checkbox } from '../ui/Checkbox'
import { ServerSelector } from '../ssh/ServerSelector'
import AddressList from './AddressList'
import QuarantineSettings, { isQuarantineValid } from './QuarantineSettings'

interface Props {
  servers: Server[]
  settings: LossSettingsData | null
  loadFailed: boolean
  onSave: (next: LossSettingsData) => Promise<boolean>
}

function sameIds(a: number[], b: number[]): boolean {
  if (a.length !== b.length) return false
  const sortedB = [...b].sort((x, y) => x - y)
  return [...a].sort((x, y) => x - y).every((id, i) => id === sortedB[i])
}

function sameTargets(a: LossExcludedTarget[], b: LossExcludedTarget[]): boolean {
  return a.length === b.length && a.every((item, i) => item.target === b[i].target && item.total_only === b[i].total_only)
}

function sameQuarantine(a: QuarantineSettingsData, b: QuarantineSettingsData): boolean {
  const { exempt_server_ids: serversA, exempt_targets: targetsA, ...valuesA } = a
  const { exempt_server_ids: serversB, exempt_targets: targetsB, ...valuesB } = b
  return sameIds(serversA, serversB)
    && targetsA.length === targetsB.length && targetsA.every((target, i) => target === targetsB[i])
    && (Object.keys(valuesA) as (keyof typeof valuesA)[]).every(key => valuesA[key] === valuesB[key])
}

export default function LossSettings({ servers, settings, loadFailed, onSave }: Props) {
  const { t } = useTranslation()
  const [open, setOpen] = useState(false)
  const [serverDraft, setServerDraft] = useState<number[]>([])
  const [targetsDraft, setTargetsDraft] = useState<LossExcludedTarget[]>([])
  const [quarantineDraft, setQuarantineDraft] = useState<QuarantineSettingsData | null>(null)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    if (!settings) return
    setServerDraft(settings.excluded_server_ids)
    setTargetsDraft(settings.excluded_targets)
    setQuarantineDraft(settings.quarantine)
  }, [settings])

  const dirty = settings !== null && quarantineDraft !== null && (
    !sameIds(settings.excluded_server_ids, serverDraft)
    || !sameTargets(settings.excluded_targets, targetsDraft)
    || !sameQuarantine(settings.quarantine, quarantineDraft)
  )
  const excludedCount = settings ? settings.excluded_server_ids.length + settings.excluded_targets.length : 0

  const addTargets = (added: string[]) => {
    setTargetsDraft(prev => [...prev, ...added.map(target => ({ target, total_only: false }))])
  }

  const setTotalOnly = (target: string, totalOnly: boolean) => {
    setTargetsDraft(prev => prev.map(item => (item.target === target ? { ...item, total_only: totalOnly } : item)))
  }

  const removeTarget = (target: string) => {
    setTargetsDraft(prev => prev.filter(item => item.target !== target))
  }

  const save = async () => {
    if (!quarantineDraft) return
    setSaving(true)
    await onSave({ excluded_server_ids: serverDraft, excluded_targets: targetsDraft, quarantine: quarantineDraft })
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
          ) : quarantineDraft === null ? (
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
                <AddressList
                  addresses={targetsDraft.map(item => item.target)}
                  onAdd={addTargets}
                  onRemove={removeTarget}
                  renderOptions={(target, index) => (
                    <label className="flex items-center gap-2 text-xs text-dark-400 cursor-pointer" title={t('loss.total_only_hint')}>
                      <Checkbox checked={targetsDraft[index].total_only} onChange={e => setTotalOnly(target, e.target.checked)} />
                      {t('loss.total_only')}
                    </label>
                  )}
                />
              </div>
              <div className="border-t border-dark-800/50 pt-5">
                <QuarantineSettings servers={servers} value={quarantineDraft} onChange={setQuarantineDraft} />
              </div>
              <div className="flex justify-end">
                <button
                  onClick={save}
                  disabled={!dirty || saving || !isQuarantineValid(quarantineDraft)}
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
