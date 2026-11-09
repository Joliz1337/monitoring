import { useTranslation } from 'react-i18next'
import { AlertTriangle, Gauge } from 'lucide-react'
import type { HAProxyAvailableServer } from '../../api/client'
import { type BackendServerGroup, isCapacityInvalid } from '../../utils/loadWeights'

/** Каналы серверов правила с весами по нагрузке: одна строка на сервер со всеми его IP */
export default function ServerCapacitiesBox({
  groups, foreign, nodes, draft, onDraftChange, onFillWeights, canFillWeights,
}: {
  groups: BackendServerGroup[]
  foreign: string[]
  nodes: HAProxyAvailableServer[]
  draft: Record<number, string>
  onDraftChange: (serverId: number, value: string) => void
  onFillWeights: () => void
  canFillWeights: boolean
}) {
  const { t } = useTranslation()
  const nodeById = new Map(nodes.map(node => [node.id, node]))
  const inp = 'w-28 px-2.5 py-1 rounded-lg bg-dark-800 border text-dark-100 text-sm focus:outline-none focus:border-accent-500/50'

  if (groups.length === 0 && foreign.length === 0) return null

  return (
    <div className="p-3 mt-2 bg-dark-900/40 rounded-lg border border-dark-700/40 space-y-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="min-w-0">
          <span className="flex items-center gap-1.5 text-xs text-dark-200 font-medium">
            <Gauge className="w-3.5 h-3.5 text-accent-500" /> {t('balancer.capacities')}
          </span>
          <p className="text-2xs text-dark-500">{t('balancer.capacities_hint')}</p>
        </div>
        <button type="button" onClick={onFillWeights} disabled={!canFillWeights}
          className="btn-tool disabled:opacity-50 disabled:cursor-not-allowed">
          {t('balancer.capacities_fill_weights')}
        </button>
      </div>

      {groups.map(({ serverId, addresses }) => {
        const node = nodeById.get(serverId)
        const value = draft[serverId] ?? ''
        const invalid = isCapacityInvalid(value)
        return (
          <div key={serverId} className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <div className="min-w-0 flex-1">
              <span className="block text-sm text-dark-200 truncate" title={addresses.join(', ')}>{node?.name ?? addresses[0]}</span>
              <span className="text-2xs text-dark-500">{t('balancer.capacities_addresses', { count: addresses.length })}</span>
            </div>
            <div className="flex items-center gap-1.5">
              <input type="number" inputMode="numeric" value={value} onChange={e => onDraftChange(serverId, e.target.value)}
                placeholder="1000" min={10} max={400000}
                className={`${inp} ${invalid ? 'border-red-500/60' : 'border-dark-700'}`} />
              <span className="text-2xs text-dark-500">{t('balancer.capacities_mbit')}</span>
            </div>
            {node?.nic_speed_mbps != null && (
              <div className="flex items-center gap-1.5 text-2xs text-dark-500">
                {t('balancer.capacities_nic', { speed: node.nic_speed_mbps })}
                <button type="button" onClick={() => onDraftChange(serverId, String(node.nic_speed_mbps))}
                  className="text-accent-400 hover:text-accent-300 transition-colors">
                  {t('balancer.capacities_fill_nic')}
                </button>
              </div>
            )}
            {invalid && <p className="basis-full text-2xs text-red-400">{t('balancer.capacities_invalid')}</p>}
            {!value.trim() && (
              <p className="basis-full flex items-center gap-1 text-2xs text-amber-400">
                <AlertTriangle className="w-3 h-3 shrink-0" /> {t('balancer.load_capacity_missing')}
              </p>
            )}
          </div>
        )
      })}

      {foreign.length > 0 && (
        <p className="text-2xs text-dark-500 break-words">{t('balancer.capacities_foreign', { addresses: foreign.join(', ') })}</p>
      )}
    </div>
  )
}
