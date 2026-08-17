import type { DnatProtocol, DnatRuleData } from '../api/client'

type ListenPorts = Pick<DnatRuleData, 'listen_port' | 'listen_port_end'>

/** «443» / «20000-30000» — что слушает нода */
export function formatListen(rule: ListenPorts): string {
  return rule.listen_port_end ? `${rule.listen_port}-${rule.listen_port_end}` : String(rule.listen_port)
}

/** «10.0.0.2:8443»; при target_port = 0 порт сохраняется, поэтому показываем входящий */
export function formatTarget(rule: ListenPorts & Pick<DnatRuleData, 'target_ip' | 'target_port'>): string {
  if (rule.target_port) return `${rule.target_ip}:${rule.target_port}`
  return `${rule.target_ip}:${formatListen(rule)}`
}

export function protocolLabel(protocol: DnatProtocol): string {
  return protocol === 'both' ? 'TCP+UDP' : protocol.toUpperCase()
}
