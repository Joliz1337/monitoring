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
  Globe,
  type LucideIcon,
} from 'lucide-react'
import { useTranslation } from 'react-i18next'
import type {
  DownloadProxySummary, ImageDeliveryJobInfo, ImageDeliveryJobStatus, ImageDeliveryStep,
  NodeUpdateProgress, NodeUpdateStep, RemnawaveInstallJobInfo,
} from '../../api/client'
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
  // null — нода не ответила или ещё не умеет управлять прокси
  downloadProxy: DownloadProxySummary | null
}

const DELIVERY_CHIP: Record<ImageDeliveryJobStatus, { icon: LucideIcon; spin?: boolean; className: string }> = {
  queued: { icon: Clock, className: 'text-dark-300 bg-dark-700/50' },
  running: { icon: Loader2, spin: true, className: 'text-warning bg-warning/10' },
  success: { icon: CheckCircle2, className: 'text-success bg-success/10' },
  error: { icon: XCircle, className: 'text-danger bg-danger/10' },
}

// Порядок этапов — для «2/4» и полоски: на ноде UPDATE_STAGES, у доставки — шаги node_image_delivery
const NODE_UPDATE_STEPS: NodeUpdateStep[] = ['download', 'files', 'images', 'restart']
const DELIVERY_STEPS: ImageDeliveryStep[] = ['prepare', 'upload', 'load', 'start']

interface ProgressView {
  label: string
  // null — этап неизвестен (старый агент): только надпись, без полоски
  stepIndex: number | null
  total: number
  percent: number | null
  tooltip: string
  onClick?: () => void
}

function ProgressBar({ stepIndex, total, percent }: { stepIndex: number; total: number; percent: number | null }) {
  return (
    <div className="flex gap-1 mt-1.5">
      {Array.from({ length: total }, (_, i) => (
        <div key={i} className="h-1 flex-1 rounded-full bg-dark-700/60 overflow-hidden">
          {i < stepIndex && <div className="h-full w-full bg-warning" />}
          {i === stepIndex && (percent !== null
            ? <div className="h-full bg-warning transition-[width] duration-500" style={{ width: `${percent}%` }} />
            : <div className="h-full w-full bg-warning/60 animate-pulse" />)}
        </div>
      ))}
    </div>
  )
}

interface Props {
  node: NodeState
  index: number
  needsUpdate: boolean
  isDevChannel: boolean
  isUpdating: boolean
  updateResult?: { success: boolean; message: string }
  // идущее обновление через агента — панель следит за ним и после перезагрузки страницы
  agentUpdate?: NodeUpdateProgress
  deliveryJob?: ImageDeliveryJobInfo
  haproxyJob?: RemnawaveInstallJobInfo
  selected: boolean
  onToggleSelect: () => void
  onUpdate: () => void
  onOpenDelivery: () => void
  onOpenHAProxy: () => void
  onOpenProxy: () => void
}

export default function NodeUpdateCard({
  node, index, needsUpdate, isDevChannel, isUpdating, updateResult, agentUpdate, deliveryJob, haproxyJob,
  selected, onToggleSelect, onUpdate, onOpenDelivery, onOpenHAProxy, onOpenProxy,
}: Props) {
  const { t } = useTranslation()
  const isNodeLoading = node.loadState === 'pending' || node.loadState === 'loading'
  const isOnline = node.status === 'online'
  const isBusy = isUpdating || !!agentUpdate

  // Идущее обновление — отдельной строкой на всю ширину карточки, чтобы этап и «N/4» не обрезались
  const progressView = (): ProgressView | null => {
    if (deliveryJob?.status === 'running') {
      const step = deliveryJob.step
      const percent = step === 'upload' ? deliveryJob.percent : null
      return {
        label: step
          ? `SSH: ${t(`imageDelivery.step_${step}`)}${percent !== null ? ` ${percent}%` : ''}`
          : t('imageDelivery.status_running'),
        stepIndex: step ? DELIVERY_STEPS.indexOf(step) : null,
        total: DELIVERY_STEPS.length,
        percent,
        tooltip: t('imageDelivery.open_log'),
        onClick: onOpenDelivery,
      }
    }
    if (!agentUpdate || isUpdating || deliveryJob) return null
    const since = new Date(agentUpdate.started_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
    return {
      label: agentUpdate.step
        ? `${t('updates.agent_updating')}: ${t(`updates.step_${agentUpdate.step}`)}`
        : `${t('updates.agent_updating')}…`,
      stepIndex: agentUpdate.step ? NODE_UPDATE_STEPS.indexOf(agentUpdate.step) : null,
      total: NODE_UPDATE_STEPS.length,
      percent: null,
      tooltip: t('updates.agent_updating_since', { time: since }),
    }
  }
  const progress = progressView()

  const renderProgress = (view: ProgressView) => {
    const body = (
      <>
        <div className="flex items-center gap-1.5 text-xs text-warning">
          <Loader2 className="w-3.5 h-3.5 shrink-0 animate-spin" />
          <span className="truncate flex-1">{view.label}</span>
          {view.stepIndex !== null && (
            <span className="font-mono shrink-0">{view.stepIndex + 1}/{view.total}</span>
          )}
        </div>
        {view.stepIndex !== null && <ProgressBar stepIndex={view.stepIndex} total={view.total} percent={view.percent} />}
      </>
    )
    const className = 'mt-3 w-full text-left rounded-lg bg-warning/10 px-2.5 py-2'
    return (
      <Tooltip label={view.tooltip} maxWidth={300}>
        {view.onClick
          ? <button onClick={view.onClick} className={`${className} hover:brightness-125 transition`}>{body}</button>
          : <div className={className}>{body}</div>}
      </Tooltip>
    )
  }

  const renderDeliveryChip = (job: ImageDeliveryJobInfo) => {
    const chip = DELIVERY_CHIP[job.status]
    const Icon = chip.icon
    return (
      <Tooltip label={job.error || t('imageDelivery.open_log')} maxWidth={320}>
        <button
          onClick={onOpenDelivery}
          className={`flex items-center gap-1.5 text-xs px-2 py-1 rounded-lg w-fit max-w-full hover:brightness-125 transition ${chip.className}`}
        >
          <Icon className={`w-3.5 h-3.5 shrink-0 ${chip.spin ? 'animate-spin' : ''}`} />
          <span className="truncate">{t(`imageDelivery.status_${job.status}`)}</span>
        </button>
      </Tooltip>
    )
  }

  const renderStatusArea = () => {
    if (progress) return null
    if (deliveryJob) return renderDeliveryChip(deliveryJob)
    return renderUpdateStatus()
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
          {!isNodeLoading && !!node.downloadProxy?.urls.length && (
            <Tooltip label={t('updates.proxy_found_hint')} maxWidth={300}>
              <button
                onClick={onOpenProxy}
                className="flex items-center gap-1.5 text-xs text-warning mt-0.5 max-w-full hover:brightness-125 transition"
              >
                <Globe className="w-3 h-3 shrink-0" />
                <span className="shrink-0">{t('updates.proxy_label')}</span>
                <span className="font-mono truncate">{node.downloadProxy.urls[0]}</span>
                {node.downloadProxy.urls.length > 1 && <span className="shrink-0">+{node.downloadProxy.urls.length - 1}</span>}
              </button>
            </Tooltip>
          )}
        </div>
      </div>

      {progress && renderProgress(progress)}

      <div className="flex items-center justify-between mt-3 pt-3 border-t border-dark-700/30">
        <div className="flex-1 min-w-0">
          {renderStatusArea()}
        </div>

        <div className="flex items-center gap-2 flex-shrink-0">
          {!isNodeLoading && isOnline && node.downloadProxy && (
            <Tooltip label={t('updates.proxy_open')} maxWidth={280}>
              <motion.button
                onClick={onOpenProxy}
                disabled={isUpdating}
                className="btn btn-secondary text-xs px-2.5 py-1.5"
                whileHover={{ scale: 1.05 }}
                whileTap={{ scale: 0.95 }}
              >
                <Globe className="w-3.5 h-3.5" />
              </motion.button>
            </Tooltip>
          )}
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
            disabled={isBusy || isNodeLoading || !isOnline || (!needsUpdate && !isDevChannel)}
            className="btn btn-secondary text-xs px-3 py-1.5"
            whileHover={{ scale: 1.05 }}
            whileTap={{ scale: 0.95 }}
          >
            {isBusy ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Download className="w-3.5 h-3.5" />}
            {t('updates.update')}
          </motion.button>
        </div>
      </div>
    </motion.div>
  )
}
