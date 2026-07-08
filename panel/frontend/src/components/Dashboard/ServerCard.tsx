import { memo, useState } from 'react'
import { useSortable } from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'
import { useNavigate, useParams } from 'react-router-dom'
import {
  GripVertical,
  Cpu,
  HardDrive,
  Network,
  Clock,
  MemoryStick,
  ChevronRight,
  WifiOff,
  AlertTriangle,
  ShieldCheck,
  ShieldAlert,
  ArrowDownToLine,
  ArrowUpFromLine,
  PowerOff,
  Database,
  Activity,
} from 'lucide-react'
import { Server, ServerMetrics } from '../../api/client'
import StatusBadge from '../ui/StatusBadge'
import ProgressBar from '../ui/ProgressBar'
import { formatBytes, formatBitsPerSecLocalized, formatUptime, formatTimeAgo, extractHost } from '../../utils/format'
import { useTranslation } from 'react-i18next'
import { CopyableIp } from '../ui/CopyableIp'
import type { DetailLevel } from '../../stores/settingsStore'

interface ServerTraffic {
  rx_bytes: number
  tx_bytes: number
  days: number
}

function getLoadAvgColor(loadAvg: number, coresLogical: number): string {
  const percent = (loadAvg / coresLogical) * 100
  if (percent > 100) return 'text-danger'
  if (percent >= 70) return 'text-warning'
  return 'text-success'
}

interface ServerCardProps {
  server: Server & {
    metrics?: ServerMetrics | null
    traffic?: ServerTraffic | null
    status: 'online' | 'offline' | 'loading' | 'error'
    last_seen?: string | null
    last_error?: string | null
    error_code?: number | null
  }
  compact?: boolean
  detailLevel?: DetailLevel
  index?: number
}

interface DragHandle {
  attributes: ReturnType<typeof useSortable>['attributes']
  listeners: ReturnType<typeof useSortable>['listeners']
}

interface ServerCardViewProps extends ServerCardProps {
  cardRef?: (node: HTMLElement | null) => void
  dndStyle?: React.CSSProperties
  isDragging?: boolean
  isOverlay?: boolean
  handle?: DragHandle
}

function ServerCardView({
  server,
  compact,
  detailLevel = 'standard',
  index = 0,
  cardRef,
  dndStyle,
  isDragging = false,
  isOverlay = false,
  handle,
}: ServerCardViewProps) {
  const { uid } = useParams()
  const navigate = useNavigate()
  const { t } = useTranslation()

  // dnd-kit transform — оставляем как inline-style (он меняется в DOM напрямую при drag, без React-ререндера)
  // animationDelay тоже inline — entrance keyframe запускается один раз при mount
  const sortableStyle: React.CSSProperties = {
    ...dndStyle,
    position: 'relative' as const,
    animationDelay: `${Math.min(index, 20) * 30}ms`,
    // Виртуализация без удаления из DOM: браузер пропускает рендер/лейаут офф-скрин
    // карточек (критично на сотнях нод). При drag выключаем, чтобы не мешать перетаскиванию.
    contentVisibility: isDragging || isOverlay ? 'visible' : 'auto',
    containIntrinsicSize: 'auto 320px',
  }

  // Во время drag настоящая карточка остаётся на месте приглушённым placeholder'ом,
  // за курсором едет копия в DragOverlay
  const dragClass = isOverlay
    ? 'shadow-2xl shadow-accent-500/20 ring-2 ring-accent-500/30'
    : isDragging
      ? 'opacity-30'
      : ''
  const disabledDragClass = isOverlay
    ? 'opacity-50 shadow-2xl ring-2 ring-dark-600/30'
    : isDragging
      ? 'opacity-30'
      : 'opacity-50'

  // CSS-анимация появления с fill-mode: forwards навсегда перебивает inline-transform
  // от dnd-kit (анимации в каскаде сильнее inline-стилей) — карточки не раздвигались
  // при перетаскивании. После завершения анимации снимаем класс с обёртки.
  const [entered, setEntered] = useState(isOverlay)
  const wrapperProps = {
    style: sortableStyle,
    className: entered ? undefined : 'card-enter',
    onAnimationEnd: entered
      ? undefined
      : (e: React.AnimationEvent<HTMLDivElement>) => {
          if (e.animationName === 'card-enter') setEntered(true)
        },
  }

  const metrics = server.metrics

  const handleClick = () => {
    navigate(`/${uid}/server/${server.id}`)
  }

  // Server disabled (monitoring off)
  if (!server.is_active) {
    if (compact) {
      return (
        <div ref={cardRef} {...wrapperProps}>
          <div
            className={`server-card card group cursor-pointer ${disabledDragClass}`}
            onClick={handleClick}
          >
            <div className="flex items-center gap-4">
              <button
                {...handle?.attributes}
                {...handle?.listeners}
                className="p-1.5 text-dark-600 hover:text-dark-400 cursor-grab active:cursor-grabbing
                           hover:bg-dark-800 rounded-lg transition-colors touch-none"
                onClick={(e) => e.stopPropagation()}
              >
                <GripVertical className="w-5 h-5" />
              </button>

              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-3">
                  <h3 className="font-semibold text-dark-400 truncate">
                    {server.name}
                  </h3>
                  <CopyableIp value={extractHost(server.url)} className="text-xs text-dark-600 font-mono hidden sm:inline" />
                  <span className="text-xs px-2 py-0.5 rounded-md bg-dark-700/50 text-dark-500">
                    {t('servers.disabled')}
                  </span>
                </div>
              </div>

              <div className="flex items-center">
                <ChevronRight className="w-5 h-5 text-dark-600" />
              </div>
            </div>
          </div>
        </div>
      )
    }

    return (
      <div ref={cardRef} {...wrapperProps}>
        <div
          className={`server-card card group cursor-pointer ${disabledDragClass}`}
          onClick={handleClick}
        >
          <div className="flex items-start justify-between mb-4">
            <div className="flex items-center gap-3">
              <button
                {...handle?.attributes}
                {...handle?.listeners}
                className="p-1.5 text-dark-600 hover:text-dark-400 cursor-grab active:cursor-grabbing
                           hover:bg-dark-800 rounded-lg transition-colors touch-none"
                onClick={(e) => e.stopPropagation()}
              >
                <GripVertical className="w-5 h-5" />
              </button>
              <div>
                <div className="flex items-center gap-2">
                  <h3 className="font-semibold text-dark-400">
                    {server.name}
                  </h3>
                  <CopyableIp value={extractHost(server.url)} className="text-xs text-dark-600 font-mono" />
                </div>
              </div>
            </div>
            <span className="text-xs px-2 py-1 rounded-lg bg-dark-700/50 text-dark-500">
              {t('servers.disabled')}
            </span>
          </div>

          <div className="h-24 flex flex-col items-center justify-center gap-3">
            <PowerOff className="w-8 h-8 text-dark-600" />
            <span className="text-dark-500 text-sm">{t('servers.monitoring_disabled')}</span>
          </div>

          <div className="absolute right-4 top-1/2 -translate-y-1/2 opacity-0 group-hover:opacity-100 transition-opacity">
            <ChevronRight className="w-5 h-5 text-dark-500" />
          </div>
        </div>
      </div>
    )
  }

  if (compact) {
    return (
      <div ref={cardRef} {...wrapperProps}>
        <div
          className={`server-card card group cursor-pointer ${dragClass}`}
          onClick={handleClick}
        >
          <div className="flex items-center gap-4">
            <button
              {...handle?.attributes}
              {...handle?.listeners}
              className="p-1.5 text-dark-500 hover:text-dark-300 cursor-grab active:cursor-grabbing
                         hover:bg-dark-800 rounded-lg transition-colors touch-none"
              onClick={(e) => e.stopPropagation()}
            >
              <GripVertical className="w-5 h-5" />
            </button>

            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-3">
                <h3 className="font-semibold text-dark-100 truncate">
                  {server.name}
                </h3>
                <CopyableIp value={extractHost(server.url)} className="text-xs text-dark-500 font-mono hidden sm:inline" />
                <StatusBadge status={server.status} showLabel={false} />
              </div>
            </div>

            {metrics && (
              <div className="hidden sm:flex items-center gap-6 text-sm">
                {server.status === 'offline' && (
                  <div className="flex items-center gap-1 px-1.5 py-0.5 bg-warning/15 border border-warning/25 rounded text-[10px] text-warning" title={t('cache.cached')}>
                    <Database className="w-2.5 h-2.5" />
                  </div>
                )}
                <div className="flex items-center gap-2 text-dark-400">
                  <Cpu className="w-4 h-4" />
                  <span className="font-mono">{metrics.cpu.usage_percent.toFixed(1)}%</span>
                </div>
                <div className="flex items-center gap-2 text-dark-400">
                  <MemoryStick className="w-4 h-4" />
                  <span className="font-mono">{metrics.memory.ram.percent.toFixed(1)}%</span>
                </div>
                {server.traffic && (server.traffic.rx_bytes > 0 || server.traffic.tx_bytes > 0) && (
                  <div className="flex items-center gap-2 text-dark-400">
                    <Network className="w-4 h-4" />
                    <span className="font-mono text-xs">
                      ↓{formatBytes(server.traffic.rx_bytes)} ↑{formatBytes(server.traffic.tx_bytes)}
                    </span>
                  </div>
                )}
                {metrics.certificates?.closest_expiry ? (
                  <div className={`flex items-center gap-2 ${
                    metrics.certificates.closest_expiry.expired
                      ? 'text-danger'
                      : metrics.certificates.closest_expiry.days_left < 30
                        ? 'text-warning'
                        : 'text-success'
                  }`}>
                    {metrics.certificates.closest_expiry.expired || metrics.certificates.closest_expiry.days_left < 30 ? (
                      <ShieldAlert className="w-4 h-4" />
                    ) : (
                      <ShieldCheck className="w-4 h-4" />
                    )}
                    <span className="font-mono text-xs truncate" title={metrics.certificates.closest_expiry.domain}>
                      {metrics.certificates.closest_expiry.domain} ({metrics.certificates.closest_expiry.expired
                        ? t('server_card.cert_expired')
                        : t('server_card.cert_days', { days: metrics.certificates.closest_expiry.days_left })})
                    </span>
                  </div>
                ) : (
                  <div className="flex items-center gap-2 text-dark-500">
                    <ShieldAlert className="w-4 h-4" />
                    <span className="font-mono text-xs">{t('server_card.no_certs')}</span>
                  </div>
                )}
                <div className="flex items-center gap-2 text-dark-400">
                  <Clock className="w-4 h-4" />
                  <span className="font-mono">{formatUptime(metrics.system.uptime_seconds)}</span>
                </div>
              </div>
            )}

            <div className="flex items-center">
              <ChevronRight className="w-5 h-5 text-dark-500 group-hover:text-accent-400 transition-colors" />
            </div>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div ref={cardRef} {...wrapperProps}>
      <div
        className={`server-card card group cursor-pointer ${dragClass}`}
        onClick={handleClick}
      >
        {/* Gradient border on hover */}
        <div
          className="absolute inset-0 rounded-2xl opacity-0 group-hover:opacity-100 transition-opacity duration-500 pointer-events-none"
          style={{
            background: 'linear-gradient(135deg, rgba(34,211,238,0.1) 0%, rgba(16,185,129,0.1) 100%)',
            zIndex: -1
          }}
        />

        <div className="flex items-start justify-between mb-4">
          <div className="flex items-center gap-3">
            <button
              {...handle?.attributes}
              {...handle?.listeners}
              className="p-1.5 text-dark-500 hover:text-dark-300 cursor-grab active:cursor-grabbing
                         hover:bg-dark-800 rounded-lg transition-colors touch-none"
              onClick={(e) => e.stopPropagation()}
            >
              <GripVertical className="w-5 h-5" />
            </button>
            <div>
              <div className="flex items-center gap-2">
                <h3 className="font-semibold text-dark-100 group-hover:text-white transition-colors">
                  {server.name}
                </h3>
                <CopyableIp value={extractHost(server.url)} className="text-xs text-dark-500 font-mono" />
              </div>
              {metrics && (
                <p className="text-xs text-dark-500 mt-0.5">
                  {metrics.system.hostname}
                </p>
              )}
            </div>
          </div>
          <div className="flex items-center gap-2">
            {server.status === 'offline' && metrics && (
              <div
                className="inline-flex items-center gap-1 px-1.5 py-0.5 bg-warning/15 border border-warning/25 rounded text-[10px] text-warning"
                title={server.last_seen ? t('cache.last_update', { time: formatTimeAgo(server.last_seen) }) : t('cache.cached')}
              >
                <Database className="w-2.5 h-2.5" />
                <span className="font-medium">{t('cache.cached')}</span>
              </div>
            )}
            {server.antiddos_emergency_mode && (
              <div className="flex items-center gap-1 px-1.5 py-0.5 rounded bg-red-500/15 border border-red-500/25 text-[10px] text-red-400" title={t('anti_ddos.mode_emergency')}>
                <ShieldAlert className="w-2.5 h-2.5" />
                <span className="font-medium">{t('anti_ddos.mode_emergency')}</span>
              </div>
            )}
            <StatusBadge status={server.status} />
          </div>
        </div>

        {metrics ? (
          <div>
            {detailLevel === 'minimal' && (
              <div className="flex items-center gap-4">
                <div className="flex items-center gap-2 flex-1">
                  <Cpu className="w-4 h-4 text-accent-500" />
                  <span className="text-sm text-dark-300">{t('common.cpu')}</span>
                  <span className="ml-auto text-sm font-mono text-dark-200">
                    {metrics.cpu.usage_percent.toFixed(1)}%
                  </span>
                </div>
                <div className="flex items-center gap-2 flex-1">
                  <MemoryStick className="w-4 h-4 text-accent-500" />
                  <span className="text-sm text-dark-300">{t('common.ram')}</span>
                  <span className="ml-auto text-sm font-mono text-dark-200">
                    {metrics.memory.ram.percent.toFixed(1)}%
                  </span>
                </div>
              </div>
            )}

            {detailLevel === 'standard' && (
              <>
                <div className="grid grid-cols-2 gap-4 mb-4">
                  <MetricItem
                    icon={<Cpu className="w-4 h-4" />}
                    label={t('common.cpu')}
                    value={metrics.cpu.usage_percent}
                  />
                  <MetricItem
                    icon={<MemoryStick className="w-4 h-4" />}
                    label={t('common.ram')}
                    value={metrics.memory.ram.percent}
                    usedBytes={metrics.memory.ram.used}
                    totalBytes={metrics.memory.ram.total}
                  />
                </div>

                <div className="grid grid-cols-2 gap-4 mb-4">
                  <MetricItem
                    icon={<HardDrive className="w-4 h-4" />}
                    label={t('common.disk')}
                    value={metrics.disk.partitions[0]?.percent || 0}
                    usedBytes={metrics.disk.partitions[0]?.used}
                    totalBytes={metrics.disk.partitions[0]?.total}
                  />
                  <div>
                    <div className="flex items-center gap-2 mb-2">
                      <Network className="w-4 h-4 text-accent-500" />
                      <span className="text-sm text-dark-300">{t('common.network')}</span>
                    </div>
                    <div className="flex gap-2 text-xs font-mono">
                      <span className="text-success flex items-center gap-1">
                        ↓ {formatBitsPerSecLocalized(
                          metrics.network.total?.rx_bytes_per_sec || 0,
                          t
                        )}
                      </span>
                      <span className="text-accent-400 flex items-center gap-1">
                        ↑ {formatBitsPerSecLocalized(
                          metrics.network.total?.tx_bytes_per_sec || 0,
                          t
                        )}
                      </span>
                    </div>
                  </div>
                </div>

                <div className="pt-3 border-t border-dark-700/50 flex flex-wrap items-center text-xs text-dark-400 gap-x-3 gap-y-1">
                  <div className="flex items-center gap-1.5">
                    <Clock className="w-3.5 h-3.5" />
                    <span>{formatUptime(metrics.system.uptime_seconds)}</span>
                  </div>
                  <div className="flex items-center gap-1.5" title={t('server_card.load_avg_tooltip', { cores: metrics.cpu.cores_logical })}>
                    <Activity className="w-3.5 h-3.5" />
                    <span className="font-mono">
                      <span className={getLoadAvgColor(metrics.cpu.load_avg_1, metrics.cpu.cores_logical)}>{metrics.cpu.load_avg_1.toFixed(2)}</span>
                      {' / '}
                      <span className={getLoadAvgColor(metrics.cpu.load_avg_5, metrics.cpu.cores_logical)}>{metrics.cpu.load_avg_5.toFixed(2)}</span>
                      {' / '}
                      <span className={getLoadAvgColor(metrics.cpu.load_avg_15, metrics.cpu.cores_logical)}>{metrics.cpu.load_avg_15.toFixed(2)}</span>
                    </span>
                  </div>
                  {metrics.certificates?.closest_expiry ? (
                    <div className={`flex items-center gap-1 ${
                      metrics.certificates.closest_expiry.expired
                        ? 'text-danger'
                        : metrics.certificates.closest_expiry.days_left < 30
                          ? 'text-warning'
                          : 'text-success'
                    }`}>
                      <ShieldCheck className="w-3.5 h-3.5 flex-shrink-0" />
                      <span className="truncate" title={metrics.certificates.closest_expiry.domain}>
                        {metrics.certificates.closest_expiry.expired
                          ? t('server_card.cert_expired_domain', { domain: metrics.certificates.closest_expiry.domain })
                          : t('server_card.cert_days_domain', {
                              domain: metrics.certificates.closest_expiry.domain,
                              days: metrics.certificates.closest_expiry.days_left
                            })}
                      </span>
                    </div>
                  ) : (
                    <div className="flex items-center gap-1 text-dark-500">
                      <ShieldAlert className="w-3.5 h-3.5" />
                      <span>{t('server_card.no_certs')}</span>
                    </div>
                  )}
                  <span className="text-dark-500 truncate max-w-[140px] ml-auto">{metrics.system.os}</span>
                </div>
              </>
            )}

            {detailLevel === 'detailed' && (
              <>
                <div className="grid grid-cols-2 gap-4 mb-4">
                  <CpuCoresItem
                    icon={<Cpu className="w-4 h-4" />}
                    label={t('common.cpu')}
                    totalUsage={metrics.cpu.usage_percent}
                    perCpuPercent={metrics.cpu.per_cpu_percent}
                  />
                  <MetricItem
                    icon={<MemoryStick className="w-4 h-4" />}
                    label={t('common.ram')}
                    value={metrics.memory.ram.percent}
                    usedBytes={metrics.memory.ram.used}
                    totalBytes={metrics.memory.ram.total}
                  />
                </div>

                <div className="grid grid-cols-2 gap-4 mb-4">
                  <MetricItem
                    icon={<HardDrive className="w-4 h-4" />}
                    label={t('common.disk')}
                    value={metrics.disk.partitions[0]?.percent || 0}
                    usedBytes={metrics.disk.partitions[0]?.used}
                    totalBytes={metrics.disk.partitions[0]?.total}
                  />
                  <div>
                    <div className="flex items-center gap-2 mb-2">
                      <Network className="w-4 h-4 text-accent-500" />
                      <span className="text-sm text-dark-300">{t('common.network')}</span>
                    </div>
                    <div className="flex gap-2 text-xs font-mono">
                      <span className="text-success flex items-center gap-1">
                        ↓ {formatBitsPerSecLocalized(
                          metrics.network.total?.rx_bytes_per_sec || 0,
                          t
                        )}
                      </span>
                      <span className="text-accent-400 flex items-center gap-1">
                        ↑ {formatBitsPerSecLocalized(
                          metrics.network.total?.tx_bytes_per_sec || 0,
                          t
                        )}
                      </span>
                    </div>
                  </div>
                </div>

                {server.traffic && (server.traffic.rx_bytes > 0 || server.traffic.tx_bytes > 0) && (
                  <div className="mb-4 p-3 bg-dark-800/40 rounded-xl border border-dark-700/30">
                    <div className="flex items-center justify-between">
                      <div className="flex items-center gap-2">
                        <span className="text-xs text-dark-500">
                          {t('server_card.traffic_period', { days: server.traffic.days })}
                        </span>
                      </div>
                      <div className="flex gap-3 text-xs font-mono">
                        <span className="text-success flex items-center gap-1">
                          <ArrowDownToLine className="w-3 h-3" />
                          {formatBytes(server.traffic.rx_bytes)}
                        </span>
                        <span className="text-accent-400 flex items-center gap-1">
                          <ArrowUpFromLine className="w-3 h-3" />
                          {formatBytes(server.traffic.tx_bytes)}
                        </span>
                      </div>
                    </div>
                  </div>
                )}

                <div className="pt-3 border-t border-dark-700/50 flex flex-wrap items-center text-xs text-dark-400 gap-x-3 gap-y-1">
                  <div className="flex items-center gap-1.5">
                    <Clock className="w-3.5 h-3.5" />
                    <span>{formatUptime(metrics.system.uptime_seconds)}</span>
                  </div>
                  <div className="flex items-center gap-1.5" title={t('server_card.load_avg_tooltip', { cores: metrics.cpu.cores_logical })}>
                    <Activity className="w-3.5 h-3.5" />
                    <span className="font-mono">
                      <span className={getLoadAvgColor(metrics.cpu.load_avg_1, metrics.cpu.cores_logical)}>{metrics.cpu.load_avg_1.toFixed(2)}</span>
                      {' / '}
                      <span className={getLoadAvgColor(metrics.cpu.load_avg_5, metrics.cpu.cores_logical)}>{metrics.cpu.load_avg_5.toFixed(2)}</span>
                      {' / '}
                      <span className={getLoadAvgColor(metrics.cpu.load_avg_15, metrics.cpu.cores_logical)}>{metrics.cpu.load_avg_15.toFixed(2)}</span>
                    </span>
                  </div>
                  {metrics.certificates?.closest_expiry ? (
                    <div className={`flex items-center gap-1 ${
                      metrics.certificates.closest_expiry.expired
                        ? 'text-danger'
                        : metrics.certificates.closest_expiry.days_left < 30
                          ? 'text-warning'
                          : 'text-success'
                    }`}>
                      <ShieldCheck className="w-3.5 h-3.5 flex-shrink-0" />
                      <span className="truncate" title={metrics.certificates.closest_expiry.domain}>
                        {metrics.certificates.closest_expiry.expired
                          ? t('server_card.cert_expired_domain', { domain: metrics.certificates.closest_expiry.domain })
                          : t('server_card.cert_days_domain', {
                              domain: metrics.certificates.closest_expiry.domain,
                              days: metrics.certificates.closest_expiry.days_left
                            })}
                      </span>
                    </div>
                  ) : (
                    <div className="flex items-center gap-1 text-dark-500">
                      <ShieldAlert className="w-3.5 h-3.5" />
                      <span>{t('server_card.no_certs')}</span>
                    </div>
                  )}
                  <span className="text-dark-500 truncate max-w-[140px] ml-auto">{metrics.system.os}</span>
                </div>
              </>
            )}
          </div>
        ) : server.status === 'loading' ? (
          <div className="h-32 flex items-center justify-center">
            <div className="spinner" />
          </div>
        ) : (
          <div className="h-32 flex flex-col items-center justify-center gap-3">
            <div>
              {server.status === 'error' ? (
                <AlertTriangle className="w-8 h-8 text-warning" />
              ) : (
                <WifiOff className="w-8 h-8 text-danger/70" />
              )}
            </div>
            <span className="text-danger font-medium text-sm">
              {server.last_error || t('server_card.server_unavailable')}
            </span>
            {server.error_code && (
              <span className="text-xs text-dark-600 bg-dark-800 px-2 py-0.5 rounded">
                {t('server_card.error_code', { code: server.error_code })}
              </span>
            )}
            {server.last_seen && (
              <span className="text-xs text-dark-600">
                {t('server_card.last_seen', { time: formatTimeAgo(server.last_seen) })}
              </span>
            )}
          </div>
        )}

        <div className="absolute right-4 top-1/2 -translate-y-1/2 opacity-0 group-hover:opacity-100 transition-opacity">
          <ChevronRight className="w-5 h-5 text-accent-400" />
        </div>
      </div>
    </div>
  )
}

function SortableServerCard(props: ServerCardProps) {
  const {
    attributes,
    listeners,
    setNodeRef,
    transform,
    transition,
    isDragging,
  } = useSortable({ id: props.server.id })

  const dndStyle: React.CSSProperties = {
    transform: CSS.Transform.toString(transform),
    transition,
    zIndex: isDragging ? 50 : undefined,
  }

  return (
    <ServerCardView
      {...props}
      cardRef={setNodeRef}
      dndStyle={dndStyle}
      isDragging={isDragging}
      handle={{ attributes, listeners }}
    />
  )
}

const ServerCard = memo(SortableServerCard, (prevProps, nextProps) => {
  const a = prevProps.server
  const b = nextProps.server
  if (a.id !== b.id) return false
  if (a.status !== b.status) return false
  if (a.is_active !== b.is_active) return false
  if (a.antiddos_emergency_mode !== b.antiddos_emergency_mode) return false
  if (prevProps.compact !== nextProps.compact) return false
  if (prevProps.detailLevel !== nextProps.detailLevel) return false
  if (prevProps.index !== nextProps.index) return false
  if (a.folder !== b.folder) return false

  const am = a.metrics
  const bm = b.metrics
  if (!am !== !bm) return false
  if (am && bm) {
    if (am.cpu.usage_percent !== bm.cpu.usage_percent) return false
    if (am.cpu.load_avg_1 !== bm.cpu.load_avg_1) return false
    if (am.memory.ram.percent !== bm.memory.ram.percent) return false
    if (am.memory.ram.used !== bm.memory.ram.used) return false
    if (am.disk.partitions[0]?.percent !== bm.disk.partitions[0]?.percent) return false
    if (am.network.total?.rx_bytes_per_sec !== bm.network.total?.rx_bytes_per_sec) return false
    if (am.network.total?.tx_bytes_per_sec !== bm.network.total?.tx_bytes_per_sec) return false
    if (am.system.uptime_seconds !== bm.system.uptime_seconds) return false
    // per_cpu_percent — мелкий массив, сравним длиной и поэлементно
    if (prevProps.detailLevel === 'detailed') {
      const ac = am.cpu.per_cpu_percent
      const bc = bm.cpu.per_cpu_percent
      if (ac.length !== bc.length) return false
      for (let i = 0; i < ac.length; i++) if (ac[i] !== bc[i]) return false
    }
  }

  if (a.traffic?.rx_bytes !== b.traffic?.rx_bytes) return false
  if (a.traffic?.tx_bytes !== b.traffic?.tx_bytes) return false

  return true
})

export default ServerCard

// Копия карточки для DragOverlay: без useSortable, иначе оверлей регистрирует второй
// draggable/droppable с тем же id и при размонтировании удаляет из реестра dnd-kit
// регистрацию настоящей карточки — сервер после первого drag переставал перетаскиваться.
export function ServerCardOverlay(props: ServerCardProps) {
  return <ServerCardView {...props} isOverlay />
}

interface MetricItemProps {
  icon: React.ReactNode
  label: string
  value: number
  usedBytes?: number
  totalBytes?: number
}

function MetricItem({ icon, label, value, usedBytes, totalBytes }: MetricItemProps) {
  return (
    <div>
      <div className="flex items-center gap-2 mb-2">
        <span className="text-accent-500">{icon}</span>
        <span className="text-sm text-dark-300">{label}</span>
        <span className="ml-auto text-sm font-mono text-dark-200">
          {value.toFixed(1)}%
        </span>
      </div>
      <ProgressBar value={value} size="sm" animated />
      {usedBytes !== undefined && totalBytes !== undefined && (
        <div className="text-xs text-dark-500 mt-1 font-mono">
          {formatBytes(usedBytes)} / {formatBytes(totalBytes)}
        </div>
      )}
    </div>
  )
}

interface CpuCoresItemProps {
  icon: React.ReactNode
  label: string
  totalUsage: number
  perCpuPercent: number[]
}

function getColorForPercent(percent: number): string {
  if (percent >= 80) return 'bg-danger'
  if (percent >= 50) return 'bg-warning'
  return 'bg-success'
}

function CpuCoresItem({ icon, label, totalUsage, perCpuPercent }: CpuCoresItemProps) {
  return (
    <div>
      <div className="flex items-center gap-2 mb-2">
        <span className="text-accent-500">{icon}</span>
        <span className="text-sm text-dark-300">{label}</span>
        <span className="ml-auto text-sm font-mono text-dark-200">
          {totalUsage.toFixed(1)}%
        </span>
      </div>
      <div className="space-y-1">
        {perCpuPercent.map((percent, index) => (
          <div key={index} className="flex items-center gap-2">
            <span className="text-xs font-mono text-dark-500 w-4 text-right">{index}</span>
            <div className="flex-1 h-1.5 bg-dark-800/60 rounded-full overflow-hidden">
              <div
                className={`cpu-core-fill ${getColorForPercent(percent)}`}
                style={{ width: `${Math.min(100, percent)}%` }}
              />
            </div>
            <span className="text-xs font-mono text-dark-400 w-10 text-right">{percent.toFixed(0)}%</span>
          </div>
        ))}
      </div>
    </div>
  )
}
