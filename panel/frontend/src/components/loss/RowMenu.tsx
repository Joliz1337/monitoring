import { useEffect, useRef, useState } from 'react'
import { Loader2, MoreHorizontal, type LucideIcon } from 'lucide-react'

export interface MenuAction {
  label: string
  hint?: string
  icon: LucideIcon
  onSelect: () => void
}

interface Props {
  label: string
  actions: MenuAction[]
  busy?: boolean
}

export default function RowMenu({ label, actions, busy = false }: Props) {
  const [open, setOpen] = useState(false)
  const rootRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const closeOnOutsideClick = (event: MouseEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false)
    }
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', closeOnOutsideClick)
    document.addEventListener('keydown', closeOnEscape)
    return () => {
      document.removeEventListener('mousedown', closeOnOutsideClick)
      document.removeEventListener('keydown', closeOnEscape)
    }
  }, [open])

  if (actions.length === 0) return null

  return (
    <div ref={rootRef} className="relative shrink-0">
      <button
        type="button"
        aria-label={label}
        aria-haspopup="menu"
        aria-expanded={open}
        disabled={busy}
        onClick={() => setOpen(prev => !prev)}
        className="w-7 h-7 rounded-lg flex items-center justify-center text-dark-400 hover:text-dark-100 hover:bg-dark-800/70 transition-colors disabled:opacity-40"
      >
        {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : <MoreHorizontal className="w-4 h-4" />}
      </button>
      {open && (
        <div
          role="menu"
          className="absolute right-0 top-full z-20 mt-1 min-w-[13rem] p-1.5 rounded-[10px] border border-dark-800 bg-dark-900 shadow-2xl shadow-black/60"
        >
          {actions.map(action => (
            <button
              key={action.label}
              type="button"
              role="menuitem"
              title={action.hint}
              onClick={() => {
                setOpen(false)
                action.onSelect()
              }}
              className="w-full flex items-center gap-2.5 px-2.5 py-1.5 rounded-md text-xs text-left text-dark-200 hover:text-dark-100 hover:bg-dark-800/70 transition-colors"
            >
              <action.icon className="w-4 h-4 shrink-0 text-dark-400" />
              {action.label}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
