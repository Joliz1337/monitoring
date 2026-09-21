import { useState } from 'react'
import { Cloud, Download, Loader2, Play, RefreshCw } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import type { ExitProxyNode, ExitProxySelectMode } from '../../api/client'
import { useExitProxyStore } from '../../stores/exitProxyStore'
import { formatTimeAgo, getFlag } from '../../utils/format'
import NodeRestrictedNotice from '../servers/NodeRestrictedNotice'
import { Tooltip } from '../ui/Tooltip'
import CandidateList from './CandidateList'
import WarpInstallLog from './WarpInstallLog'
import { HealthBadge, SelfTestBadge } from './badges'

const MODES: ExitProxySelectMode[] = ['auto', 'manual']

export default function NodePanel({ node }: { node: ExitProxyNode }) {
  const { t } = useTranslation()
  const updateNode = useExitProxyStore(s => s.updateNode)
  const checkNow = useExitProxyStore(s => s.checkNow)
  const installWarp = useExitProxyStore(s => s.installWarp)
  const fetchNodes = useExitProxyStore(s => s.fetchNodes)
  const isBusy = useExitProxyStore(s => s.isBusy)
  const isNodeBusy = useExitProxyStore(s => s.isNodeBusy)
  const [warpJob, setWarpJob] = useState<string | null>(null)

  const busy = isNodeBusy(node.server_id)
  const checking = isBusy(`node-${node.server_id}-check`) || node.check_in_progress
  const canAct = node.online && node.install_status === 'active'
  const current = node.current_exit
  const selfTest = node.self_test

  const startWarpInstall = async () => {
    const jobId = await installWarp(node.server_id)
    if (jobId) setWarpJob(jobId)
  }

  return (
    <div className="px-4 pb-4 pt-1 space-y-4 border-t border-dark-800/60">
      {node.install_status === 'denied' && <NodeRestrictedNotice compact variant="closed" />}

      {(node.sync_error || node.listen_error || node.last_check_error) && (
        <div className="bg-red-500/10 border border-red-500/20 rounded-lg p-3 space-y-1 text-xs text-red-400 break-all">
          {node.sync_error && <p>{node.sync_error}</p>}
          {node.listen_error && <p>{node.listen_error}</p>}
          {node.last_check_error && <p>{t('exit_proxy.check_error')}: {node.last_check_error}</p>}
        </div>
      )}

      <div className="flex flex-wrap items-center gap-4">
        <div className="flex items-center gap-2">
          <span className="text-sm text-dark-400">{t('exit_proxy.select_mode')}</span>
          <div className="flex items-center gap-1 bg-dark-800/60 border border-dark-700 rounded-lg p-0.5">
            {MODES.map(mode => (
              <button
                key={mode}
                disabled={busy}
                onClick={() => mode !== node.select_mode && updateNode(node.server_id, { select_mode: mode }, `node-${node.server_id}-mode`)}
                className={`px-3 py-1 rounded-md text-xs font-medium transition-colors ${node.select_mode === mode ? 'bg-accent-500 text-white' : 'text-dark-400 hover:text-dark-200'}`}
              >
                {t(`exit_proxy.selection_${mode}`)}
              </button>
            ))}
          </div>
          <Tooltip label={t('exit_proxy.selection_hint')} maxWidth={320}>
            <span className="text-xs text-dark-500 cursor-help">?</span>
          </Tooltip>
        </div>

        <div className="flex items-center gap-2 ml-auto">
          <button onClick={() => checkNow(node.server_id)} disabled={!canAct || checking || busy} className="btn btn-secondary text-xs py-1.5 disabled:opacity-40">
            {checking ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Play className="w-3.5 h-3.5" />}
            {checking ? t('exit_proxy.checking') : t('exit_proxy.check_now')}
          </button>
          <button onClick={() => updateNode(node.server_id, {}, `node-${node.server_id}-resync`)} disabled={!node.online || busy} className="btn btn-secondary text-xs py-1.5 disabled:opacity-40">
            <RefreshCw className="w-3.5 h-3.5" />
            {t('exit_proxy.resync')}
          </button>
        </div>
      </div>

      <div className="grid md:grid-cols-2 gap-3">
        <div className="bg-dark-800/40 rounded-lg p-3 space-y-1">
          <p className="text-xs text-dark-500">{t('exit_proxy.current_exit')}</p>
          {current ? (
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-sm text-dark-100 font-mono">{current.label}</span>
              {current.country && <span className="text-sm text-dark-300">{getFlag(current.country)} {current.country}</span>}
              <HealthBadge healthy={current.healthy} />
            </div>
          ) : (
            <p className="text-sm text-dark-400">{t('exit_proxy.no_current_exit')}</p>
          )}
          <p className="text-[11px] text-dark-500">
            {t('exit_proxy.connections', { count: node.stats.active_connections })} · {t('exit_proxy.connections_total', { count: node.stats.total_connections })}
          </p>
        </div>

        <div className="bg-dark-800/40 rounded-lg p-3 space-y-1">
          <div className="flex items-center justify-between">
            <p className="text-xs text-dark-500">{t('exit_proxy.self_test_title')}</p>
            <SelfTestBadge ok={selfTest ? selfTest.ok : null} />
          </div>
          {selfTest ? (
            <div className="text-[11px] text-dark-400 space-y-0.5">
              <p>{t('exit_proxy.self_test_ip')}: <span className="font-mono text-dark-200">{selfTest.ip ?? '—'}</span></p>
              <p>{t('exit_proxy.self_test_expected')}: <span className="font-mono text-dark-200">{selfTest.expected ?? '—'}</span></p>
              <p>{t('exit_proxy.checked_at')} {selfTest.at ? formatTimeAgo(selfTest.at) : '—'}</p>
              {!selfTest.ok && <p className="text-red-400">{selfTest.error || t('exit_proxy.self_test_mismatch')}</p>}
            </div>
          ) : (
            <p className="text-[11px] text-dark-500">{t('exit_proxy.self_test_unknown')}</p>
          )}
        </div>
      </div>

      <div className="space-y-2">
        <div className="flex items-center justify-between">
          <p className="text-xs text-dark-500">{t('exit_proxy.candidates')}</p>
          <p className="text-[11px] text-dark-500">{t('exit_proxy.candidates_hint')}</p>
        </div>
        <CandidateList node={node} />
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <Cloud className="w-4 h-4 text-dark-400" />
        <span className="text-xs text-dark-300">{node.warp.present ? t('exit_proxy.warp_present') : t('exit_proxy.warp_absent')}</span>
        {!node.warp.present && !warpJob && (
          <button onClick={startWarpInstall} disabled={!node.online || busy} className="btn btn-secondary text-xs py-1 disabled:opacity-40">
            <Download className="w-3.5 h-3.5" />
            {t('exit_proxy.warp_install')}
          </button>
        )}
      </div>
      {warpJob && (
        <WarpInstallLog
          jobId={warpJob}
          onFinished={() => setTimeout(fetchNodes, 5000)}
          onClose={() => setWarpJob(null)}
        />
      )}
    </div>
  )
}
