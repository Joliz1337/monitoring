// SOCKS5-прокси панели: ip:port или ip:port@login:pass (пароль может содержать ':' и '@').
// Тот же формат проверяет validate_proxy_input на бэкенде
const PROXY_INPUT_RE = /^[^\s:@/]+:\d{1,5}(@[^\s:@/]+:\S+)?$/

export function isValidProxyInput(value: string): boolean {
  return PROXY_INPUT_RE.test(value)
}
