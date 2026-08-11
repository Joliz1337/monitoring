import { useCallback, useEffect, useState } from 'react'
import { serversApi, type NodeCapabilityDomain, type Server } from '../api/client'
import { nodeAllows } from '../utils/nodeCapabilities'

/**
 * Права нод для страниц, где своего списка серверов нет: сервер там выбирается
 * селектором внутри страницы. Там, где список уже загружен (стор или свой
 * запрос), хук не нужен — два источника истины в одном рендере разъезжаются.
 */
export function useNodeCapabilities() {
  const [servers, setServers] = useState<Server[]>([])

  useEffect(() => {
    let cancelled = false
    serversApi.list()
      .then(({ data }) => {
        if (!cancelled) setServers(data.servers || [])
      })
      .catch(() => {
        // Права не загрузились — страница работает как раньше, без ограничений
      })
    return () => { cancelled = true }
  }, [])

  const serverOf = useCallback(
    (serverId: number | null | undefined) =>
      serverId == null ? undefined : servers.find(s => s.id === serverId),
    [servers],
  )

  const allows = useCallback(
    (serverId: number | null | undefined, domain: NodeCapabilityDomain, mode: 'read' | 'write' = 'read') =>
      nodeAllows(serverOf(serverId), domain, mode),
    [serverOf],
  )

  return { servers, serverOf, allows }
}
