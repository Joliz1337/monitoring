import type { ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { WifiOff } from 'lucide-react'

/**
 * Плашка страницы сервера, нода которого молчит: всё ниже — последнее, что панель
 * от неё получила. Текст задаёт страница: у одних данные только для чтения,
 * у других правки копятся в панели и доезжают, когда нода вернётся.
 */
export default function SilentNodeBanner({ children }: { children?: ReactNode }) {
  const { t } = useTranslation()
  return (
    <div className="flex items-center gap-3 p-4 bg-warning/10 border border-warning/30 rounded-xl mb-6">
      <div className="flex-shrink-0 p-2 bg-warning/20 rounded-lg">
        <WifiOff className="w-5 h-5 text-warning" />
      </div>
      <div className="min-w-0">
        <div className="text-warning font-medium">{t('node_silent.title')}</div>
        {children && <div className="text-warning/70 text-sm mt-1">{children}</div>}
      </div>
    </div>
  )
}
