import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Filter } from 'lucide-react'
import type { SpreadLevel } from './quarantineSpread'
import {
  FACETS,
  UNKNOWN_OWNER,
  type FacetKey,
  type FacetOption,
  type QuarantineState,
  type WorstState,
} from './targetFilters'

// Нод-владельцев у парка сотни: в ряду — самые «больные», остальные по кнопке
const OWNERS_SHOWN = 8

const GROUP_LABELS: Record<FacetKey, string> = {
  worst: 'loss.col_worst',
  spread: 'loss.col_relays',
  quarantine: 'loss.filter_quarantine',
  owner: 'loss.filter_owner',
}

// Для готовых значений — ключи переводов, на кнопке — уже текст
interface ChipText {
  label: string
  hint?: string
}

const WORST_TEXT: Record<WorstState, ChipText> = {
  no_reply: { label: 'loss_probe.no_reply', hint: 'loss_probe.no_reply_tooltip' },
  lossy: { label: 'loss.filter_lossy' },
  clean: { label: 'loss.filter_clean' },
}

const SPREAD_TEXT: Record<SpreadLevel, ChipText> = {
  all: { label: 'loss.quarantine_spread_all', hint: 'loss.filter_spread_all_hint' },
  most: { label: 'loss.filter_spread_most', hint: 'loss.filter_spread_most_hint' },
  limited: { label: 'loss.filter_spread_limited', hint: 'loss.filter_spread_limited_hint' },
}

const QUARANTINE_TEXT: Record<QuarantineState, ChipText> = {
  disabled: { label: 'loss.filter_disabled', hint: 'loss.filter_disabled_hint' },
  active: { label: 'loss.filter_not_disabled' },
}

const FIXED_TEXT: Record<Exclude<FacetKey, 'owner'>, Record<string, ChipText>> = {
  worst: WORST_TEXT,
  spread: SPREAD_TEXT,
  quarantine: QUARANTINE_TEXT,
}

const chipCls = 'px-2 py-0.5 rounded-md border text-xs whitespace-nowrap transition-colors'
const idleCls = 'border-dark-700/70 text-dark-300 hover:border-dark-600 hover:text-dark-100 disabled:opacity-40 disabled:hover:border-dark-700/70 disabled:hover:text-dark-300'
const activeCls = 'bg-accent-500/15 text-accent-300 border-accent-500/40'
const linkCls = 'px-1.5 py-0.5 rounded-md text-xs text-accent-400 hover:bg-accent-500/10 transition-colors'

interface Props {
  options: Record<FacetKey, FacetOption[]>
  shown: number
  total: number
  onToggle: (facet: FacetKey, value: string) => void
  onReset: () => void
}

export default function TargetFilterBar({ options, shown, total, onToggle, onReset }: Props) {
  const { t } = useTranslation()
  const [allOwners, setAllOwners] = useState(false)

  const isActive = (facet: FacetKey) => options[facet].some(option => option.active)
  // Группа из одного значения ничего не отбирает — она появляется, только если в ней что-то нажато
  const groups = FACETS.filter(facet => options[facet].length > 1 || isActive(facet))
  if (groups.length === 0) return null

  const textOf = (facet: FacetKey, value: string): ChipText => {
    if (facet === 'owner') return { label: value === UNKNOWN_OWNER ? t('loss.owner_unknown') : value }
    const { label, hint } = FIXED_TEXT[facet][value]
    return { label: t(label), hint: hint && t(hint) }
  }

  const chipsOf = (facet: FacetKey): FacetOption[] => {
    if (facet !== 'owner' || allOwners) return options[facet]
    return options[facet].filter((option, index) => index < OWNERS_SHOWN || option.active)
  }

  return (
    <div
      role="group"
      aria-label={t('loss.filters')}
      className="flex items-center gap-x-4 gap-y-2 flex-wrap px-4 py-2.5 border-b border-dark-800/50"
    >
      <Filter className="w-3.5 h-3.5 text-dark-500 shrink-0" aria-hidden />
      {groups.map((facet, index) => {
        const chips = chipsOf(facet)
        const hidden = options[facet].length - chips.length
        return (
          <div
            key={facet}
            role="group"
            aria-label={t(GROUP_LABELS[facet])}
            className={`flex items-center gap-1.5 flex-wrap ${index > 0 ? 'pl-4 border-l border-dark-700/60' : ''}`}
          >
            {chips.map(option => {
              const { label, hint } = textOf(facet, option.value)
              return (
                <button
                  key={option.value}
                  type="button"
                  title={hint}
                  aria-pressed={option.active}
                  disabled={option.count === 0 && !option.active}
                  onClick={() => onToggle(facet, option.value)}
                  className={`${chipCls} ${option.active ? activeCls : idleCls}`}
                >
                  {label}
                  {' '}
                  <span className="opacity-60">{option.count}</span>
                </button>
              )
            })}
            {/* Одна кнопка на оба состояния: после нажатия фокус остаётся на ней */}
            {facet === 'owner' && (hidden > 0 || (allOwners && options.owner.length > OWNERS_SHOWN)) && (
              <button type="button" onClick={() => setAllOwners(shown => !shown)} className={linkCls}>
                {allOwners ? t('loss.quarantine_show_less') : t('loss.filter_more', { count: hidden })}
              </button>
            )}
          </div>
        )
      })}
      {FACETS.some(isActive) && (
        <div className="ml-auto flex items-center gap-2 text-xs text-dark-500 whitespace-nowrap">
          <span>{t('loss.filter_shown', { shown, total })}</span>
          <button type="button" onClick={onReset} className={linkCls}>
            {t('loss.filter_reset')}
          </button>
        </div>
      )}
    </div>
  )
}
