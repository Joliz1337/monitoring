import { memo } from 'react'
import { useSortable } from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'
import { useNavigate, useParams } from 'react-router-dom'
import { motion } from 'framer-motion'
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
} from 'lucide-react'
import { Server, ServerMetrics } from '../../api/client'
import StatusBadge from '../ui/StatusBadge'
import ProgressBar from '../ui/ProgressBar'
import { formatBytes, formatBitsPerSecLocalized, formatUptime, formatTimeAgo } from '../../utils/format'
import { useTranslation } from 'react-i18next'
import type { DetailLevel } from '../../stores/settingsStore'

// Extract IP/hostname from URL
function extractHost(url: string): string {
  try {
    const parsed = new URL(url)
    return parsed.hostname
  } catch {
    // Fallback: try to extract from string
    const match = url.match(/https?:\/\/([^:/]+)/)
    return match ? match[1] : url
  }
}

interface ServerTraffic {
  rx_bytes: number
  tx_bytes: number
  days: number
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

function ServerCardComponent({ server, compact, detailLevel = 'standard', index = 0 }: ServerCardProps) {
  const { uid } = useParams()
  const navigate = useNavigate()
  const { t } = useTranslation()
  
  const {
    attributes,
    listeners,
    setNodeRef,
    transform,
    transition,
    isDragging,
  } = useSortable({ id: server.id })
  
  const sortableStyle: React.CSSProperties = {
    transform: CSS.Transform.toString(transform),
    transition,
    zIndex: isDragging ? 50 : undefined,
    position: 'relative' as const,
  }
  
  const metrics = server.metrics
  
  const handleClick = () => {
    navigate(`/${uid}/server/${server.id}`)
  }

  const cardVariants = {
    hidden: { opacity: 0, y: 15, scale: 0.98 },
    visible: { 
      opacity: 1, 
      y: 0, 
      scale: 1,
      transition: { 
        duration: 0.25,
        delay: index * 0.03,
        ease: [0.4, 0, 0.2, 1]
      }
    },
    hover: {
      y: -4,
      transition: { duration: 0.2, ease: 'easeOut' }
    }
  }
  
  // Server disabled (monitoring off)
  if (!server.is_active) {
    if (compact) {
      return (
        <div ref={setNodeRef} style={sortableStyle}>
          <motion.div
            variants={cardVariants}
            initial="hidden"
            animate={isDragging ? undefined : "visible"}
            className={`server-card card group cursor-pointer transition-all duration-300 opacity-50 ${
              isDragging ? 'shadow-2xl ring-2 ring-dark-600/30' : ''
            }`}
            onClick={handleClick}
          >
            <div className="flex items-center gap-4">
              <button
                {...attributes}
                {...listeners}
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
                  <span className="text-xs text-dark-600 font-mono hidden sm:inline">{extractHost(server.url)}</span>
                  <span className="text-xs px-2 py-0.5 rounded-md bg-dark-700/50 text-dark-500">
                    {t('servers.disabled')}
                  </span>
                </div>
              </div>
              
              <div className="flex items-center">
                <ChevronRight className="w-5 h-5 text-dark-600" />
              </div>
            </div>
          </motion.div>
        </div>
      )
    }
    
    return (
      <div ref={setNodeRef} style={sortableStyle}>
        <motion.div
          variants={cardVariants}
          initial="hidden"
          animate={isDragging ? undefined : "visible"}
          className={`server-card card group cursor-pointer transition-all duration-300 opacity-50 ${
            isDragging ? 'shadow-2xl ring-2 ring-dark-600/30' : ''
          }`}
          onClick={handleClick}
        >
          <div className="flex items-start justify-between mb-4">
            <div className="flex items-center gap-3">
              <button
                {...attributes}
                {...listeners}
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
                  <span className="text-xs text-dark-600 font-mono">{extractHost(server.url)}</span>
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
        </motion.div>
      </div>
    )
  }
  
  if (compact) {
    return (
      <div ref={setNodeRef} style={sortableStyle}>
        <motion.div
          variants={cardVariants}
          initial="hidden"
          animate={isDragging ? undefined : "visible"}
          whileHover={isDragging ? undefined : "hover"}
          className={`server-card card group cursor-pointer transition-all duration-300 ${
            isDragging ? 'opacity-70 shadow-2xl shadow-accent-500/20 ring-2 ring-accent-500/30' : ''
          }`}
          onClick={handleClick}
        >
          <div className="flex items-center gap-4">
            <button
              {...attributes}
              {...listeners}
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
                <span className="text-xs text-dark-500 font-mono hidden sm:inline">{extractHost(server.url)}</span>
                <StatusBadge status={server.status} showLabel={false} />
              </div>
            </div>
            
            {metrics && (
              <div className="hidden sm:flex items-center gap-6 text-sm">
                {/* Cached indicator in compact view */}
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
                    <span className="font-mono text-xs truncate max-w-[120px]" title={metrics.certificates.closest_expiry.domain}>
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
        </motion.div>
      </div>
    )
  }
  
  return (
    <div ref={setNodeRef} style={sortableStyle}>
      <motion.div
        variants={cardVariants}
        initial="hidden"
        animate={isDragging ? undefined : "visible"}
        whileHover={isDragging ? undefined : "hover"}
        className={`server-card card group cursor-pointer transition-all duration-300 ${
          isDragging ? 'opacity-70 shadow-2xl shadow-accent-500/20 ring-2 ring-accent-500/30' : ''
        }`}
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
              {...attributes}
              {...listeners}
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
                <span className="text-xs text-dark-500 font-mono">{extractHost(server.url)}</span>
              </div>
              {metrics && (
                <p className="text-xs text-dark-500 mt-0.5">
                  {metrics.system.hostname}
                </p>
              )}
            </div>
          </div>
          <div className="flex items-center gap-2">
            {/* Show cached indicator when offline but have metrics */}
            {server.status === 'offline' && metrics && (
              <motion.div
                initial={{ opacity: 0, scale: 0.8 }}
                animate={{ opacity: 1, scale: 1 }}
                className="inline-flex items-center gap-1 px-1.5 py-0.5 bg-warning/15 border border-warning/25 rounded text-[10px] text-warning"
                title={server.last_seen ? t('cache.last_update', { time: formatTimeAgo(server.last_seen) }) : t('cache.cached')}
              >
                <Database className="w-2.5 h-2.5" />
                <span className="font-medium">{t('cache.cached')}</span>
              </motion.div>
            )}
            <StatusBadge status={server.status} />
          </div>
        </div>
        
        {metrics ? (
          <div>
            {/* Minimal: только CPU и RAM в одну строку */}
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
            
            {/* Standard: CPU (общий), RAM, диск, сеть, footer */}
            {detailLevel === 'standard' && (
              <>
                <div className="grid grid-cols-2 gap-4 mb-4">
                  <MetricItem
                    icon={<Cpu className="w-4 h-4" />}
                    label={t('common.cpu')}
                    value={metrics.cpu.usage_percent}
                    delay={0.1}
                  />
                  <MetricItem
                    icon={<MemoryStick className="w-4 h-4" />}
                    label={t('common.ram')}
                    value={metrics.memory.ram.percent}
                    delay={0.15}
                    usedBytes={metrics.memory.ram.used}
                    totalBytes={metrics.memory.ram.total}
                  />
                </div>
                
                <div className="grid grid-cols-2 gap-4 mb-4">
                  <MetricItem
                    icon={<HardDrive className="w-4 h-4" />}
                    label={t('common.disk')}
                    value={metrics.disk.partitions[0]?.percent || 0}
                    delay={0.2}
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
                          metrics.network.interfaces.reduce((acc, i) => acc + (i.rx_bytes_per_sec || 0), 0),
                          t
                        )}
                      </span>
                      <span className="text-accent-400 flex items-center gap-1">
                        ↑ {formatBitsPerSecLocalized(
                          metrics.network.interfaces.reduce((acc, i) => acc + (i.tx_bytes_per_sec || 0), 0),
                          t
                        )}
                      </span>
                    </div>
                  </div>
                </div>
                
                <div className="pt-3 border-t border-dark-700/50 flex items-center justify-between text-xs text-dark-400">
                  <div className="flex items-center gap-3">
                    <div className="flex items-center gap-1.5">
                      <Clock className="w-3.5 h-3.5" />
                      <span>{formatUptime(metrics.system.uptime_seconds)}</span>
                    </div>
                    {metrics.certificates?.closest_expiry ? (
                      <div className={`flex items-center gap-1 ${
                        metrics.certificates.closest_expiry.expired 
                          ? 'text-danger' 
                          : metrics.certificates.closest_expiry.days_left < 30 
                            ? 'text-warning' 
                            : 'text-success'
                      }`}>
                        {metrics.certificates.closest_expiry.expired || metrics.certificates.closest_expiry.days_left < 30 ? (
                          <ShieldAlert className="w-3.5 h-3.5" />
                        ) : (
                          <ShieldCheck className="w-3.5 h-3.5" />
                        )}
                        <span className="truncate max-w-[140px]" title={metrics.certificates.closest_expiry.domain}>
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
                  </div>
                  <span className="text-dark-500 truncate max-w-[120px]">{metrics.system.os}</span>
                </div>
              </>
            )}
            
            {/* Detailed: полный вид с CPU по ядрам и трафиком */}
            {detailLevel === 'detailed' && (
              <>
                <div className="grid grid-cols-2 gap-4 mb-4">
                  <CpuCoresItem
                    icon={<Cpu className="w-4 h-4" />}
                    label={t('common.cpu')}
                    totalUsage={metrics.cpu.usage_percent}
                    perCpuPercent={metrics.cpu.per_cpu_percent}
                    delay={0.1}
                  />
                  <MetricItem
                    icon={<MemoryStick className="w-4 h-4" />}
                    label={t('common.ram')}
                    value={metrics.memory.ram.percent}
                    delay={0.15}
                    usedBytes={metrics.memory.ram.used}
                    totalBytes={metrics.memory.ram.total}
                  />
                </div>
                
                <div className="grid grid-cols-2 gap-4 mb-4">
                  <MetricItem
                    icon={<HardDrive className="w-4 h-4" />}
                    label={t('common.disk')}
                    value={metrics.disk.partitions[0]?.percent || 0}
                    delay={0.2}
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
                          metrics.network.interfaces.reduce((acc, i) => acc + (i.rx_bytes_per_sec || 0), 0),
                          t
                        )}
                      </span>
                      <span className="text-accent-400 flex items-center gap-1">
                        ↑ {formatBitsPerSecLocalized(
                          metrics.network.interfaces.reduce((acc, i) => acc + (i.tx_bytes_per_sec || 0), 0),
                          t
                        )}
                      </span>
                    </div>
                  </div>
                </div>
                
                {/* Traffic summary */}
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
                
                <div className="pt-3 border-t border-dark-700/50 flex items-center justify-between text-xs text-dark-400">
                  <div className="flex items-center gap-3">
                    <div className="flex items-center gap-1.5">
                      <Clock className="w-3.5 h-3.5" />
                      <span>{formatUptime(metrics.system.uptime_seconds)}</span>
                    </div>
                    {metrics.certificates?.closest_expiry ? (
                      <div className={`flex items-center gap-1 ${
                        metrics.certificates.closest_expiry.expired 
                          ? 'text-danger' 
                          : metrics.certificates.closest_expiry.days_left < 30 
                            ? 'text-warning' 
                            : 'text-success'
                      }`}>
                        {metrics.certificates.closest_expiry.expired || metrics.certificates.closest_expiry.days_left < 30 ? (
                          <ShieldAlert className="w-3.5 h-3.5" />
                        ) : (
                          <ShieldCheck className="w-3.5 h-3.5" />
                        )}
                        <span className="truncate max-w-[140px]" title={metrics.certificates.closest_expiry.domain}>
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
                  </div>
                  <span className="text-dark-500 truncate max-w-[120px]">{metrics.system.os}</span>
                </div>
              </>
            )}
          </div>
        ) : server.status === 'loading' ? (
          <div className="h-32 flex items-center justify-center">
            <div className="relative">
              <motion.div
                className="w-10 h-10 border-2 border-accent-500/30 rounded-full"
                animate={{ rotate: 360 }}
                transition={{ duration: 1.5, repeat: Infinity, ease: 'linear' }}
              />
              <motion.div
                className="absolute inset-0 w-10 h-10 border-2 border-transparent border-t-accent-500 rounded-full"
                animate={{ rotate: 360 }}
                transition={{ duration: 1, repeat: Infinity, ease: 'linear' }}
              />
            </div>
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
        
        {/* Hover arrow indicator */}
        <div className="absolute right-4 top-1/2 -translate-y-1/2 opacity-0 group-hover:opacity-100 transition-opacity">
          <ChevronRight className="w-5 h-5 text-accent-400" />
        </div>
      </motion.div>
    </div>
  )
}

// Memoize ServerCard to prevent unnecessary re-renders
const ServerCard = memo(ServerCardComponent, (prevProps, nextProps) => {
  // Custom comparison - only re-render if important props changed
  return (
    prevProps.server.id === nextProps.server.id &&
    prevProps.server.status === nextProps.server.status &&
    prevProps.server.is_active === nextProps.server.is_active &&
    prevProps.compact === nextProps.compact &&
    prevProps.detailLevel === nextProps.detailLevel &&
    prevProps.index === nextProps.index &&
    JSON.stringify(prevProps.server.metrics?.cpu?.usage_percent) === JSON.stringify(nextProps.server.metrics?.cpu?.usage_percent) &&
    JSON.stringify(prevProps.server.metrics?.memory?.ram?.percent) === JSON.stringify(nextProps.server.metrics?.memory?.ram?.percent) &&
    JSON.stringify(prevProps.server.traffic) === JSON.stringify(nextProps.server.traffic)
  )
})

export default ServerCard

interface MetricItemProps {
  icon: React.ReactNode
  label: string
  value: number
  delay: number
  usedBytes?: number
  totalBytes?: number
}

function MetricItem({ icon, label, value, delay, usedBytes, totalBytes }: MetricItemProps) {
  return (
    <motion.div
      initial={{ opacity: 0, x: -10 }}
      animate={{ opacity: 1, x: 0 }}
      transition={{ delay }}
    >
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
    </motion.div>
  )
}

interface CpuCoresItemProps {
  icon: React.ReactNode
  label: string
  totalUsage: number
  perCpuPercent: number[]
  delay: number
}

function getColorForPercent(percent: number): string {
  if (percent >= 80) return 'bg-danger'
  if (percent >= 50) return 'bg-warning'
  return 'bg-success'
}

function CpuCoresItem({ icon, label, totalUsage, perCpuPercent, delay }: CpuCoresItemProps) {
  return (
    <motion.div
      initial={{ opacity: 0, x: -10 }}
      animate={{ opacity: 1, x: 0 }}
      transition={{ delay }}
    >
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
              <motion.div
                className={`h-full rounded-full ${getColorForPercent(percent)}`}
                initial={{ width: 0 }}
                animate={{ width: `${Math.min(100, percent)}%` }}
                transition={{ duration: 0.6, ease: [0.4, 0, 0.2, 1], delay: delay + index * 0.02 }}
              />
            </div>
            <span className="text-xs font-mono text-dark-400 w-10 text-right">{percent.toFixed(0)}%</span>
          </div>
        ))}
      </div>
    </motion.div>
  )
}
