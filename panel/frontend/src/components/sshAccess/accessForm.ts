import type { SshAccessChange, SshProxyMode } from '../../api/client'
import { isValidProxyInput } from '../../utils/proxy'
import { SSH_CREDS_DEFAULTS, type SshCredsValue } from '../servers/SshCredsFields'

export interface AccessFormValue {
  creds: SshCredsValue
  proxy: string
  proxyMode: SshProxyMode
}

// Пустая форма ничего не меняет: пользователь и порт не подставляются, как в окнах одной ноды
export const ACCESS_FORM_BLANK: AccessFormValue = {
  creds: { ...SSH_CREDS_DEFAULTS, port: '', user: '' },
  proxy: '',
  proxyMode: 'fallback',
}

/** Поля формы в тело запроса: уходят только заполненные — для бэка остальное «не менять» */
export function toAccessChange({ creds, proxy, proxyMode }: AccessFormValue): SshAccessChange {
  const change: SshAccessChange = {}
  if (creds.user.trim()) change.ssh_user = creds.user.trim()
  if (creds.port) change.ssh_port = Number(creds.port)

  if (creds.authMethod === 'password') {
    if (creds.password) change.ssh_password = creds.password
  } else if (creds.privateKey.trim()) {
    change.ssh_private_key = creds.privateKey
    if (creds.passphrase) change.ssh_passphrase = creds.passphrase
  }

  if (proxyMode === 'off') {
    change.ssh_proxy_mode = 'off'
  } else if (proxy.trim()) {
    change.ssh_proxy = proxy.trim()
    change.ssh_proxy_mode = proxyMode
  }
  return change
}

export const MAX_SSH_PORT = 65535

/** Что в форме введено неверно: на сервер такое не отправляется */
export function accessFormErrors({ creds, proxy, proxyMode }: AccessFormValue) {
  const port = Number(creds.port)
  return {
    port: creds.port !== '' && (port < 1 || port > MAX_SSH_PORT),
    proxy: proxyMode !== 'off' && proxy.trim() !== '' && !isValidProxyInput(proxy.trim()),
  }
}

export function isAccessFormDirty(form: AccessFormValue): boolean {
  return Object.keys(toAccessChange(form)).length > 0
}
