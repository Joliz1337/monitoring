import { useEffect, useMemo, useState } from 'react'
import { motion } from 'framer-motion'
import { useTranslation } from 'react-i18next'
import { Loader2, Save, Shuffle } from 'lucide-react'
import type { SourcePoolAssignments, SourcePoolMode, SourcePoolNodeView } from '../../api/client'
import { Tooltip } from '../ui/Tooltip'
import { addressDotClass, assignmentsKey, poolMarks, roundRobinAssignments } from '../../utils/sourcePool'

const MODES: SourcePoolMode[] = ['auto', 'manual']

interface Props {
  view: SourcePoolNodeView
  editable: boolean
  saving: boolean
  onModeChange: (mode: SourcePoolMode) => void
  onSaveAssignments: (assignments: SourcePoolAssignments) => void
}

export default function MarkLayoutCard({ view, editable, saving, onModeChange, onSaveAssignments }: Props) {
  const { t } = useTranslation()
  const manual = view.mode === 'manual'
  const marks = poolMarks(view)

  // Черновик живёт до сохранения: страница опрашивает ноду раз в 10 с и не должна сбрасывать правки
  const savedKey = assignmentsKey(view.assignments)
  const [draft, setDraft] = useState<SourcePoolAssignments>(view.assignments)
  useEffect(() => {
    setDraft(Object.fromEntries(JSON.parse(savedKey)))
  }, [savedKey])
  const dirty = manual && assignmentsKey(draft) !== savedKey

  const addressIndex = useMemo(
    () => new Map(view.addresses.map((item, index) => [item.address, index])),
    [view.addresses],
  )
  const actual = useMemo(() => new Map(view.bindings.map(b => [b.mark, b.address])), [view.bindings])
  // Сохранённый адрес, пропавший с интерфейса, остаётся в списке — иначе select молча показал бы «основной IP»
  const absentAddresses = Array.from(new Set(Object.values(draft))).filter(address => !addressIndex.has(address))

  const addressFor = (mark: number): string | undefined => {
    if (manual) return draft[String(mark)]
    return view.enabled ? actual.get(mark) : undefined
  }

  const setMark = (mark: number, address: string) => {
    setDraft(prev => {
      const next = { ...prev }
      if (address) next[String(mark)] = address
      else delete next[String(mark)]
      return next
    })
  }

  const canEdit = editable && !saving
  const assignedCount = manual ? Object.keys(draft).length : actual.size

  const hint = !view.enabled
    ? t('source_pool.layout_off')
    : manual ? t('source_pool.layout_hint_manual') : t('source_pool.layout_hint_auto')

  return (
    <motion.div className="card" initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.08 }}>
      <div className="flex flex-wrap items-center justify-between gap-3 mb-1">
        <h2 className="text-dark-100 font-medium">{t('source_pool.layout_title')}</h2>
        <div className="flex items-center gap-1 bg-dark-800/60 border border-dark-700 rounded-lg p-0.5">
          {MODES.map(mode => {
            const blocked = mode === 'manual' && !view.supports_manual
            return (
              // Обёртка: выключенная кнопка не ловит наведение, и подсказка не показалась бы
              <Tooltip
                key={mode}
                label={t('source_pool.mode_manual_unsupported', { version: view.min_node_version_manual })}
                disabled={!blocked}
              >
                <span className="inline-flex">
                  <button
                    disabled={!canEdit || blocked}
                    onClick={() => mode !== view.mode && onModeChange(mode)}
                    className={`px-3 py-1 rounded-md text-xs font-medium transition-colors disabled:opacity-50 ${view.mode === mode ? 'bg-accent-500 text-white' : 'text-dark-400 hover:text-dark-200'}`}
                  >
                    {t(`source_pool.mode_${mode}`)}
                  </button>
                </span>
              </Tooltip>
            )
          })}
        </div>
      </div>
      <div className="flex flex-wrap items-center justify-between gap-2 mb-3">
        <p className="text-xs text-dark-500">{hint}</p>
        <div className="flex items-center gap-3">
          <span className="text-xs text-dark-500">{t('source_pool.assigned', { count: assignedCount, total: marks.length })}</span>
          {manual && canEdit && view.addresses.length > 0 && (
            <button
              onClick={() => setDraft(roundRobinAssignments(marks, view.addresses.map(item => item.address)))}
              className="btn-tool btn-tool-accent"
            >
              <Shuffle className="w-4 h-4" />
              {t('source_pool.fill_round_robin')}
            </button>
          )}
        </div>
      </div>

      {/* Ширина ячейки — под самый длинный IPv4 (255.255.255.255) со стрелкой select'а */}
      <div className="grid grid-cols-[repeat(auto-fill,minmax(12rem,1fr))] gap-2">
        {marks.map(mark => {
          const address = addressFor(mark)
          const absent = !!address && !addressIndex.has(address)
          return (
            <div
              key={mark}
              title={absent ? `${address} · ${t('source_pool.not_on_interface')}` : undefined}
              className={`flex items-center gap-2 px-2.5 py-1.5 rounded-lg border min-w-0 ${absent ? 'border-warning/40 bg-warning/5' : 'border-dark-700/40 bg-dark-800/40'}`}
            >
              <span className="font-mono text-xs text-dark-400 shrink-0">{mark}</span>
              <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${address ? addressDotClass(addressIndex.get(address)) : 'bg-dark-600'}`} />
              {manual && editable ? (
                <select
                  value={address ?? ''}
                  disabled={saving}
                  onChange={e => setMark(mark, e.target.value)}
                  className="flex-1 min-w-0 bg-transparent text-xs font-mono text-dark-100 focus:outline-none cursor-pointer disabled:opacity-50 [&>option]:bg-dark-900"
                >
                  <option value="">{t('source_pool.main_ip')}</option>
                  {view.addresses.map(item => (
                    <option key={item.address} value={item.address}>{item.address}</option>
                  ))}
                  {absentAddresses.map(item => (
                    <option key={item} value={item}>{item} · {t('source_pool.not_on_interface')}</option>
                  ))}
                </select>
              ) : (
                <span className={`font-mono text-xs truncate ${address ? 'text-dark-100' : 'text-dark-500'}`}>
                  {address ?? t('source_pool.main_ip')}
                </span>
              )}
            </div>
          )
        })}
      </div>

      {dirty && (
        <div className="flex flex-wrap items-center justify-end gap-2 mt-4">
          <span className="text-xs text-warning mr-auto">{t('source_pool.unsaved')}</span>
          <button onClick={() => setDraft(view.assignments)} disabled={saving} className="btn btn-secondary text-xs py-1.5">
            {t('common.cancel')}
          </button>
          <button onClick={() => onSaveAssignments(draft)} disabled={saving} className="btn btn-primary text-xs py-1.5">
            {saving ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Save className="w-3.5 h-3.5" />}
            {t('source_pool.save_layout')}
          </button>
        </div>
      )}
    </motion.div>
  )
}
