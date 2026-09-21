import { useEffect, useState } from 'react'
import { DndContext, PointerSensor, closestCenter, useSensor, useSensors, type DragEndEvent } from '@dnd-kit/core'
import { SortableContext, arrayMove, useSortable, verticalListSortingStrategy } from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'
import { ArrowRightLeft, GripVertical, Loader2 } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import type { ExitCandidate, ExitProxyNode } from '../../api/client'
import { useExitProxyStore } from '../../stores/exitProxyStore'
import { formatTimeAgo, getFlag } from '../../utils/format'
import { Toggle } from '../ui/Toggle'
import { Tooltip } from '../ui/Tooltip'
import { CaptchaChip, CheckChip, HealthBadge, KindIcon } from './badges'

const GEMINI_TONE: Record<string, boolean | null> = { ok: true, blocked: false, error: null }

function CandidateRow({ node, candidate, isCurrent, onToggle, onSwitch }: {
  node: ExitProxyNode
  candidate: ExitCandidate
  isCurrent: boolean
  onToggle: () => void
  onSwitch: () => void
}) {
  const { t } = useTranslation()
  const isNodeBusy = useExitProxyStore(s => s.isNodeBusy)
  const busy = isNodeBusy(node.server_id)
  const { setNodeRef, setActivatorNodeRef, attributes, listeners, transform, transition, isDragging } = useSortable({ id: candidate.tag })
  const style = { transform: CSS.Transform.toString(transform), transition, opacity: isDragging ? 0.4 : 1 }
  const canSwitch = candidate.enabled && !isCurrent && node.install_status === 'active' && node.online
  const checks = Object.entries(candidate.checks)

  return (
    <div ref={setNodeRef} style={style} className={`flex flex-wrap items-center gap-2 px-3 py-2 rounded-lg border ${isCurrent ? 'border-accent-500/40 bg-accent-500/5' : 'border-dark-800 bg-dark-900/40'}`}>
      <button ref={setActivatorNodeRef} {...attributes} {...listeners} className="text-dark-600 hover:text-dark-300 cursor-grab touch-none" title={t('exit_proxy.drag_hint')}>
        <GripVertical className="w-4 h-4" />
      </button>
      <KindIcon kind={candidate.kind} />
      <div className="min-w-[140px]">
        <div className="flex items-center gap-1.5 text-sm text-dark-100 font-mono">
          {candidate.label}
          {candidate.primary && <span className="text-[10px] text-dark-500 font-sans">{t('exit_proxy.candidate_primary')}</span>}
          {candidate.managed && <span className="text-[10px] text-dark-500 font-sans">{t('exit_proxy.candidate_managed')}</span>}
        </div>
        <div className="text-[11px] text-dark-500">
          {candidate.country ? <>{getFlag(candidate.country)} {candidate.country}{candidate.country_confirm ? ` · ${candidate.country_confirm}` : ''}</> : '—'}
          {candidate.ip && candidate.kind === 'warp' ? ` · ${candidate.ip}` : ''}
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-1.5 flex-1 min-w-[200px]">
        <HealthBadge healthy={candidate.healthy} enabled={candidate.enabled} />
        {candidate.captcha && <CaptchaChip />}
        {candidate.gemini && candidate.gemini !== 'skipped' && (
          <CheckChip name={t('exit_proxy.check_gemini')} ok={GEMINI_TONE[candidate.gemini] ?? null} detail={t(`exit_proxy.gemini_${candidate.gemini}`)} />
        )}
        {checks.map(([name, check]) => (
          <CheckChip key={name} name={name} ok={check.status === null ? null : check.ok} detail={check.detail} />
        ))}
        {candidate.error && <span className="text-[11px] text-red-400 break-all">{candidate.error}</span>}
      </div>

      <div className="text-[11px] text-dark-500 whitespace-nowrap">
        {candidate.checked_at ? `${t('exit_proxy.checked_at')} ${formatTimeAgo(candidate.checked_at)}` : t('exit_proxy.not_checked')}
      </div>

      <Toggle on={candidate.enabled} onClick={onToggle} disabled={busy} title={t('exit_proxy.candidate_enabled')} />

      {isCurrent ? (
        <span className="text-xs text-accent-400 font-medium whitespace-nowrap">{t('exit_proxy.candidate_current')}</span>
      ) : (
        <Tooltip label={t('exit_proxy.candidate_switch_blocked')} disabled={canSwitch}>
          <button onClick={onSwitch} disabled={!canSwitch || busy} className="btn btn-secondary text-xs py-1 disabled:opacity-40">
            {busy ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <ArrowRightLeft className="w-3.5 h-3.5" />}
            {t('exit_proxy.candidate_switch')}
          </button>
        </Tooltip>
      )}
    </div>
  )
}

// Порядок = приоритет: первый включённый берётся, когда здоровых нет
export default function CandidateList({ node }: { node: ExitProxyNode }) {
  const { t } = useTranslation()
  const updateNode = useExitProxyStore(s => s.updateNode)
  const switchExit = useExitProxyStore(s => s.switchExit)
  const fetchNodes = useExitProxyStore(s => s.fetchNodes)
  const sorted = [...node.candidates].sort((a, b) => a.priority - b.priority)
  const [order, setOrder] = useState<string[]>(sorted.map(c => c.tag))
  const sensors = useSensors(useSensor(PointerSensor, { activationConstraint: { distance: 6 } }))

  const tagsKey = sorted.map(c => c.tag).join('|')
  useEffect(() => {
    setOrder(tagsKey ? tagsKey.split('|') : [])
  }, [tagsKey])

  const byTag = new Map(node.candidates.map(c => [c.tag, c]))
  const items = order.map(tag => byTag.get(tag)).filter((c): c is ExitCandidate => Boolean(c))

  const onDragEnd = async ({ active, over }: DragEndEvent) => {
    if (!over || active.id === over.id) return
    const from = order.indexOf(String(active.id))
    const to = order.indexOf(String(over.id))
    if (from === -1 || to === -1) return
    const next = arrayMove(order, from, to)
    setOrder(next)
    const ok = await updateNode(node.server_id, { candidates_order: next }, `node-${node.server_id}-order`)
    if (ok) toast.success(t('exit_proxy.order_saved'))
    else fetchNodes()
  }

  const toggle = (candidate: ExitCandidate) => {
    const enabledCount = node.candidates.filter(c => c.enabled).length
    if (candidate.enabled && enabledCount <= 1) {
      toast.error(t('exit_proxy.candidate_last_enabled'))
      return
    }
    const disabled = node.candidates.filter(c => !c.enabled).map(c => c.tag)
    const next = candidate.enabled ? [...disabled, candidate.tag] : disabled.filter(tag => tag !== candidate.tag)
    updateNode(node.server_id, { candidates_disabled: next }, `node-${node.server_id}-cand-${candidate.tag}`)
  }

  const switchTo = (candidate: ExitCandidate) => {
    if (!window.confirm(t('exit_proxy.confirm_switch', { name: node.name, label: candidate.label }))) return
    switchExit(node.server_id, candidate.tag)
  }

  if (items.length === 0) {
    return <p className="text-xs text-dark-500">{t('exit_proxy.no_candidates')}</p>
  }

  return (
    <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={onDragEnd}>
      <SortableContext items={order} strategy={verticalListSortingStrategy}>
        <div className="space-y-1.5">
          {items.map(candidate => (
            <CandidateRow
              key={candidate.tag}
              node={node}
              candidate={candidate}
              isCurrent={node.current_exit?.tag === candidate.tag}
              onToggle={() => toggle(candidate)}
              onSwitch={() => switchTo(candidate)}
            />
          ))}
        </div>
      </SortableContext>
    </DndContext>
  )
}
