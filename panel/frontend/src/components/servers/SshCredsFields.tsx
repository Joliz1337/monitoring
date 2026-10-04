import { useTranslation } from 'react-i18next'
import type { ImageDeliveryCreds, ImageDeliverySettings } from '../../api/client'
import { SecretInput } from '../ui/SecretInput'

export interface SshCredsValue {
  host: string
  port: string
  user: string
  authMethod: 'password' | 'key'
  password: string
  privateKey: string
  passphrase: string
}

export const SSH_CREDS_DEFAULTS: SshCredsValue = {
  host: '',
  port: '22',
  user: 'root',
  authMethod: 'password',
  password: '',
  privateKey: '',
  passphrase: '',
}

/** Сохранённый у сервера SSH-доступ → поля формы; секреты бэк не отдаёт, они остаются пустыми */
export function credsFromSettings(settings: ImageDeliverySettings): SshCredsValue {
  return {
    ...SSH_CREDS_DEFAULTS,
    host: settings.ssh_host || '',
    port: String(settings.ssh_port || 22),
    user: settings.ssh_user || 'root',
    authMethod: settings.has_ssh_private_key ? 'key' : 'password',
  }
}

export function hasStoredSshSecret(settings: ImageDeliverySettings): boolean {
  return settings.has_ssh_password || settings.has_ssh_private_key
}

export function hasSshSecret(value: SshCredsValue): boolean {
  return value.authMethod === 'password' ? !!value.password : !!value.privateKey
}

/** Поля формы в тело запроса. Пустой пароль/ключ не шлём: для бэка это «оставить сохранённый» */
export function toDeliveryCreds(value: SshCredsValue): ImageDeliveryCreds {
  const creds: ImageDeliveryCreds = {
    ssh_port: Number(value.port) || 22,
    ssh_user: value.user.trim() || 'root',
  }
  if (value.host.trim()) creds.ssh_host = value.host.trim()
  if (value.authMethod === 'password') {
    if (value.password) creds.ssh_password = value.password
  } else {
    if (value.privateKey) creds.ssh_private_key = value.privateKey
    if (value.passphrase) creds.ssh_passphrase = value.passphrase
  }
  return creds
}

interface Props {
  value: SshCredsValue
  onChange: (patch: Partial<SshCredsValue>) => void
  disabled?: boolean
  /** Массовому запуску хост не нужен — у каждого сервера свой */
  showHost?: boolean
}

export default function SshCredsFields({ value, onChange, disabled = false, showHost = true }: Props) {
  const { t } = useTranslation()

  const hostField = (
    <>
      <label className="block text-xs text-dark-400 mb-1">{t('imageDelivery.host')}</label>
      <input className="input w-full" value={value.host} onChange={(e) => onChange({ host: e.target.value })} disabled={disabled} />
    </>
  )
  const userField = (
    <>
      <label className="block text-xs text-dark-400 mb-1">{t('imageDelivery.user')}</label>
      <input className="input w-full" value={value.user} onChange={(e) => onChange({ user: e.target.value })} disabled={disabled} />
    </>
  )

  return (
    <div className="space-y-3">
      <div className="grid grid-cols-3 gap-3">
        <div className="col-span-2">{showHost ? hostField : userField}</div>
        <div>
          <label className="block text-xs text-dark-400 mb-1">{t('imageDelivery.port')}</label>
          <input className="input w-full" value={value.port} onChange={(e) => onChange({ port: e.target.value.replace(/\D/g, '') })} disabled={disabled} />
        </div>
      </div>
      {showHost && <div>{userField}</div>}
      <div className="flex gap-4 text-sm">
        <label className="flex items-center gap-1.5 cursor-pointer">
          <input type="radio" checked={value.authMethod === 'password'} onChange={() => onChange({ authMethod: 'password' })} disabled={disabled} />
          {t('imageDelivery.auth_password')}
        </label>
        <label className="flex items-center gap-1.5 cursor-pointer">
          <input type="radio" checked={value.authMethod === 'key'} onChange={() => onChange({ authMethod: 'key' })} disabled={disabled} />
          {t('imageDelivery.auth_key')}
        </label>
      </div>
      {value.authMethod === 'password' ? (
        <SecretInput className="input w-full" placeholder={t('imageDelivery.password')} value={value.password} onChange={(e) => onChange({ password: e.target.value })} disabled={disabled} />
      ) : (
        <>
          <textarea className="input w-full font-mono text-xs" rows={4} placeholder={t('imageDelivery.private_key')} value={value.privateKey} onChange={(e) => onChange({ privateKey: e.target.value })} disabled={disabled} />
          <SecretInput className="input w-full" placeholder={t('imageDelivery.passphrase')} value={value.passphrase} onChange={(e) => onChange({ passphrase: e.target.value })} disabled={disabled} />
        </>
      )}
    </div>
  )
}
