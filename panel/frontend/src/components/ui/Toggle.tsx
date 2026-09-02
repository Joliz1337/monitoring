interface ToggleProps {
  on: boolean
  onClick: () => void
  disabled?: boolean
  title?: string
}

export function Toggle({ on, onClick, disabled, title }: ToggleProps) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={title}
      aria-pressed={on}
      className={`relative w-10 h-5 rounded-full transition-colors shrink-0 ${on ? 'bg-accent-500' : 'bg-dark-700'} ${disabled ? 'opacity-50 cursor-not-allowed' : ''}`}
    >
      <div className={`absolute top-0.5 left-0.5 w-4 h-4 rounded-full bg-white transition-transform ${on ? 'translate-x-5' : ''}`} />
    </button>
  )
}
