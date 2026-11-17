import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { PauseCircle, ShieldOff } from 'lucide-react'
import type { LossTarget, QuarantineOverview } from '../../api/client'
import { Checkbox } from '../ui/Checkbox'
import { readStorage, writeStorage } from '../../utils/storage'
import QuarantineCard from './QuarantineCard'
import type { AddressActions } from './QuarantineAddressRow'
import { countAddresses, groupByNode, groupByRelay, type GroupBy } from './quarantineGroups'
import { indexProbes } from './quarantineSpread'
import { formatSpan } from './quarantineText'

const MERGE_PORTS_KEY = 'loss_quarantine_merge_ports'
const GROUP_BY_KEY = 'loss_quarantine_group_by'
const GROUPINGS: GroupBy[] = ['node', 'relay']
// Мёртвый адрес отключён на каждом релее парка — карточек релеев бывают тысячи
const CARDS_SHOWN = 30

interface Props extends AddressActions {
  view: QuarantineOverview | null
  // Таблица потерь той же страницы: по ней видно, на скольких релеях из всех адрес не работает
  targets: LossTarget[] | null
  // Порог потерь автоотключения; null, пока настройки не загружены
  lossThreshold: number | null
}

export default function QuarantineBlock({ view, targets, lossThreshold, ...actions }: Props) {
  const { t } = useTranslation()
  const [mergePorts, setMergePorts] = useState(() => readStorage(MERGE_PORTS_KEY) === '1')
  const [groupBy, setGroupBy] = useState<GroupBy>(() => (readStorage(GROUP_BY_KEY) === 'relay' ? 'relay' : 'node'))
  const [showAllCards, setShowAllCards] = useState(false)
  const byRelay = groupBy === 'relay'
  const cards = useMemo(() => {
    if (!view) return []
    return byRelay ? groupByRelay(view, mergePorts) : groupByNode(view, mergePorts)
  }, [view, mergePorts, byRelay])
  const addressCount = useMemo(() => (view ? countAddresses(view, mergePorts) : 0), [view, mergePorts])
  const probes = useMemo(() => (targets ? indexProbes(targets) : null), [targets])
  if (!view) return null

  const nothingToShow = addressCount === 0 && view.paused_relays.length === 0
  if (view.mode === 'off' && nothingToShow) return null

  const observing = view.mode === 'observe'
  const shownCards = showAllCards ? cards : cards.slice(0, CARDS_SHOWN)
  const now = Date.now() / 1000

  const toggleMergePorts = (checked: boolean) => {
    setMergePorts(checked)
    writeStorage(MERGE_PORTS_KEY, checked ? '1' : '0')
  }

  const chooseGroupBy = (mode: GroupBy) => {
    setGroupBy(mode)
    setShowAllCards(false)
    writeStorage(GROUP_BY_KEY, mode)
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
            <div className="ml-auto flex items-center gap-3 flex-wrap">
              <div
                role="radiogroup"
                aria-label={t('loss.quarantine_group_by')}
                className="inline-flex p-0.5 rounded-lg border border-dark-800 text-xs"
              >
                {GROUPINGS.map(mode => (
                  <button
                    key={mode}
                    type="button"
                    role="radio"
                    aria-checked={groupBy === mode}
                    title={t(`loss.quarantine_group_${mode}_hint`)}
                    onClick={() => chooseGroupBy(mode)}
                    className={`px-2.5 py-1 rounded-md transition-colors ${
                      groupBy === mode ? 'bg-accent-500/15 text-accent-300' : 'text-dark-400 hover:text-dark-200'
                    }`}
                  >
                    {t(`loss.quarantine_group_${mode}`)}
                  </button>
                ))}
              </div>
              <label
                title={t('loss.quarantine_merge_ports_hint')}
                className="flex items-center gap-2 text-xs text-dark-400 cursor-pointer"
              >
                <Checkbox checked={mergePorts} onChange={e => toggleMergePorts(e.target.checked)} />
                {t('loss.quarantine_merge_ports')}
              </label>
            </div>
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

      {addressCount > 0 && cards.length === 0 && (
        <p className="px-4 py-4 rounded-xl border border-dark-800/50 bg-dark-900/50 text-sm text-dark-500">
          {t('loss.quarantine_empty_relays')}
        </p>
      )}

      {cards.length > 0 && (
        <div className="grid gap-4 items-start grid-cols-[repeat(auto-fill,minmax(min(100%,28rem),1fr))]">
          {shownCards.map(card => (
            <QuarantineCard
              key={card.key}
              group={card}
              byRelay={byRelay}
              mergePorts={mergePorts}
              now={now}
              probes={probes}
              lossThreshold={lossThreshold}
              {...actions}
            />
          ))}
        </div>
      )}

      {cards.length > CARDS_SHOWN && (
        <button
          type="button"
          onClick={() => setShowAllCards(prev => !prev)}
          className="w-full px-4 py-2 rounded-xl border border-dark-800/50 text-xs text-accent-400 hover:bg-accent-500/10 transition-colors"
        >
          {showAllCards ? t('loss.quarantine_show_less') : t('loss.quarantine_show_all', { count: cards.length })}
        </button>
      )}
    </section>
  )
}
