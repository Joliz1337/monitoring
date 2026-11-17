import { useTranslation } from 'react-i18next'
import type { SshProxyMode } from '../../api/client'
import { SecretInput } from '../ui/SecretInput'

const MODES: SshProxyMode[] = ['fallback', 'always', 'off']

interface Props {
  proxy: string
  mode: SshProxyMode
  onChange: (patch: { proxy?: string; proxyMode?: SshProxyMode }) => void
  disabled?: boolean
}

/** SOCKS5-прокси только для SSH и когда им пользоваться; в адресе может быть пароль — поле маскируется */
export default function SshProxyFields({ proxy, mode, onChange, disabled = false }: Props) {
  const { t } = useTranslation()

  return (
    <div className="space-y-2">
      <label className="block text-xs text-dark-400">{t('sshAccess.proxy')}</label>
      <SecretInput
        className="input w-full font-mono text-xs"
        placeholder={t('sshAccess.proxy_placeholder')}
        value={proxy}
        onChange={(e) => onChange({ proxy: e.target.value })}
        disabled={disabled || mode === 'off'}
        revealed={!proxy.includes('@')}
      />
      <div className="flex gap-x-4 gap-y-1 flex-wrap text-sm">
        {MODES.map(option => (
          <label key={option} className="flex items-center gap-1.5 cursor-pointer">
            <input
              type="radio"
              name="ssh-access-proxy-mode"
              checked={mode === option}
              onChange={() => onChange({ proxyMode: option })}
              disabled={disabled}
            />
            {t(`sshAccess.mode_${option}`)}
          </label>
        ))}
      </div>
    </div>
  )
}
