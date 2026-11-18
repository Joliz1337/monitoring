import type { ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import type { PacketLossScope } from '../../api/client'

// От самого подробного к самому тихому
const SCOPES: PacketLossScope[] = ['all', 'wide', 'dead']

interface Props {
  scope: PacketLossScope
  // Сохранённая доля релеев, %: её называет подсказка уровня
  share: number
  onChange: (next: PacketLossScope) => void
  // Ползунок доли: на уровне «Все события» она ничего не решает и не показывается
  children: ReactNode
}

export default function LossScopeChoice({ scope, share, onChange, children }: Props) {
  const { t } = useTranslation()

  return (
    <div className="space-y-2">
      <span className="block text-sm text-dark-300">{t('alerts.packet_loss_scope')}</span>
      <div role="radiogroup" aria-label={t('alerts.packet_loss_scope')} className="flex gap-2 flex-wrap">
        {SCOPES.map(option => (
          <button
            key={option}
            type="button"
            role="radio"
            aria-checked={scope === option}
            onClick={() => { if (option !== scope) onChange(option) }}
            className={`px-3 py-1.5 rounded-lg text-sm border transition-colors ${
              scope === option
                ? 'bg-accent-500/15 text-accent-300 border-accent-500/40'
                : 'text-dark-400 border-dark-700 hover:text-dark-200'
            }`}
          >
            {t(`alerts.packet_loss_scope_${option}`)}
          </button>
        ))}
      </div>
      <p className="text-xs text-dark-500">{t(`alerts.packet_loss_scope_${scope}_hint`, { share })}</p>
      {scope !== 'all' && children}
    </div>
  )
}
