import { ChevronDown, Loader2 } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import type { ExitProxyNode } from '../../api/client'
import { useExitProxyStore } from '../../stores/exitProxyStore'
import { getFlag } from '../../utils/format'
import { Toggle } from '../ui/Toggle'
import { Tooltip } from '../ui/Tooltip'
import NodePanel from './NodePanel'
import { HealthBadge, InstallBadge, SelfTestBadge } from './badges'

function versionAtLeast(version: string | null, minimum: string): boolean {
  if (!version) return false
  const parse = (v: string) => v.split('.').map(part => parseInt(part, 10) || 0)
  const a = parse(version)
  const b = parse(minimum)
  for (let i = 0; i < Math.max(a.length, b.length); i++) {
    const diff = (a[i] ?? 0) - (b[i] ?? 0)
    if (diff !== 0) return diff > 0
  }
  return true
}

interface Props {
  node: ExitProxyNode
  open: boolean
  onToggleOpen: () => void
  minNodeVersion: string
}

export default function NodeRow({ node, open, onToggleOpen, minNodeVersion }: Props) {
  const { t } = useTranslation()
  const updateNode = useExitProxyStore(s => s.updateNode)
  const isNodeBusy = useExitProxyStore(s => s.isNodeBusy)
  const busy = isNodeBusy(node.server_id)
  const supported = versionAtLeast(node.node_version, minNodeVersion)
  const status = !node.enabled && !supported && node.node_version ? 'unsupported' : node.install_status

  const toggleEnabled = () => {
    if (node.enabled && !window.confirm(t('exit_proxy.confirm_disable', { name: node.name }))) return
    updateNode(node.server_id, { enabled: !node.enabled }, `node-${node.server_id}-toggle`)
  }

  const hint = status === 'unsupported'
    ? t('exit_proxy.unsupported_hint', { version: minNodeVersion })
    : status === 'failed' && node.sync_error ? node.sync_error : undefined

  return (
    <div className="bg-dark-900/40">
      <div className="flex flex-wrap items-center gap-3 px-4 py-3 cursor-pointer" onClick={onToggleOpen}>
        <span className={`w-2 h-2 rounded-full shrink-0 ${node.online ? 'bg-green-400 animate-pulse' : 'bg-dark-600'}`} />
        <div className="min-w-[160px]">
          <div className="text-sm text-dark-100">{node.name}</div>
          <div className="text-[11px] text-dark-500">{node.node_version ? `v${node.node_version}` : '—'}</div>
        </div>
        <InstallBadge status={status} hint={hint} />

        <div className="flex flex-wrap items-center gap-2 flex-1 min-w-[200px] text-sm text-dark-300">
          {node.enabled && node.check_in_progress && (
            <span className="inline-flex items-center gap-1 text-xs text-dark-400"><Loader2 className="w-3 h-3 animate-spin" />{t('exit_proxy.checking')}</span>
          )}
          {node.enabled && node.current_exit && (
            <>
              <span className="text-xs text-dark-500">{t('exit_proxy.exit_short')}</span>
              <span className="font-mono text-dark-100">{node.current_exit.label}</span>
              {node.current_exit.country && <span>{getFlag(node.current_exit.country)} {node.current_exit.country}</span>}
              <HealthBadge healthy={node.current_exit.healthy} />
              <SelfTestBadge ok={node.self_test ? node.self_test.ok : null} />
              <span className="text-[11px] text-dark-500">{t('exit_proxy.connections', { count: node.stats.active_connections })}</span>
            </>
          )}
          {!node.enabled && <span className="text-xs text-dark-500">—</span>}
        </div>

        <div className="flex items-center gap-3" onClick={e => e.stopPropagation()}>
          <Tooltip label={t('exit_proxy.node_offline')} disabled={node.online || node.enabled}>
            <span>
              <Toggle
                on={node.enabled}
                onClick={toggleEnabled}
                disabled={busy || (!node.enabled && (!supported || !node.online))}
                title={t('exit_proxy.enable_node')}
              />
            </span>
          </Tooltip>
          <button onClick={onToggleOpen} className="text-dark-500 hover:text-dark-300">
            <ChevronDown className={`w-4 h-4 transition-transform ${open ? 'rotate-180' : ''}`} />
          </button>
        </div>
      </div>
      {open && node.enabled && <NodePanel node={node} />}
      {open && !node.enabled && (
        <p className="px-4 pb-4 text-xs text-dark-500">{t('exit_proxy.node_disabled_hint')}</p>
      )}
    </div>
  )
}
