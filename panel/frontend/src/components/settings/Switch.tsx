import { useSettingRowLabelId } from './SettingRow'

interface SwitchProps {
  checked: boolean
  onChange: (checked: boolean) => void
  disabled?: boolean
  'aria-label'?: string
  className?: string
}

export function Switch({ checked, onChange, disabled, 'aria-label': ariaLabel, className = '' }: SwitchProps) {
  const labelId = useSettingRowLabelId()

  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={ariaLabel}
      aria-labelledby={ariaLabel ? undefined : labelId}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={`relative inline-flex w-11 h-6 shrink-0 rounded-full transition-colors
                  focus:outline-none focus-visible:ring-2 focus-visible:ring-accent-500/40
                  disabled:opacity-50 disabled:cursor-not-allowed
                  ${checked ? 'bg-accent-500' : 'bg-dark-600'} ${className}`}
    >
      <span
        className={`absolute top-0.5 left-0.5 w-5 h-5 rounded-full bg-white shadow-md
                    transition-transform motion-reduce:transition-none ${checked ? 'translate-x-5' : ''}`}
      />
    </button>
  )
}
