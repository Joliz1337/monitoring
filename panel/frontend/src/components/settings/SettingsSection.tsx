import type { ReactNode } from 'react'
import type { LucideIcon } from 'lucide-react'
import { FAQIcon, type FAQScreen } from '../FAQ'

// Общая анимация входа/выхода корня вкладки настроек
export const TAB_MOTION = {
  initial: { opacity: 0, y: 12 },
  animate: { opacity: 1, y: 0 },
  exit: { opacity: 0, y: -12 },
  transition: { duration: 0.2 },
}

interface SettingsSectionProps {
  title: string
  description?: string
  icon: LucideIcon
  faq?: FAQScreen
  right?: ReactNode
  children: ReactNode
  className?: string
}

export function SettingsSection({ title, description, icon: Icon, faq, right, children, className = '' }: SettingsSectionProps) {
  return (
    <section className={`card ${className}`}>
      <div className="flex items-start gap-3 mb-4">
        <div className="w-10 h-10 rounded-xl bg-accent-500/10 border border-accent-500/20 flex items-center justify-center shrink-0">
          <Icon className="w-5 h-5 text-accent-400" />
        </div>
        <div className="flex-1 min-w-0">
          <h2 className="font-semibold text-dark-100 flex items-center gap-2">
            {title}
            {faq && <FAQIcon screen={faq} size="sm" />}
          </h2>
          {description && <p className="text-sm text-dark-500 mt-0.5">{description}</p>}
        </div>
        {right && <div className="flex items-center gap-2 shrink-0">{right}</div>}
      </div>
      {children}
    </section>
  )
}
