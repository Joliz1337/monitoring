import { createContext, useContext, useId, type ReactNode } from 'react'

// id подписи строки — контролы внутри берут его как aria-labelledby
const SettingRowLabelContext = createContext<string | undefined>(undefined)

export const useSettingRowLabelId = () => useContext(SettingRowLabelContext)

interface SettingRowProps {
  label: string
  hint?: ReactNode
  htmlFor?: string
  children: ReactNode
}

export function SettingRow({ label, hint, htmlFor, children }: SettingRowProps) {
  const id = useId()
  const labelClass = 'block text-sm font-medium text-dark-200'

  return (
    <SettingRowLabelContext.Provider value={id}>
      <div className="flex flex-col gap-2.5 py-4 first:pt-0 last:pb-0 border-t border-dark-800/50 first:border-0 sm:flex-row sm:items-center sm:justify-between sm:gap-6">
        <div className="min-w-0">
          {htmlFor
            ? <label id={id} htmlFor={htmlFor} className={labelClass}>{label}</label>
            : <span id={id} className={labelClass}>{label}</span>}
          {hint && <p className="text-xs text-dark-500 mt-0.5">{hint}</p>}
        </div>
        <div className="shrink-0 min-w-0">{children}</div>
      </div>
    </SettingRowLabelContext.Provider>
  )
}
