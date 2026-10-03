import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import type { BackendServer } from '../../api/client'

type PortChangeMode = 'all' | 'one'

const PORT_CHANGE_MODES: PortChangeMode[] = ['all', 'one']
const MAX_PORT = 65535

function parsePort(value: string): number | null {
  const port = Number(value)
  return Number.isInteger(port) && port >= 1 && port <= MAX_PORT ? port : null
}

// Порты списка от самого частого: первый — разумный выбор «какой порт менять» по умолчанию
function portsByFrequency(servers: BackendServer[]): { port: number; count: number }[] {
  const counts = new Map<number, number>()
  for (const { port } of servers) {
    if (port > 0) counts.set(port, (counts.get(port) ?? 0) + 1)
  }
  return [...counts]
    .map(([port, count]) => ({ port, count }))
    .sort((a, b) => b.count - a.count || a.port - b.port)
}

/** Смена целевого порта у серверов балансировщика: у всех сразу или только у тех, что на заданном порту */
export default function ServersPortBox({
  servers, onApply, onClose,
}: {
  servers: BackendServer[]
  onApply: (servers: BackendServer[], changed: number) => void
  onClose: () => void
}) {
  const { t } = useTranslation()
  const ports = portsByFrequency(servers)
  const [mode, setMode] = useState<PortChangeMode>('all')
  const [fromPort, setFromPort] = useState(ports.length > 0 ? String(ports[0].port) : '')
  const [toPort, setToPort] = useState('')

  const source = parsePort(fromPort)
  const target = parsePort(toPort)
  const isAffected = (srv: BackendServer) => target !== null && srv.port !== target && (mode === 'all' || srv.port === source)
  const changed = servers.filter(isAffected).length

  const apply = () => {
    if (target === null || changed === 0) return
    onApply(servers.map(srv => isAffected(srv) ? { ...srv, port: target } : srv), changed)
  }

  const inp = 'w-full px-2.5 py-1 rounded-lg bg-dark-800 border border-dark-700 text-dark-100 text-sm focus:outline-none focus:border-accent-500/50'
  const actionButton = 'px-3 py-1.5 rounded-lg text-xs transition-colors disabled:opacity-50 disabled:cursor-not-allowed'

  return (
    <div className="p-3 mb-2 bg-dark-900/40 rounded-lg border border-dark-700/40 space-y-3">
      <div className="flex flex-wrap items-end gap-3">
        <div>
          <span className="block text-2xs text-dark-500 mb-0.5">{t('balancer.port_change_scope')}</span>
          <div className="inline-flex rounded-lg border border-dark-700 overflow-hidden">
            {PORT_CHANGE_MODES.map(option => (
              <button key={option} type="button" onClick={() => setMode(option)}
                className={`px-2.5 py-1 text-xs transition-colors ${mode === option ? 'bg-accent-600 text-white' : 'bg-dark-800 text-dark-400 hover:text-dark-200'}`}>
                {t(`balancer.port_change_${option}`)}
              </button>
            ))}
          </div>
        </div>
        {mode === 'one' && (
          <label className="w-24">
            <span className="block text-2xs text-dark-500 mb-0.5">{t('balancer.port_from')}</span>
            <input type="number" value={fromPort} onChange={e => setFromPort(e.target.value)}
              placeholder="443" min={1} max={MAX_PORT}
              className={`${inp} ${fromPort && source === null ? 'border-red-500/60' : ''}`} />
          </label>
        )}
        <label className="w-24">
          <span className="block text-2xs text-dark-500 mb-0.5">{t('balancer.port_to')}</span>
          <input type="number" value={toPort} onChange={e => setToPort(e.target.value)} autoFocus
            placeholder="8443" min={1} max={MAX_PORT}
            className={`${inp} ${toPort && target === null ? 'border-red-500/60' : ''}`} />
        </label>
      </div>

      {mode === 'one' && ports.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-2xs text-dark-500">{t('balancer.port_in_list')}</span>
          {ports.map(({ port, count }) => (
            <button key={port} type="button" onClick={() => setFromPort(String(port))}
              className={`px-2 py-0.5 rounded-md text-2xs font-mono border transition-colors ${source === port
                ? 'border-accent-500/60 text-accent-300 bg-accent-500/10'
                : 'border-dark-700 text-dark-300 bg-dark-800 hover:text-dark-100'}`}>
              {port} <span className="text-dark-500">×{count}</span>
            </button>
          ))}
        </div>
      )}

      <p className="text-2xs text-dark-500">{t('balancer.port_change_hint')}</p>
      {target !== null && (
        <p className="text-2xs text-dark-400">{t('balancer.port_change_summary', { count: changed, total: servers.length })}</p>
      )}

      <div className="flex flex-wrap justify-end gap-2">
        <button type="button" onClick={onClose}
          className={`${actionButton} text-dark-300 hover:text-dark-100 bg-dark-800 hover:bg-dark-700 border border-dark-700`}>
          {t('common.cancel')}
        </button>
        <button type="button" onClick={apply} disabled={changed === 0}
          className={`${actionButton} font-medium bg-accent-600 hover:bg-accent-500 text-white`}>
          {t('balancer.port_change_apply')}
        </button>
      </div>
    </div>
  )
}
