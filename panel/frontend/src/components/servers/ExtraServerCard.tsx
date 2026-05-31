import { useRef, useEffect } from 'react'
import { motion } from 'framer-motion'
import {
  Server as ServerIcon,
  Link as LinkIcon,
  Trash2,
  CheckCircle2,
  XCircle,
  Loader2,
  Terminal,
  RotateCw,
  AlertTriangle,
  PlusCircle,
} from 'lucide-react'
import { useTranslation } from 'react-i18next'
import DeployTargetFields, { type DeployFormData } from './DeployTargetFields'
import type { RemnawaveCertProfile, HAProxyConfigProfile, FirewallProfile } from '../../api/client'

export type DeployStatus = 'idle' | 'running' | 'success' | 'error'

export interface ExtraTarget {
  id: string
  name: string
  host: string
  port: string
  deploy: DeployFormData
  status: DeployStatus
  log: string[]
  error: string | null
  serverId?: number
  jobId?: string
}

interface Props {
  index: number
  target: ExtraTarget
  onChange: (patch: Partial<Omit<ExtraTarget, 'id' | 'deploy'>>) => void
  onDeployChange: (patch: Partial<DeployFormData>) => void
  onRemove: () => void
  onRetry: () => void
  onAddAnother: () => void
  remnaCertProfiles: RemnawaveCertProfile[]
  haproxyProfiles: HAProxyConfigProfile[]
  firewallProfiles: FirewallProfile[]
  savingCert: boolean
  onSaveCert: () => void
  onDeleteCert: (id: number) => void
  disabled: boolean
}

export default function ExtraServerCard({
  index,
  target,
  onChange,
  onDeployChange,
  onRemove,
  onRetry,
  onAddAnother,
  remnaCertProfiles,
  haproxyProfiles,
  firewallProfiles,
  savingCert,
  onSaveCert,
  onDeleteCert,
  disabled,
}: Props) {
  const { t } = useTranslation()
  const logRef = useRef<HTMLPreElement>(null)

  useEffect(() => {
    const el = logRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [target.log])

  const status = target.status
  const showLog = target.log.length > 0 || status === 'running'

  return (
    <motion.div
      className="rounded-xl border border-dark-700/50 bg-dark-800/30 overflow-hidden"
      initial={{ opacity: 0, height: 0 }}
      animate={{ opacity: 1, height: 'auto' }}
      exit={{ opacity: 0, height: 0 }}
      transition={{ duration: 0.15 }}
    >
      <div className="flex items-center justify-between gap-3 p-3 border-b border-dark-700/50 bg-dark-800/40">
        <div className="flex items-center gap-2 min-w-0 flex-1">
          <ServerIcon className="w-4 h-4 text-accent-500 flex-shrink-0" />
          <span className="text-sm font-medium text-dark-100 truncate">
            {target.name.trim() || t('servers.deploy_extra_default_name', { n: index + 2 })}
          </span>
          {status === 'running' && (
            <span className="flex items-center gap-1 text-xs text-dark-300">
              <Loader2 className="w-3 h-3 animate-spin" />
              {t('common.loading')}
            </span>
          )}
          {status === 'success' && (
            <span className="flex items-center gap-1 text-xs text-success">
              <CheckCircle2 className="w-3 h-3" />
              {t('servers.deploy_extra_ok')}
            </span>
          )}
          {status === 'error' && (
            <span className="flex items-center gap-1 text-xs text-danger">
              <XCircle className="w-3 h-3" />
              {t('servers.deploy_extra_failed')}
            </span>
          )}
        </div>
        <button
          type="button"
          onClick={onRemove}
          disabled={disabled}
          className="p-1.5 rounded-lg text-dark-400 hover:bg-danger/10 hover:text-danger transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
          title={t('servers.deploy_extra_remove')}
        >
          <Trash2 className="w-4 h-4" />
        </button>
      </div>

      <div className="p-4 space-y-4">
        <div>
          <label className="block text-sm text-dark-300 mb-2 flex items-center gap-2">
            <ServerIcon className="w-4 h-4" />
            {t('servers.server_name')}
          </label>
          <input
            type="text"
            value={target.name}
            onChange={(e) => onChange({ name: e.target.value })}
            placeholder={t('servers.server_name_placeholder')}
            className="input"
            disabled={status === 'running'}
          />
        </div>

        <div>
          <label className="block text-sm text-dark-300 mb-2 flex items-center gap-2">
            <LinkIcon className="w-4 h-4" />
            {t('servers.server_host')}
          </label>
          <div className="flex gap-3">
            <div className="flex-1">
              <input
                type="text"
                value={target.host}
                onChange={(e) => onChange({ host: e.target.value })}
                placeholder={t('servers.server_host_placeholder')}
                className="input"
                disabled={status === 'running'}
              />
            </div>
            <div className="w-28">
              <input
                type="text"
                value={target.port}
                onChange={(e) => onChange({ port: e.target.value.replace(/\D/g, '') })}
                placeholder={t('servers.server_port_placeholder')}
                className="input text-center"
                disabled={status === 'running'}
              />
            </div>
          </div>
        </div>

        <div className="border-t border-dark-700/50 pt-4">
          <DeployTargetFields
            deploy={target.deploy}
            onChange={onDeployChange}
            remnaCertProfiles={remnaCertProfiles}
            haproxyProfiles={haproxyProfiles}
            firewallProfiles={firewallProfiles}
            savingCert={savingCert}
            onSaveCert={onSaveCert}
            onDeleteCert={onDeleteCert}
          />
        </div>

        {status === 'error' && target.error && (
          <div className="flex items-start gap-2 p-3 rounded-lg bg-danger/10 border border-danger/20 text-danger text-xs">
            <AlertTriangle className="w-4 h-4 flex-shrink-0 mt-0.5" />
            <span className="break-words">{target.error}</span>
          </div>
        )}

        {showLog && (
          <div className="rounded-lg bg-dark-900/70 border border-dark-700/50 p-3">
            <div className="flex items-center gap-2 mb-2 text-xs text-dark-400">
              {status === 'running'
                ? <Loader2 className="w-3.5 h-3.5 animate-spin" />
                : <Terminal className="w-3.5 h-3.5" />}
              {t('servers.deploy_log')}
            </div>
            <pre
              ref={logRef}
              className="text-[11px] leading-relaxed font-mono text-dark-300 max-h-48 overflow-auto whitespace-pre-wrap"
            >
              {target.log.join('\n')}
            </pre>
          </div>
        )}

        <div className="flex flex-wrap gap-2">
          {status === 'error' && (
            <button
              type="button"
              onClick={onRetry}
              disabled={disabled}
              className="btn btn-secondary text-sm"
            >
              <RotateCw className="w-4 h-4" />
              {t('servers.deploy_extra_retry')}
            </button>
          )}
          <button
            type="button"
            onClick={onAddAnother}
            disabled={disabled}
            className="btn btn-secondary text-sm"
          >
            <PlusCircle className="w-4 h-4" />
            {t('servers.deploy_add_extra')}
          </button>
        </div>
      </div>
    </motion.div>
  )
}
