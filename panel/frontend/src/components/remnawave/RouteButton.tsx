import { useTranslation } from 'react-i18next'
import { Route } from 'lucide-react'
import { Tooltip } from '../ui/Tooltip'

interface RouteButtonProps {
  // Через релей нода видит адрес релея — трасса до него ничего не скажет о клиенте
  relay: boolean
  disabled: boolean
  onClick: () => void
}

export default function RouteButton({ relay, disabled, onClick }: RouteButtonProps) {
  const { t } = useTranslation()
  const blocked = relay || disabled
  return (
    <Tooltip label={relay ? t('remnawave.geo.route_unavailable_relay') : t('remnawave.geo.route_button')}>
      <button
        type="button"
        disabled={blocked}
        onClick={e => {
          e.stopPropagation()
          onClick()
        }}
        aria-label={t('remnawave.geo.route_button')}
        className="text-dark-500 hover:text-accent-400 transition-colors disabled:opacity-40 disabled:hover:text-dark-500 disabled:cursor-not-allowed"
      >
        <Route className="w-3.5 h-3.5" />
      </button>
    </Tooltip>
  )
}
