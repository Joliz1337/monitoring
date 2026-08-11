import {
  CAP_MODE,
  NODE_CAPABILITY_DOMAINS,
  type NodeCapabilities,
  type NodeCapabilityDomain,
  type Server,
} from '../api/client'

type TranslateFunction = (key: string, options?: Record<string, unknown>) => string

interface WithCapabilities {
  node_capabilities?: NodeCapabilities | null
}

/**
 * Разрешает ли нода этот раздел. Нет данных — да: пока список серверов
 * грузится, страница должна рисовать обычный интерфейс, а не мигать заглушкой.
 */
export function nodeAllows(
  server: WithCapabilities | null | undefined,
  domain: NodeCapabilityDomain,
  mode: 'read' | 'write' = 'read',
): boolean {
  const caps = server?.node_capabilities
  if (!caps) return true
  const level = caps[domain] ?? CAP_MODE.FULL
  if (level === CAP_MODE.NONE) return false
  if (level === CAP_MODE.READ) return mode === 'read'
  return true
}

export function nodeIsRestricted(server: WithCapabilities | null | undefined): boolean {
  const caps = server?.node_capabilities
  if (!caps) return false
  return NODE_CAPABILITY_DOMAINS.some(domain => (caps[domain] ?? CAP_MODE.FULL) !== CAP_MODE.FULL)
}

/** «HAProxy, Файрвол (только просмотр)» — то, что осталось доступно. */
export function describeAllowedDomains(
  server: WithCapabilities | null | undefined,
  t: TranslateFunction,
): string {
  const caps = server?.node_capabilities
  if (!caps) return t('node_caps.allowed_everything')

  const parts = NODE_CAPABILITY_DOMAINS
    .map(domain => {
      const level = caps[domain] ?? CAP_MODE.FULL
      if (level === CAP_MODE.NONE) return null
      const name = t(`node_caps.domain_${domain}`)
      return level === CAP_MODE.READ ? `${name} (${t('node_caps.readonly_suffix')})` : name
    })
    .filter((part): part is string => part !== null)

  return parts.length ? parts.join(', ') : t('node_caps.allowed_none')
}

export function findServer(servers: Server[], serverId: number | null | undefined): Server | undefined {
  if (serverId == null) return undefined
  return servers.find(s => s.id === serverId)
}
