import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { PauseCircle, ShieldOff } from 'lucide-react'
import type { QuarantineOverview } from '../../api/client'
import { Checkbox } from '../ui/Checkbox'
import { readStorage, writeStorage } from '../../utils/storage'
import QuarantineNodeCard from './QuarantineNodeCard'
import type { AddressActions } from './QuarantineAddressRow'
import { groupByNode } from './quarantineGroups'
import { formatSpan } from './quarantineText'

const MERGE_PORTS_KEY = 'loss_quarantine_merge_ports'

interface Props extends AddressActions {
  view: QuarantineOverview | null
}

export default function QuarantineBlock({ view, ...actions }: Props) {
  const { t } = useTranslation()
  const [mergePorts, setMergePorts] = useState(() => readStorage(MERGE_PORTS_KEY) === '1')
  const nodes = useMemo(() => (view ? groupByNode(view, mergePorts) : []), [view, mergePorts])
  if (!view) return null

  const nothingToShow = nodes.length === 0 && view.paused_relays.length === 0
  if (view.mode === 'off' && nothingToShow) return null

  const observing = view.mode === 'observe'
  const addressCount = nodes.reduce((total, node) => total + node.addresses.length, 0)
  const now = Date.now() / 1000

  const toggleMergePorts = (checked: boolean) => {
    setMergePorts(checked)
    writeStorage(MERGE_PORTS_KEY, checked ? '1' : '0')
  }

  return (
    <section className="space-y-3">
      <div className="flex items-center gap-2 flex-wrap">
        <ShieldOff className="w-4 h-4 text-warning" />
        <h2 className="text-sm font-medium text-dark-200">
          {t(observing ? 'loss.quarantine_title_observe' : 'loss.quarantine_title')}
        </h2>
        {addressCount > 0 && (
          <>
            <span className="px-2 py-px rounded-full bg-dark-800 text-2xs text-dark-300">{addressCount}</span>
            <label
              title={t('loss.quarantine_merge_ports_hint')}
              className="ml-auto flex items-center gap-2 text-xs text-dark-400 cursor-pointer"
            >
              <Checkbox checked={mergePorts} onChange={e => toggleMergePorts(e.target.checked)} />
              {t('loss.quarantine_merge_ports')}
            </label>
          </>
        )}
      </div>

      {view.paused_relays.length > 0 && (
        <div className="px-4 py-2.5 space-y-1.5 rounded-xl border border-warning/20 bg-warning/5">
          {view.paused_relays.map(relay => (
            <div key={relay.server_id} className="flex items-center gap-2 text-xs text-warning">
              <PauseCircle className="w-3.5 h-3.5 shrink-0" />
              {t('loss.quarantine_paused', { name: relay.name, span: formatSpan(now - relay.since, t) })}
            </div>
          ))}
        </div>
      )}

      {nothingToShow && (
        <p className="px-4 py-4 rounded-xl border border-dark-800/50 bg-dark-900/50 text-sm text-dark-500">
          {t(observing ? 'loss.quarantine_empty_observe' : 'loss.quarantine_empty')}
        </p>
      )}

      {nodes.length > 0 && (
        <div className="grid gap-4 items-start grid-cols-[repeat(auto-fill,minmax(min(100%,28rem),1fr))]">
          {nodes.map(node => (
            <QuarantineNodeCard
              key={node.owner ?? ''}
              node={node}
              mergePorts={mergePorts}
              now={now}
              {...actions}
            />
          ))}
        </div>
      )}
    </section>
  )
}
