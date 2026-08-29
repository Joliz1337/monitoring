import { useId, type KeyboardEvent, type ReactNode } from 'react'
import { LayoutGroup, motion, useReducedMotion } from 'framer-motion'
import { useSettingRowLabelId } from './SettingRow'

export interface SegmentedOption<T extends string | number> {
  value: T
  label: ReactNode
  hint?: string
}

interface SegmentedControlProps<T extends string | number> {
  value: T
  options: readonly SegmentedOption<T>[]
  onChange: (value: T) => void
  size?: 'sm' | 'md'
  'aria-label'?: string
  className?: string
}

const PILL_TRANSITION = { type: 'spring', stiffness: 400, damping: 30 } as const

export function SegmentedControl<T extends string | number>({
  value, options, onChange, size = 'md', 'aria-label': ariaLabel, className = '',
}: SegmentedControlProps<T>) {
  const groupId = useId()
  const labelId = useSettingRowLabelId()
  const reduceMotion = useReducedMotion()

  const select = (next: T) => {
    if (next !== value) onChange(next)
  }

  // Стрелки переключают по кругу и переносят фокус — roving tabindex
  const handleKeyDown = (e: KeyboardEvent<HTMLButtonElement>) => {
    const delta = e.key === 'ArrowRight' || e.key === 'ArrowDown' ? 1
      : e.key === 'ArrowLeft' || e.key === 'ArrowUp' ? -1 : 0
    if (!delta) return
    e.preventDefault()
    const current = options.findIndex(o => o.value === value)
    const nextIndex = (current + delta + options.length) % options.length
    select(options[nextIndex].value)
    const buttons = e.currentTarget.parentElement?.querySelectorAll<HTMLButtonElement>('[role="radio"]')
    buttons?.[nextIndex]?.focus()
  }

  const padding = size === 'sm' ? 'px-2.5 py-1 text-xs' : 'px-3 py-1.5 text-sm'

  return (
    <LayoutGroup id={groupId}>
      <div
        role="radiogroup"
        aria-label={ariaLabel}
        aria-labelledby={ariaLabel ? undefined : labelId}
        className={`inline-flex flex-wrap gap-1 p-1 rounded-xl bg-dark-800/60 border border-dark-700/50 ${className}`}
      >
        {options.map(option => {
          const active = option.value === value
          return (
            <button
              key={String(option.value)}
              type="button"
              role="radio"
              aria-checked={active}
              tabIndex={active ? 0 : -1}
              onClick={() => select(option.value)}
              onKeyDown={handleKeyDown}
              className={`relative rounded-lg font-medium transition-colors
                          focus:outline-none focus-visible:ring-2 focus-visible:ring-accent-500/40
                          ${padding} ${active ? 'text-dark-950' : 'text-dark-400 hover:text-dark-200'}`}
            >
              {active && (
                <motion.span
                  layoutId={`${groupId}-pill`}
                  className="absolute inset-0 rounded-lg bg-gradient-to-r from-accent-500 to-accent-600 shadow-md shadow-accent-500/20"
                  transition={reduceMotion ? { duration: 0 } : PILL_TRANSITION}
                />
              )}
              <span className="relative z-10 flex flex-col items-center gap-0.5">
                <span className="flex items-center gap-1.5 whitespace-nowrap">{option.label}</span>
                {option.hint && (
                  <span className={`text-xs font-normal max-w-[11rem] ${active ? 'text-dark-900/70' : 'text-dark-500'}`}>
                    {option.hint}
                  </span>
                )}
              </span>
            </button>
          )
        })}
      </div>
    </LayoutGroup>
  )
}
