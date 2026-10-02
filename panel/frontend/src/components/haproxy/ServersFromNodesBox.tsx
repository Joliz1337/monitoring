import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import type { BackendServer, HAProxyAvailableServer, HAProxyIpOwners } from '../../api/client'
import { useRememberedState } from '../../hooks/useRememberedState'
import { groupAddressesByNode, pickAddresses, type NodeAddressKind } from '../../utils/nodeAddresses'
import { Checkbox } from '../ui/Checkbox'
import FolderedServerPicker from '../servers/FolderedServerPicker'
import type { ServersMergeMode } from './ServersPasteBox'

const ADDRESS_KINDS: NodeAddressKind[] = ['primary', 'extra', 'all']
const ADDRESSES_SHOWN = 2
const DEFAULT_WEIGHT = 100
const MAX_WEIGHT = 256
const MAX_PORT = 65535

function parseBounded(value: string, max: number): number | null {
  const number = Number(value)
  return Number.isInteger(number) && number >= 1 && number <= max ? number : null
}

function summarizeAddresses(addresses: string[]): string {
  const shown = addresses.slice(0, ADDRESSES_SHOWN).join(', ')
  const hidden = addresses.length - ADDRESSES_SHOWN
  return hidden > 0 ? `${shown} +${hidden}` : shown
}

/** Серверы балансировщика из IP выбранных нод: основных, дополнительных или всех сразу */
export default function ServersFromNodesBox({
  nodes, ipOwners, defaults, onApply, onClose,
}: {
  nodes: HAProxyAvailableServer[]
  ipOwners: HAProxyIpOwners
  defaults: BackendServer
  onApply: (servers: BackendServer[], mode: ServersMergeMode) => void
  onClose: () => void
}) {
  const { t } = useTranslation()
  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [kind, setKind] = useRememberedState<NodeAddressKind>('haproxy_nodes_address_kind', 'all')
  const [port, setPort] = useState(defaults.port > 0 ? String(defaults.port) : '')
  const [weight, setWeight] = useState(String(DEFAULT_WEIGHT))

  const addressesByNode = useMemo(() => groupAddressesByNode(ipOwners), [ipOwners])
  const addressesOf = (node: HAProxyAvailableServer) => pickAddresses(addressesByNode.get(node.id), kind)

  const selectedNodes = nodes.filter(node => selected.has(node.id))
  const nodesWithoutAddresses = selectedNodes.filter(node => addressesOf(node).length === 0)
  const targetPort = parseBounded(port, MAX_PORT)
  const targetWeight = parseBounded(weight, MAX_WEIGHT)
  const servers: BackendServer[] = targetPort === null || targetWeight === null ? [] : selectedNodes.flatMap(node =>
    addressesOf(node).map(address => ({ ...defaults, name: '', address, port: targetPort, weight: targetWeight })),
  )

  const setNodesSelected = (ids: number[], checked: boolean) => {
    setSelected(prev => {
      const next = new Set(prev)
      for (const id of ids) {
        if (checked) next.add(id)
        else next.delete(id)
      }
      return next
    })
  }

  const inp = 'w-full px-2.5 py-1 rounded-lg bg-dark-800 border border-dark-700 text-dark-100 text-sm focus:outline-none focus:border-accent-500/50'
  const actionButton = 'px-3 py-1.5 rounded-lg text-xs transition-colors disabled:opacity-50 disabled:cursor-not-allowed'

  return (
    <div className="p-3 mb-2 bg-dark-900/40 rounded-lg border border-dark-700/40 space-y-3">
      <FolderedServerPicker
        servers={nodes}
        storageKey="haproxy_nodes_expanded_folders"
        labels={{
          searchPlaceholder: t('haproxy_configs.search_server'),
          empty: t('bulk_actions.no_results'),
          noFolder: t('bulk_actions.no_folder'),
        }}
        renderFolderAction={(_, members) => {
          const selectedCount = members.filter(member => selected.has(member.id)).length
          const allSelected = selectedCount === members.length
          return (
            <Checkbox
              checked={allSelected}
              indeterminate={selectedCount > 0 && !allSelected}
              onChange={() => setNodesSelected(members.map(member => member.id), !allSelected)}
            />
          )
        }}
        renderServer={node => {
          const addresses = addressesOf(node)
          return (
            <label key={node.id} className="flex items-center gap-2 px-2 py-1.5 rounded-lg hover:bg-dark-800/50 cursor-pointer">
              <Checkbox checked={selected.has(node.id)} onChange={e => setNodesSelected([node.id], e.target.checked)} />
              <span className="text-sm text-dark-200 truncate">{node.name}</span>
              {addresses.length > 0
                ? <span className="ml-auto pl-2 text-[10px] text-dark-500 font-mono truncate" title={addresses.join(', ')}>{summarizeAddresses(addresses)}</span>
                : <span className="ml-auto pl-2 text-[10px] text-dark-600 shrink-0">{t('balancer.nodes_no_addresses')}</span>}
            </label>
          )
        }}
      />

      <div className="flex flex-wrap items-end gap-3">
        <div>
          <span className="block text-[10px] text-dark-500 mb-0.5">{t('balancer.nodes_addresses')}</span>
          <div className="inline-flex rounded-lg border border-dark-700 overflow-hidden">
            {ADDRESS_KINDS.map(option => (
              <button key={option} type="button" onClick={() => setKind(option)}
                className={`px-2.5 py-1 text-xs transition-colors ${kind === option ? 'bg-accent-600 text-white' : 'bg-dark-800 text-dark-400 hover:text-dark-200'}`}>
                {t(`balancer.nodes_kind_${option}`)}
              </button>
            ))}
          </div>
        </div>
        <label className="w-24">
          <span className="block text-[10px] text-dark-500 mb-0.5">{t('haproxy.target_port')}</span>
          <input type="number" value={port} onChange={e => setPort(e.target.value)}
            placeholder="443" min={1} max={MAX_PORT}
            className={`${inp} ${port && targetPort === null ? 'border-red-500/60' : ''}`} />
        </label>
        <label className="w-20">
          <span className="block text-[10px] text-dark-500 mb-0.5">{t('balancer.weight')}</span>
          <input type="number" value={weight} onChange={e => setWeight(e.target.value)}
            min={1} max={MAX_WEIGHT}
            className={`${inp} ${targetWeight === null ? 'border-red-500/60' : ''}`} />
        </label>
      </div>

      <p className="text-[10px] text-dark-500">{t('balancer.nodes_hint')}</p>
      {selectedNodes.length > 0 && (
        <p className="text-[10px] text-dark-400">
          {t('balancer.nodes_summary', { nodes: selectedNodes.length, count: servers.length })}
          {nodesWithoutAddresses.length > 0 && (
            <span className="text-amber-400"> · {t('balancer.nodes_without_addresses', { names: nodesWithoutAddresses.map(node => node.name).join(', ') })}</span>
          )}
        </p>
      )}

      <div className="flex flex-wrap justify-end gap-2">
        <button type="button" onClick={onClose}
          className={`${actionButton} text-dark-300 hover:text-dark-100 bg-dark-800 hover:bg-dark-700 border border-dark-700`}>
          {t('common.cancel')}
        </button>
        <button type="button" onClick={() => onApply(servers, 'append')} disabled={servers.length === 0}
          className={`${actionButton} text-dark-200 bg-dark-800 hover:bg-dark-700 border border-dark-700`}>
          {t('balancer.paste_append')}
        </button>
        <button type="button" onClick={() => onApply(servers, 'replace')} disabled={servers.length === 0}
          className={`${actionButton} font-medium bg-accent-600 hover:bg-accent-500 text-white`}>
          {t('balancer.paste_replace')}
        </button>
      </div>
    </div>
  )
}
