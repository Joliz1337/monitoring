import { motion } from 'framer-motion'
import {
  Download,
  CheckCircle2,
  XCircle,
  Loader2,
  Server as ServerIcon,
  Clock,
  Check,
  Upload,
  ArrowUpCircle,
  type LucideIcon,
} from 'lucide-react'
import { useTranslation } from 'react-i18next'
import type { ImageDeliveryJobInfo, ImageDeliveryJobStatus, RemnawaveInstallJobInfo } from '../../api/client'
import { shortHAProxyVersion } from './HAProxyUpgradeModal'
import { Skeleton } from '../ui/Skeleton'
import { Tooltip } from '../ui/Tooltip'
import { Checkbox } from '../ui/Checkbox'

export type NodeLoadState = 'pending' | 'loading' | 'loaded' | 'error'

export interface NodeState {
  id: number
  name: string
  url: string
  folder: string | null
  hasSshCreds: boolean
  loadState: NodeLoadState
  version: string | null
  status: 'online' | 'offline'
  haproxyVersion: string | null
  haproxyTarget: string | null
}

const DELIVERY_CHIP: Record<ImageDeliveryJobStatus, { icon: LucideIcon; spin?: boolean; className: string }> = {
  queued: { icon: Clock, className: 'text-dark-300 bg-dark-700/50' },
  running: { icon: Loader2, spin: true, className: 'text-accent-400 bg-accent-500/10' },
  success: { icon: CheckCircle2, className: 'text-success bg-success/10' },
  error: { icon: XCircle, className: 'text-danger bg-danger/10' },
}

interface Props {
  node: NodeState
  index: number
  needsUpdate: boolean
  isDevChannel: boolean
  isUpdating: boolean
  updateResult?: { success: boolean; message: string }
  deliveryJob?: ImageDeliveryJobInfo
  haproxyJob?: RemnawaveInstallJobInfo
  selected: boolean
  onToggleSelect: () => void
  onUpdate: () => void
  onOpenDelivery: () => void
  onOpenHAProxy: () => void
}

export default function NodeUpdateCard({
  node, index, needsUpdate, isDevChannel, isUpdating, updateResult, deliveryJob, haproxyJob,
  selected, onToggleSelect, onUpdate, onOpenDelivery, onOpenHAProxy,
}: Props) {
  const { t } = useTranslation()
  const isNodeLoading = node.loadState === 'pending' || node.loadState === 'loading'
  const isOnline = node.status === 'online'

  const renderDeliveryChip = (job: ImageDeliveryJobInfo) => {
    const chip = DELIVERY_CHIP[job.status]
    const Icon = chip.icon
    return (
      <Tooltip label={job.error || t('imageDelivery.open_log')} maxWidth={320}>
        <button
          onClick={onOpenDelivery}
          className={`flex items-center gap-1.5 text-xs px-2 py-1 rounded-lg w-fit hover:brightness-125 transition ${chip.className}`}
        >
          <Icon className={`w-3.5 h-3.5 ${chip.spin ? 'animate-spin' : ''}`} />
          <span className="truncate max-w-[140px]">{t(`imageDelivery.status_${job.status}`)}</span>
        </button>
      </Tooltip>
    )
  }

  const renderHAProxyAction = () => {
    if (haproxyJob) {
      const chip = DELIVERY_CHIP[haproxyJob.status]
      const Icon = chip.icon
      return (
        <Tooltip label={haproxyJob.error || t('updates.haproxy_open_log')} maxWidth={320}>
          <button onClick={onOpenHAProxy} className={`flex items-center gap-1 px-1.5 py-0.5 rounded-md hover:brightness-125 transition ${chip.className}`}>
            <Icon className={`w-3 h-3 ${chip.spin ? 'animate-spin' : ''}`} />
            {t(`updates.haproxy_status_${haproxyJob.status}`)}
          </button>
        </Tooltip>
      )
    }
    if (!node.haproxyTarget || !isOnline) return null
    return (
      <Tooltip label={t('updates.haproxy_upgrade_hint', { version: node.haproxyTarget })} maxWidth={300}>
        <button
          onClick={onOpenHAProxy}
          className="flex items-center gap-1 px-1.5 py-0.5 rounded-md font-mono text-accent-400 bg-accent-500/10 hover:bg-accent-500/20 transition"
        >
          <ArrowUpCircle className="w-3 h-3" />
          {node.haproxyTarget}
        </button>
      </Tooltip>
    )
  }

  const renderUpdateStatus = () => {
    if (updateResult) {
      return (
        <motion.div
          className={`flex items-center gap-1.5 text-xs px-2 py-1 rounded-lg w-fit ${
            updateResult.success ? 'text-success bg-success/10' : 'text-danger bg-danger/10'
          }`}
          initial={{ opacity: 0, scale: 0.8 }}
          animate={{ opacity: 1, scale: 1 }}
        >
          {isUpdating ? (
            <Loader2 className="w-3.5 h-3.5 animate-spin" />
          ) : updateResult.success ? (
            <CheckCircle2 className="w-3.5 h-3.5" />
          ) : (
            <XCircle className="w-3.5 h-3.5" />
          )}
          <span className="truncate max-w-[120px]">{updateResult.message}</span>
        </motion.div>
      )
    }
    if (isNodeLoading) {
      return (
        <span className="flex items-center gap-1.5 text-xs text-dark-500">
          <Loader2 className="w-3.5 h-3.5 animate-spin" />
        </span>
      )
    }
    if (!isOnline) {
      return (
        <span className="flex items-center gap-1.5 text-xs text-dark-500">
          <Clock className="w-3.5 h-3.5" />
          {t('updates.offline')}
        </span>
      )
    }
    if (isUpdating) return null
    if (needsUpdate) {
      return (
        <motion.span
          className="px-2 py-0.5 text-xs font-medium bg-accent-500/20 text-accent-400 rounded-full"
          initial={{ scale: 0 }}
          animate={{ scale: 1 }}
        >
          {t('updates.update_available')}
        </motion.span>
      )
    }
    return (
      <span className="flex items-center gap-1.5 text-xs text-success">
        <Check className="w-3.5 h-3.5" />
        {t('updates.up_to_date')}
      </span>
    )
  }

  return (
    <motion.div
      className={`card group hover:border-dark-700 transition-all overflow-visible flex flex-col ${
        selected ? 'border-accent-500/40 bg-accent-500/[0.03]' : ''
      }`}
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: Math.min(index, 20) * 0.03 }}
    >
      <div className="flex items-center gap-3">
        <Checkbox checked={selected} onChange={onToggleSelect} />
        <motion.div
          className={`w-10 h-10 rounded-xl flex items-center justify-center border flex-shrink-0
            ${isNodeLoading
              ? 'bg-dark-800/50 border-dark-700/30 animate-pulse'
              : isOnline
                ? 'bg-gradient-to-br from-dark-700 to-dark-800 border-dark-700/50 group-hover:border-accent-500/30'
                : 'bg-dark-800/50 border-dark-700/30'
            } transition-colors`}
          whileHover={{ rotate: 5, scale: 1.05 }}
        >
          {isNodeLoading ? (
            <Loader2 className="w-4 h-4 text-dark-500 animate-spin" />
          ) : (
            <ServerIcon className={`w-4 h-4 ${isOnline ? 'text-accent-500' : 'text-dark-500'}`} />
          )}
        </motion.div>
        <div className="min-w-0 flex-1">
          <h3 className="font-semibold text-dark-100 flex items-center gap-2 truncate">
            <span className="truncate">{node.name}</span>
            {!isNodeLoading && (
              <span className={`w-2 h-2 rounded-full flex-shrink-0 ${isOnline ? 'bg-success' : 'bg-dark-500'}`} />
            )}
          </h3>
          {isNodeLoading ? (
            <Skeleton className="h-4 w-20 mt-0.5" />
          ) : (
            <span className="text-xs text-dark-500">
              <span className={`font-mono ${node.version ? 'text-dark-300' : 'text-dark-500'}`}>
                {node.version ? `v${node.version}` : t('updates.unknown')}
              </span>
            </span>
          )}
          {!isNodeLoading && (node.haproxyVersion || node.haproxyTarget || haproxyJob) && (
            <div className="flex items-center gap-1.5 text-xs text-dark-500 mt-0.5">
              <span>HAProxy</span>
              <Tooltip label={node.haproxyVersion ?? t('updates.unknown')}>
                <span className="font-mono text-dark-300">{shortHAProxyVersion(node.haproxyVersion) ?? '—'}</span>
              </Tooltip>
              {renderHAProxyAction()}
            </div>
          )}
        </div>
      </div>

      <div className="flex items-center justify-between mt-3 pt-3 border-t border-dark-700/30">
        <div className="flex-1 min-w-0">
          {deliveryJob ? renderDeliveryChip(deliveryJob) : renderUpdateStatus()}
        </div>

        <div className="flex items-center gap-2 flex-shrink-0">
          {!isNodeLoading && (
            <Tooltip label={t('imageDelivery.deliver_hint')} maxWidth={280}>
              <motion.button
                onClick={onOpenDelivery}
                disabled={isUpdating}
                className="btn btn-secondary text-xs px-2.5 py-1.5"
                whileHover={{ scale: 1.05 }}
                whileTap={{ scale: 0.95 }}
              >
                <Upload className="w-3.5 h-3.5" />
              </motion.button>
            </Tooltip>
          )}
          <motion.button
            onClick={onUpdate}
            disabled={isUpdating || isNodeLoading || !isOnline || (!needsUpdate && !isDevChannel)}
            className="btn btn-secondary text-xs px-3 py-1.5"
            whileHover={{ scale: 1.05 }}
            whileTap={{ scale: 0.95 }}
          >
            {isUpdating ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Download className="w-3.5 h-3.5" />}
            {t('updates.update')}
          </motion.button>
        </div>
      </div>
    </motion.div>
  )
}
