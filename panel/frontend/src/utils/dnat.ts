import type { DnatProtocol, DnatRuleData } from '../api/client'

type ListenPorts = Pick<DnatRuleData, 'listen_port' | 'listen_port_end'>

/** «443» / «20000-30000» — что слушает нода */
export function formatListen(rule: ListenPorts): string {
  return rule.listen_port_end ? `${rule.listen_port}-${rule.listen_port_end}` : String(rule.listen_port)
}

/** Список IP назначения: «10.0.0.2,10.0.0.3» → ['10.0.0.2', '10.0.0.3'] */
export function splitTargets(targetIp: string): string[] {
  return targetIp.split(',').map(part => part.trim()).filter(Boolean)
}

/** «10.0.0.2:8443» или «10.0.0.2 / 10.0.0.3:8443»; при target_port = 0 порт сохраняется, поэтому показываем входящий */
export function formatTarget(rule: ListenPorts & Pick<DnatRuleData, 'target_ip' | 'target_port'>): string {
  const host = splitTargets(rule.target_ip).join(' / ')
  if (rule.target_port) return `${host}:${rule.target_port}`
  return `${host}:${formatListen(rule)}`
}

export function protocolLabel(protocol: DnatProtocol): string {
  return protocol === 'both' ? 'TCP+UDP' : protocol.toUpperCase()
}
