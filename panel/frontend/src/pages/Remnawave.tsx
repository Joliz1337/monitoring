import { useState, useEffect, useCallback, memo, type ReactNode } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import {
  Radio, Settings, Users, BarChart3, Check, X,
  Search, Play, Server, Trash2, Eye, EyeOff, ChevronDown, ChevronUp,
  Loader2, Smartphone, Globe, ArrowUp, ArrowDown, AlertTriangle, Shield, ExternalLink
} from 'lucide-react'
import { remnawaveApi } from '../api/client'
import type { RemnawaveApiNode, RemnawaveHwidDevice, RemnawaveAnomaly } from '../api/client'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import { useAutoRefresh } from '../hooks/useAutoRefresh'
import { Tooltip } from '../components/ui/Tooltip'
import { FAQIcon } from '../components/FAQ'

type TabType = 'overview' | 'users' | 'anomalies' | 'settings'

export default function Remnawave() {
  const { t } = useTranslation()
  const [activeTab, setActiveTab] = useState<TabType>('overview')

  const tabs: { id: TabType; label: string; icon: typeof Radio }[] = [
    { id: 'overview', label: t('remnawave.overview'), icon: BarChart3 },
    { id: 'users', label: t('remnawave.users'), icon: Users },
    { id: 'anomalies', label: t('remnawave.anomalies'), icon: Shield },
    { id: 'settings', label: t('remnawave.settings'), icon: Settings },
  ]

  return (
    <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} className="space-y-6">
      <motion.div
        className="flex items-center justify-between"
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.4 }}
      >
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-accent-500/20 to-blue-500/20 flex items-center justify-center">
            <Radio className="w-5 h-5 text-accent-400" />
          </div>
          <div>
            <h1 className="text-xl font-semibold text-dark-50 flex items-center gap-2">
              {t('common.remnawave', 'Remnawave')}
              <FAQIcon screen={activeTab === 'anomalies' ? 'REMNAWAVE_HWID_ANOMALIES' : 'PAGE_REMNAWAVE'} />
            </h1>
            <p className="text-sm text-dark-400">{t('remnawave.subtitle')}</p>
          </div>
        </div>
      </motion.div>

      <div className="flex gap-1 p-1 bg-dark-900/50 rounded-xl border border-dark-800/50 w-fit">
        {tabs.map(tab => (
          <button
            key={tab.id}
            onClick={() => setActiveTab(tab.id)}
            className={`flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition-all duration-200 ${
              activeTab === tab.id
                ? 'bg-accent-500/15 text-accent-400 shadow-sm'
                : 'text-dark-400 hover:text-dark-200 hover:bg-dark-800/50'
            }`}
          >
            <tab.icon className="w-4 h-4" />
            {tab.label}
          </button>
        ))}
      </div>

      <AnimatePresence mode="wait">
        {activeTab === 'overview' ? (
          <OverviewTab key="overview" />
        ) : activeTab === 'users' ? (
          <UsersTab key="users" />
        ) : activeTab === 'anomalies' ? (
          <AnomaliesTab key="anomalies" />
        ) : (
          <SettingsTab key="settings" />
        )}
      </AnimatePresence>
    </motion.div>
  )
}

function Section({ title, icon, children, right }: {
  title: string
  icon?: ReactNode
  children: ReactNode
  right?: ReactNode
}) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      className="card"
    >
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2 text-dark-200 text-sm font-medium">
          {icon}
          {title}
        </div>
        {right}
      </div>
      {children}
    </motion.div>
  )
}

function getFlag(code: string) {
  if (!code || code === 'XX') return '\uD83C\uDF10'
  return code
    .toUpperCase()
    .split('')
    .map(c => String.fromCodePoint(0x1F1E6 + c.charCodeAt(0) - 65))
    .join('')
}

function OverviewTab() {
  const { t } = useTranslation()
  const [summary, setSummary] = useState<any>(null)
  const [status, setStatus] = useState<any>(null)
  const [nodes, setNodes] = useState<RemnawaveApiNode[]>([])
  const [loading, setLoading] = useState(true)
  const [collecting, setCollecting] = useState(false)

  const fetchData = useCallback(async () => {
    try {
      const [summaryRes, statusRes, nodesRes] = await Promise.all([
        remnawaveApi.getSummary(),
        remnawaveApi.getCollectorStatus(),
        remnawaveApi.getNodes(),
      ])
      setSummary(summaryRes.data)
      setStatus(statusRes.data)
      setNodes(nodesRes.data.nodes || [])
    } catch { /* ignore */ } finally { setLoading(false) }
  }, [])

  useEffect(() => { fetchData() }, [fetchData])
  useAutoRefresh(fetchData, { customInterval: 60000 })

  const handleCollect = async () => {
    setCollecting(true)
    try {
      const res = await remnawaveApi.collectNow()
      if (res.data.success) {
        toast.success(t('remnawave.collectSuccess'))
        await fetchData()
      } else {
        toast.error(res.data.error || 'Error')
      }
    } catch { toast.error('Error') } finally { setCollecting(false) }
  }

  const connectedNodes = nodes.filter(n => n.is_connected && !n.is_disabled)
  const totalOnline = connectedNodes.reduce((s, n) => s + (n.users_online || 0), 0)

  return (
    <motion.div
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: -12 }}
      className="space-y-6"
    >
      <div className="flex items-center justify-end">
        <div className="flex items-center gap-3">
          {status && (
            <div className="flex items-center gap-2 text-sm text-dark-400">
              <div className={`w-2 h-2 rounded-full ${status.running ? 'bg-green-400 shadow-sm shadow-green-400/50' : 'bg-dark-600'}`} />
              {status.running ? t('remnawave.running') : t('remnawave.stopped')}
              {status.next_collect_in != null && (
                <span className="text-dark-500">({status.next_collect_in}s)</span>
              )}
            </div>
          )}
          <motion.button
            onClick={handleCollect}
            disabled={collecting}
            className="btn btn-primary text-sm"
            whileHover={{ scale: 1.02 }}
            whileTap={{ scale: 0.98 }}
          >
            {collecting ? <Loader2 className="w-4 h-4 animate-spin" /> : <Play className="w-4 h-4" />}
            {t('remnawave.collectNow')}
          </motion.button>
        </div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <StatCard title={t('remnawave.uniqueUsers')} value={summary?.unique_users ?? 0} loading={loading} />
        <StatCard title={t('remnawave.uniqueIps')} value={summary?.unique_ips ?? 0} loading={loading} />
        <StatCard title={t('remnawave.totalDevices')} value={summary?.total_devices ?? 0} loading={loading} />
      </div>
      <p className="text-dark-500 text-xs -mt-4">{t('remnawave.onlineOnlyHint')}</p>

      {nodes.length > 0 && (
        <Section title={t('remnawave.nodes')} icon={<Server className="w-4 h-4" />}
          right={<span className="text-dark-500 text-xs">{connectedNodes.length}/{nodes.length} {t('remnawave.nodesOnline')}, {totalOnline} {t('remnawave.nodesUsers')}</span>}
        >
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2">
            {nodes.map(node => (
              <div key={node.uuid} className="flex items-center justify-between py-2.5 px-4 bg-dark-800/40 rounded-xl border border-dark-800/30">
                <div className="flex items-center gap-2.5">
                  <span className="text-base">{getFlag(node.country_code)}</span>
                  <span className="text-dark-100 text-sm truncate max-w-[140px]">{node.name}</span>
                </div>
                <div className="flex items-center gap-2">
                  {node.is_connected && !node.is_disabled && (
                    <span className="text-dark-500 text-xs">{node.users_online}</span>
                  )}
                  <div className={`w-2 h-2 rounded-full ${
                    node.is_disabled ? 'bg-dark-600' :
                    node.is_connected ? 'bg-green-400 shadow-sm shadow-green-400/50' : 'bg-red-400'
                  }`} />
                </div>
              </div>
            ))}
          </div>
        </Section>
      )}
    </motion.div>
  )
}

const StatCard = memo(function StatCard({ title, value, loading }: { title: string; value: number; loading: boolean }) {
  return (
    <motion.div
      className="card"
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
    >
      <div className="text-dark-400 text-sm mb-1">{title}</div>
      {loading ? (
        <div className="h-8 bg-dark-700/50 rounded-lg animate-pulse w-24" />
      ) : (
        <div className="text-2xl font-bold text-dark-50">{value.toLocaleString()}</div>
      )}
    </motion.div>
  )
})

const STATUS_OPTIONS = ['ACTIVE', 'DISABLED', 'LIMITED', 'EXPIRED'] as const

function SortTh({ label, col, sortBy, sortDir, onSort, align = 'left' }: {
  label: string; col: string; sortBy: string; sortDir: 'asc' | 'desc'
  onSort: (col: string) => void; align?: 'left' | 'right'
}) {
  const active = sortBy === col
  return (
    <th
      onClick={() => onSort(col)}
      className={`px-5 py-3 font-medium cursor-pointer select-none transition-colors hover:text-dark-200 ${
        align === 'right' ? 'text-right' : 'text-left'
      } ${active ? 'text-accent-400' : ''}`}
    >
      <span className="inline-flex items-center gap-1">
        {align === 'right' && active && (sortDir === 'desc' ? <ArrowDown className="w-3 h-3" /> : <ArrowUp className="w-3 h-3" />)}
        {label}
        {align === 'left' && active && (sortDir === 'desc' ? <ArrowDown className="w-3 h-3" /> : <ArrowUp className="w-3 h-3" />)}
      </span>
    </th>
  )
}

function UsersTab() {
  const { t } = useTranslation()
  const [search, setSearch] = useState('')
  const [statusFilter, setStatusFilter] = useState('ACTIVE')
  const [ipFilter, setIpFilter] = useState('')
  const [users, setUsers] = useState<any[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [expandedUser, setExpandedUser] = useState<number | null>(null)
  const [userDetails, setUserDetails] = useState<any>(null)
  const [detailsLoading, setDetailsLoading] = useState(false)
  const [offset, setOffset] = useState(0)
  const [sortBy, setSortBy] = useState('unique_ips')
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('desc')
  const limit = 50

  const toggleSort = (col: string) => {
    if (sortBy === col) {
      setSortDir(d => d === 'desc' ? 'asc' : 'desc')
    } else {
      setSortBy(col)
      setSortDir('desc')
    }
    setOffset(0)
  }

  const fetchUsers = useCallback(async () => {
    setLoading(true)
    try {
      const res = await remnawaveApi.getTopUsers({
        limit, offset,
        search: search || undefined,
        status: statusFilter || undefined,
        source_ip: ipFilter || undefined,
        sort_by: sortBy,
        sort_dir: sortDir,
      })
      setUsers(res.data.users)
      setTotal(res.data.total)
    } catch { /* ignore */ } finally { setLoading(false) }
  }, [offset, search, statusFilter, ipFilter, sortBy, sortDir])

  useEffect(() => { fetchUsers() }, [fetchUsers])
  useEffect(() => { setOffset(0) }, [search, statusFilter, ipFilter])

  const toggleUser = async (email: number) => {
    if (expandedUser === email) {
      setExpandedUser(null)
      setUserDetails(null)
      return
    }
    setExpandedUser(email)
    setDetailsLoading(true)
    try {
      const res = await remnawaveApi.getUserStats(email)
      setUserDetails(res.data)
    } catch { toast.error('Error') } finally { setDetailsLoading(false) }
  }

  const deleteIp = async (email: number, ip: string) => {
    try {
      await remnawaveApi.clearUserIp(email, ip)
      toast.success(t('remnawave.ipDeleted'))
      if (expandedUser === email) await toggleUser(email)
      await fetchUsers()
    } catch { toast.error('Error') }
  }

  const deleteAllIps = async (email: number) => {
    try {
      await remnawaveApi.clearUserAllIps(email)
      toast.success(t('remnawave.allIpsDeleted'))
      setExpandedUser(null)
      setUserDetails(null)
      await fetchUsers()
    } catch { toast.error('Error') }
  }

  return (
    <motion.div
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: -12 }}
      className="space-y-4"
    >
      <div className="flex flex-col gap-3">
        <div className="flex items-center justify-between gap-4 flex-wrap">
          <div className="relative flex-1 min-w-[200px]">
            <Search className="absolute left-4 top-1/2 -translate-y-1/2 w-4 h-4 text-dark-500" />
            <input
              type="text"
              value={search}
              onChange={e => setSearch(e.target.value)}
              placeholder={t('remnawave.searchUsers')}
              className="input pl-11"
            />
          </div>
          <div className="relative min-w-[140px] max-w-xs">
            <Search className="absolute left-4 top-1/2 -translate-y-1/2 w-4 h-4 text-dark-500" />
            <input
              type="text"
              value={ipFilter}
              onChange={e => setIpFilter(e.target.value)}
              placeholder={t('remnawave.filterByIp')}
              className="input pl-11"
            />
          </div>
          <select
            value={statusFilter}
            onChange={e => setStatusFilter(e.target.value)}
            className="bg-dark-900/50 border border-dark-700/50 rounded-xl px-3 py-3 text-sm text-dark-200
                       focus:border-accent-500/50 focus:outline-none transition-all duration-300"
          >
            <option value="">{t('remnawave.allStatuses')}</option>
            {STATUS_OPTIONS.map(s => (
              <option key={s} value={s}>{s}</option>
            ))}
          </select>
        </div>
      </div>

      <div className="card overflow-hidden !p-0">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-dark-800/50 text-dark-400">
              <SortTh label={t('remnawave.user')} col="username" sortBy={sortBy} sortDir={sortDir} onSort={toggleSort} align="left" />
              <SortTh label={t('remnawave.status')} col="status" sortBy={sortBy} sortDir={sortDir} onSort={toggleSort} align="left" />
              <SortTh label={t('remnawave.ips')} col="unique_ips" sortBy={sortBy} sortDir={sortDir} onSort={toggleSort} align="right" />
              <SortTh label={t('remnawave.devices')} col="device_count" sortBy={sortBy} sortDir={sortDir} onSort={toggleSort} align="right" />
              <th className="w-10"></th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              Array.from({ length: 5 }).map((_, i) => (
                <tr key={i} className="border-b border-dark-800/30">
                  <td colSpan={5} className="px-5 py-3"><div className="h-5 bg-dark-700/50 rounded-lg animate-pulse" /></td>
                </tr>
              ))
            ) : users.length === 0 ? (
              <tr><td colSpan={5} className="text-center py-12 text-dark-500">{t('remnawave.noData')}</td></tr>
            ) : (
              users.map(user => (
                <UserRow
                  key={user.email}
                  user={user}
                  expanded={expandedUser === user.email}
                  details={expandedUser === user.email ? userDetails : null}
                  detailsLoading={expandedUser === user.email && detailsLoading}
                  onToggle={() => toggleUser(user.email)}
                  onDeleteIp={(ip) => deleteIp(user.email, ip)}
                  onDeleteAllIps={() => deleteAllIps(user.email)}
                />
              ))
            )}
          </tbody>
        </table>
      </div>

      {total > limit && (
        <div className="flex items-center justify-center gap-4">
          <motion.button
            onClick={() => setOffset(Math.max(0, offset - limit))}
            disabled={offset === 0}
            className="btn btn-secondary text-sm"
            whileHover={{ scale: offset === 0 ? 1 : 1.02 }}
            whileTap={{ scale: offset === 0 ? 1 : 0.98 }}
          >
            {t('common.prev', 'Prev')}
          </motion.button>
          <span className="text-dark-400 text-sm">
            {offset + 1}-{Math.min(offset + limit, total)} / {total}
          </span>
          <motion.button
            onClick={() => setOffset(offset + limit)}
            disabled={offset + limit >= total}
            className="btn btn-secondary text-sm"
            whileHover={{ scale: offset + limit >= total ? 1 : 1.02 }}
            whileTap={{ scale: offset + limit >= total ? 1 : 0.98 }}
          >
            {t('common.next', 'Next')}
          </motion.button>
        </div>
      )}
    </motion.div>
  )
}

function UserRow({ user, expanded, details, detailsLoading, onToggle, onDeleteIp, onDeleteAllIps }: {
  user: any; expanded: boolean; details: any; detailsLoading: boolean;
  onToggle: () => void; onDeleteIp: (ip: string) => void; onDeleteAllIps: () => void
}) {
  const { t } = useTranslation()
  const statusColors: Record<string, string> = {
    ACTIVE: 'text-green-400', DISABLED: 'text-red-400', LIMITED: 'text-yellow-400', EXPIRED: 'text-orange-400'
  }

  return (
    <>
      <tr
        onClick={onToggle}
        className={`border-b border-dark-800/30 cursor-pointer transition-colors duration-200 ${expanded ? 'bg-dark-800/40' : 'hover:bg-dark-800/30'}`}
      >
        <td className="px-5 py-3">
          <div className="font-medium text-dark-100">{user.username || `#${user.email}`}</div>
          {user.username && <div className="text-dark-500 text-xs">ID: {user.email}</div>}
        </td>
        <td className="px-5 py-3">
          <span className={`inline-flex items-center px-2 py-0.5 rounded-md text-xs font-medium ${statusColors[user.status] || 'text-dark-400'} ${
            user.status === 'ACTIVE' ? 'bg-green-400/10' :
            user.status === 'DISABLED' ? 'bg-red-400/10' :
            user.status === 'LIMITED' ? 'bg-yellow-400/10' :
            user.status === 'EXPIRED' ? 'bg-orange-400/10' : ''
          }`}>
            {user.status || '\u2014'}
          </span>
        </td>
        <td className="px-5 py-3 text-right text-dark-300 font-mono">{user.unique_ips || 0}</td>
        <td className="px-5 py-3 text-right text-dark-300">
          {user.device_count > 0 && (
            <span className="inline-flex items-center gap-1">
              <Smartphone className="w-3 h-3 text-dark-500" />
              {user.device_count}
            </span>
          )}
        </td>
        <td className="px-5 py-3 text-right">
          {expanded ? <ChevronUp className="w-4 h-4 text-dark-500" /> : <ChevronDown className="w-4 h-4 text-dark-500" />}
        </td>
      </tr>
      <AnimatePresence>
        {expanded && (
          <tr>
            <td colSpan={5} className="p-0">
              <motion.div
                initial={{ height: 0, opacity: 0 }}
                animate={{ height: 'auto', opacity: 1 }}
                exit={{ height: 0, opacity: 0 }}
                transition={{ duration: 0.2 }}
                className="overflow-hidden"
              >
                <div className="px-6 py-4 bg-dark-900/40 border-b border-dark-800/30">
                  {detailsLoading ? (
                    <div className="space-y-2">
                      {[1,2,3].map(i => <div key={i} className="h-8 bg-dark-700/50 rounded-lg animate-pulse" />)}
                    </div>
                  ) : details ? (
                    <div className="space-y-4">
                      <div className="flex items-center justify-between">
                        <span className="text-dark-400 text-sm">
                          {details.unique_ips} IP
                        </span>
                        <motion.button
                          onClick={(e) => { e.stopPropagation(); onDeleteAllIps() }}
                          className="btn-danger flex items-center gap-1.5 px-2.5 py-1 rounded-lg text-xs"
                          whileHover={{ scale: 1.02 }}
                          whileTap={{ scale: 0.98 }}
                        >
                          <Trash2 className="w-3 h-3" />
                          {t('remnawave.deleteAllIps')}
                        </motion.button>
                      </div>

                      {details.ips?.length > 0 && (
                        <div className="space-y-1">
                          <div className="flex items-center gap-1.5 text-dark-400 text-xs font-medium mb-1.5">
                            <Globe className="w-3 h-3" />
                            {t('remnawave.ipAddresses')}
                          </div>
                          {details.ips.map((ip: any) => (
                            <div key={ip.source_ip} className="flex items-center justify-between py-2 px-3 bg-dark-800/40 rounded-xl group transition-colors hover:bg-dark-800/60">
                              <div className="flex items-center gap-2">
                                <span className="text-dark-100 font-mono text-sm">{ip.source_ip}</span>
                                <a
                                  href={`https://check-host.net/ip-info?host=${ip.source_ip}`}
                                  target="_blank"
                                  rel="noopener noreferrer"
                                  onClick={e => e.stopPropagation()}
                                  className="text-dark-500 hover:text-accent-400 transition-colors"
                                >
                                  <ExternalLink className="w-3.5 h-3.5" />
                                </a>
                              </div>
                              <div className="flex items-center gap-3">
                                {ip.last_seen && (
                                  <span className="text-dark-500 text-xs">
                                    {new Date(ip.last_seen).toLocaleString()}
                                  </span>
                                )}
                                <Tooltip label={t('common.delete')}>
                                  <button
                                    onClick={(e) => { e.stopPropagation(); onDeleteIp(ip.source_ip) }}
                                    className="opacity-0 group-hover:opacity-100 p-1 hover:bg-danger/20 rounded-lg text-danger transition-all"
                                  >
                                    <Trash2 className="w-3.5 h-3.5" />
                                  </button>
                                </Tooltip>
                              </div>
                            </div>
                          ))}
                        </div>
                      )}

                      {details.devices?.length > 0 && (
                        <div className="space-y-1">
                          <div className="flex items-center gap-1.5 text-dark-400 text-xs font-medium mb-1.5">
                            <Smartphone className="w-3 h-3" />
                            {t('remnawave.hwidDevices')} ({details.devices.length})
                          </div>
                          {details.devices.map((dev: RemnawaveHwidDevice) => (
                            <div key={dev.hwid} className="flex items-center justify-between py-2 px-3 bg-dark-800/40 rounded-xl">
                              <div className="flex items-center gap-3">
                                <Smartphone className="w-3.5 h-3.5 text-dark-500" />
                                <div>
                                  <span className="text-dark-200 text-sm">
                                    {dev.device_model || dev.platform || 'Unknown'}
                                  </span>
                                  {dev.os_version && (
                                    <span className="text-dark-500 text-xs ml-2">{dev.os_version}</span>
                                  )}
                                </div>
                              </div>
                              {dev.created_at && (
                                <span className="text-dark-500 text-xs">
                                  {new Date(dev.created_at).toLocaleDateString()}
                                </span>
                              )}
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                  ) : (
                    <div className="text-dark-500 text-sm">{t('remnawave.noData')}</div>
                  )}
                </div>
              </motion.div>
            </td>
          </tr>
        )}
      </AnimatePresence>
    </>
  )
}

const SEVERITY_STYLES: Record<string, { bg: string; text: string; border: string }> = {
  high: { bg: 'bg-red-500/10', text: 'text-red-400', border: 'border-red-500/30' },
  medium: { bg: 'bg-yellow-500/10', text: 'text-yellow-400', border: 'border-yellow-500/30' },
  low: { bg: 'bg-blue-500/10', text: 'text-blue-400', border: 'border-blue-500/30' },
}

function AnomaliesTab() {
  const { t } = useTranslation()
  const [anomalies, setAnomalies] = useState<RemnawaveAnomaly[]>([])
  const [summary, setSummary] = useState<any>(null)
  const [loading, setLoading] = useState(true)
  const [minutes, setMinutes] = useState(10)
  const [typeFilter, setTypeFilter] = useState('')
  const [expandedEmail, setExpandedEmail] = useState<number | null>(null)
  const [userDetails, setUserDetails] = useState<any>(null)
  const [detailsLoading, setDetailsLoading] = useState(false)

  const anomalyTypeLabels: Record<string, string> = {
    ip_exceeds_limit: t('remnawave.ipExceedsLimit'),
    hwid_exceeds_limit: t('remnawave.hwidExceedsLimit'),
    unknown_user_agent: t('remnawave.unknownUa'),
    traffic_exceeds_limit: t('remnawave.trafficExceedsLimit'),
    invalid_device_data: t('remnawave.invalidDeviceData'),
  }

  const fetchData = useCallback(async () => {
    setLoading(true)
    try {
      const res = await remnawaveApi.getAnomalies(minutes)
      setAnomalies(res.data.anomalies)
      setSummary(res.data.summary)
    } catch { /* ignore */ } finally { setLoading(false) }
  }, [minutes])

  useEffect(() => { fetchData() }, [fetchData])
  useAutoRefresh(fetchData, { customInterval: 60000 })

  const handleIgnore = async (userId: number, listType: 'ip' | 'hwid' | 'all') => {
    try {
      await remnawaveApi.addAnomalyIgnore(userId, listType)
      toast.success(`User #${userId} → ${listType} ignore`)
      await fetchData()
    } catch { toast.error('Error') }
  }

  const toggleProfile = async (email: number | null) => {
    if (!email || expandedEmail === email) {
      setExpandedEmail(null)
      setUserDetails(null)
      return
    }
    setExpandedEmail(email)
    setDetailsLoading(true)
    try {
      const res = await remnawaveApi.getUserStats(email)
      setUserDetails(res.data)
    } catch { toast.error('Error') } finally { setDetailsLoading(false) }
  }

  const deleteIp = async (email: number, ip: string) => {
    try {
      await remnawaveApi.clearUserIp(email, ip)
      toast.success(t('remnawave.ipDeleted'))
      if (expandedEmail === email) await toggleProfile(email)
    } catch { toast.error('Error') }
  }

  const deleteAllIps = async (email: number) => {
    try {
      await remnawaveApi.clearUserAllIps(email)
      toast.success(t('remnawave.allIpsDeleted'))
      setExpandedEmail(null)
      setUserDetails(null)
    } catch { toast.error('Error') }
  }

  const filtered = typeFilter
    ? anomalies.filter(a => a.type === typeFilter)
    : anomalies

  return (
    <motion.div
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: -12 }}
      className="space-y-6"
    >
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div className="flex items-center gap-3">
          <select
            value={minutes}
            onChange={e => setMinutes(parseInt(e.target.value))}
            className="bg-dark-900/50 border border-dark-700/50 rounded-xl px-3 py-2.5 text-sm text-dark-200 focus:border-accent-500/50 focus:outline-none"
          >
            <option value={5}>5 min</option>
            <option value={10}>10 min</option>
            <option value={30}>30 min</option>
            <option value={60}>60 min</option>
          </select>
          <select
            value={typeFilter}
            onChange={e => setTypeFilter(e.target.value)}
            className="bg-dark-900/50 border border-dark-700/50 rounded-xl px-3 py-2.5 text-sm text-dark-200 focus:border-accent-500/50 focus:outline-none"
          >
            <option value="">{t('remnawave.allTypes')}</option>
            <option value="ip_exceeds_limit">{t('remnawave.ipExceedsLimit')}</option>
            <option value="hwid_exceeds_limit">{t('remnawave.hwidExceedsLimit')}</option>
            <option value="unknown_user_agent">{t('remnawave.unknownUa')}</option>
            <option value="traffic_exceeds_limit">{t('remnawave.trafficExceedsLimit')}</option>
            <option value="invalid_device_data">{t('remnawave.invalidDeviceData')}</option>
          </select>
        </div>
        <motion.button
          onClick={fetchData}
          disabled={loading}
          className="btn btn-secondary text-sm"
          whileHover={{ scale: 1.02 }}
          whileTap={{ scale: 0.98 }}
        >
          {loading ? <Loader2 className="w-4 h-4 animate-spin" /> : null}
          {t('remnawave.refresh')}
        </motion.button>
      </div>

      {summary && (
        <div className="grid grid-cols-2 sm:grid-cols-5 gap-3">
          <div className="card !p-3">
            <div className="text-dark-500 text-xs mb-1">{t('remnawave.total')}</div>
            <div className={`text-xl font-bold ${summary.total > 0 ? 'text-red-400' : 'text-green-400'}`}>
              {summary.total}
            </div>
          </div>
          <div className="card !p-3">
            <div className="text-dark-500 text-xs mb-1">{t('remnawave.ipExceedsLimit')}</div>
            <div className={`text-xl font-bold ${summary.ip_exceeds > 0 ? 'text-red-400' : 'text-dark-300'}`}>
              {summary.ip_exceeds}
            </div>
          </div>
          <div className="card !p-3">
            <div className="text-dark-500 text-xs mb-1">{t('remnawave.hwidExceedsLimit')}</div>
            <div className={`text-xl font-bold ${summary.hwid_exceeds > 0 ? 'text-yellow-400' : 'text-dark-300'}`}>
              {summary.hwid_exceeds}
            </div>
          </div>
          <div className="card !p-3">
            <div className="text-dark-500 text-xs mb-1">{t('remnawave.unknownUa')}</div>
            <div className={`text-xl font-bold ${summary.unknown_ua > 0 ? 'text-blue-400' : 'text-dark-300'}`}>
              {summary.unknown_ua}
            </div>
          </div>
          <div className="card !p-3">
            <div className="text-dark-500 text-xs mb-1">{t('remnawave.trafficExceedsLimit')}</div>
            <div className={`text-xl font-bold ${summary.traffic_exceeds > 0 ? 'text-purple-400' : 'text-dark-300'}`}>
              {summary.traffic_exceeds}
            </div>
          </div>
          <div className="card !p-3">
            <div className="text-dark-500 text-xs mb-1">{t('remnawave.invalidDeviceData')}</div>
            <div className={`text-xl font-bold ${summary.invalid_device > 0 ? 'text-orange-400' : 'text-dark-300'}`}>
              {summary.invalid_device}
            </div>
          </div>
        </div>
      )}

      <div className="space-y-2">
        {loading ? (
          Array.from({ length: 4 }).map((_, i) => (
            <div key={i} className="card !p-3"><div className="h-6 bg-dark-700/50 rounded-lg animate-pulse" /></div>
          ))
        ) : filtered.length === 0 ? (
          <div className="card text-center py-12">
            <div className="text-green-400 text-lg font-medium">{t('remnawave.noAnomalies')}</div>
            <div className="text-dark-500 text-sm mt-1">{t('remnawave.allClear')}</div>
          </div>
        ) : (
          filtered.map((a, i) => {
            const style = SEVERITY_STYLES[a.severity] || SEVERITY_STYLES.low
            const isExpanded = expandedEmail !== null && expandedEmail === a.email
            return (
              <div key={`${a.type}-${a.email}-${i}`}>
                <motion.div
                  initial={{ opacity: 0, y: 8 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ delay: i * 0.02 }}
                  onClick={() => toggleProfile(a.email)}
                  className={`flex items-center gap-4 py-3 px-4 rounded-xl border ${style.border} ${style.bg} ${
                    a.email ? 'cursor-pointer hover:brightness-110 transition-all' : ''
                  } ${isExpanded ? 'rounded-b-none' : ''}`}
                >
                  <AlertTriangle className={`w-4 h-4 shrink-0 ${style.text}`} />
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="text-dark-100 text-sm font-medium">
                        {a.username || (a.email ? `#${a.email}` : 'Unknown')}
                      </span>
                      <span className={`px-1.5 py-0.5 rounded text-[10px] font-semibold uppercase ${style.text} ${style.bg}`}>
                        {anomalyTypeLabels[a.type] || a.type}
                      </span>
                      {a.status && (
                        <span className="text-dark-500 text-xs">{a.status}</span>
                      )}
                    </div>
                    <div className="text-dark-400 text-xs mt-0.5 truncate">{a.detail}</div>
                  </div>
                  <div className="flex items-center gap-2 shrink-0">
                    {a.limit != null && (
                      <div className="text-right">
                        <span className={`text-sm font-mono font-bold ${style.text}`}>{a.current}</span>
                        <span className="text-dark-500 text-xs">/{a.limit}</span>
                      </div>
                    )}
                    {a.email && (
                      <>
                        <Tooltip label={a.type === 'ip_exceeds_limit' ? t('remnawave.ignoreIp')
                          : a.type === 'traffic_exceeds_limit' ? t('remnawave.ignoreAll')
                          : t('remnawave.ignoreHwid')}>
                          <button
                            onClick={(e) => {
                              e.stopPropagation()
                              const listType = a.type === 'ip_exceeds_limit' ? 'ip'
                                : a.type === 'traffic_exceeds_limit' ? 'all'
                                : 'hwid'
                              handleIgnore(a.email!, listType)
                            }}
                            className="p-1.5 hover:bg-dark-700/50 rounded-lg text-dark-500 hover:text-dark-300 transition-colors"
                          >
                            <X className="w-3.5 h-3.5" />
                          </button>
                        </Tooltip>
                        {isExpanded
                          ? <ChevronUp className="w-4 h-4 text-dark-500" />
                          : <ChevronDown className="w-4 h-4 text-dark-500" />
                        }
                      </>
                    )}
                  </div>
                </motion.div>
                <AnimatePresence>
                  {isExpanded && (
                    <motion.div
                      initial={{ height: 0, opacity: 0 }}
                      animate={{ height: 'auto', opacity: 1 }}
                      exit={{ height: 0, opacity: 0 }}
                      transition={{ duration: 0.2 }}
                      className={`overflow-hidden rounded-b-xl border border-t-0 ${style.border}`}
                    >
                      <div className="px-5 py-4 bg-dark-900/60">
                        {detailsLoading ? (
                          <div className="space-y-2">
                            {[1,2,3].map(j => <div key={j} className="h-8 bg-dark-700/50 rounded-lg animate-pulse" />)}
                          </div>
                        ) : userDetails ? (
                          <div className="space-y-4">
                            <div className="flex items-center justify-between">
                              <span className="text-dark-400 text-sm">
                                {userDetails.unique_ips} IP
                              </span>
                              <motion.button
                                onClick={(e) => { e.stopPropagation(); deleteAllIps(a.email!) }}
                                className="btn-danger flex items-center gap-1.5 px-2.5 py-1 rounded-lg text-xs"
                                whileHover={{ scale: 1.02 }}
                                whileTap={{ scale: 0.98 }}
                              >
                                <Trash2 className="w-3 h-3" />
                                {t('remnawave.deleteAllIps')}
                              </motion.button>
                            </div>

                            {userDetails.ips?.length > 0 && (
                              <div className="space-y-1">
                                <div className="flex items-center gap-1.5 text-dark-400 text-xs font-medium mb-1.5">
                                  <Globe className="w-3 h-3" />
                                  {t('remnawave.ipAddresses')}
                                </div>
                                {userDetails.ips.map((ip: any) => (
                                  <div key={ip.source_ip} className="flex items-center justify-between py-2 px-3 bg-dark-800/40 rounded-xl group transition-colors hover:bg-dark-800/60">
                                    <div className="flex items-center gap-2">
                                      <span className="text-dark-100 font-mono text-sm">{ip.source_ip}</span>
                                      <a
                                        href={`https://check-host.net/ip-info?host=${ip.source_ip}`}
                                        target="_blank"
                                        rel="noopener noreferrer"
                                        onClick={e => e.stopPropagation()}
                                        className="text-dark-500 hover:text-accent-400 transition-colors"
                                      >
                                        <ExternalLink className="w-3.5 h-3.5" />
                                      </a>
                                    </div>
                                    <div className="flex items-center gap-3">
                                      {ip.last_seen && (
                                        <span className="text-dark-500 text-xs">
                                          {new Date(ip.last_seen).toLocaleString()}
                                        </span>
                                      )}
                                      <Tooltip label={t('common.delete')}>
                                        <button
                                          onClick={(e) => { e.stopPropagation(); deleteIp(a.email!, ip.source_ip) }}
                                          className="opacity-0 group-hover:opacity-100 p-1 hover:bg-danger/20 rounded-lg text-danger transition-all"
                                        >
                                          <Trash2 className="w-3.5 h-3.5" />
                                        </button>
                                      </Tooltip>
                                    </div>
                                  </div>
                                ))}
                              </div>
                            )}

                            {userDetails.devices?.length > 0 && (
                              <div className="space-y-1">
                                <div className="flex items-center gap-1.5 text-dark-400 text-xs font-medium mb-1.5">
                                  <Smartphone className="w-3 h-3" />
                                  {t('remnawave.hwidDevices')} ({userDetails.devices.length})
                                </div>
                                {userDetails.devices.map((dev: RemnawaveHwidDevice) => (
                                  <div key={dev.hwid} className="flex items-center justify-between py-2 px-3 bg-dark-800/40 rounded-xl">
                                    <div className="flex items-center gap-3">
                                      <Smartphone className="w-3.5 h-3.5 text-dark-500" />
                                      <div>
                                        <span className="text-dark-200 text-sm">
                                          {dev.device_model || dev.platform || 'Unknown'}
                                        </span>
                                        {dev.os_version && (
                                          <span className="text-dark-500 text-xs ml-2">{dev.os_version}</span>
                                        )}
                                      </div>
                                    </div>
                                    {dev.created_at && (
                                      <span className="text-dark-500 text-xs">
                                        {new Date(dev.created_at).toLocaleDateString()}
                                      </span>
                                    )}
                                  </div>
                                ))}
                              </div>
                            )}
                          </div>
                        ) : (
                          <div className="text-dark-500 text-sm">{t('remnawave.noData')}</div>
                        )}
                      </div>
                    </motion.div>
                  )}
                </AnimatePresence>
              </div>
            )
          })
        )}
      </div>
    </motion.div>
  )
}

function SettingsTab() {
  const { t } = useTranslation()
  const [settings, setSettings] = useState<any>(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [testing, setTesting] = useState(false)
  const [showToken, setShowToken] = useState(false)
  const [showCookie, setShowCookie] = useState(false)
  const [showAnomalyToken, setShowAnomalyToken] = useState(false)
  const [form, setForm] = useState({
    api_url: '', api_token: '', cookie_secret: '', enabled: false, collection_interval: 300,
    anomaly_enabled: false, anomaly_use_custom_bot: false,
    anomaly_tg_bot_token: '', anomaly_tg_chat_id: '',
    traffic_anomaly_enabled: false, traffic_threshold_gb: 30, traffic_confirm_count: 2,
  })
  const [dirty, setDirty] = useState<Set<string>>(new Set())
  const [ignoredUsers, setIgnoredUsers] = useState<any[]>([])
  const [ignoreLists, setIgnoreLists] = useState<{ all: any[]; ip: any[]; hwid: any[] }>({ all: [], ip: [], hwid: [] })
  const [newIgnoredId, setNewIgnoredId] = useState('')

  const updateField = <K extends keyof typeof form>(key: K, value: (typeof form)[K]) => {
    setForm(f => ({ ...f, [key]: value }))
    setDirty(d => new Set(d).add(key))
  }

  const fetchSettings = useCallback(async () => {
    try {
      const [settingsRes, ignoredRes, ignoreListsRes] = await Promise.all([
        remnawaveApi.getSettings(),
        remnawaveApi.getIgnoredUsers(),
        remnawaveApi.getIgnoreLists(),
      ])
      const s = settingsRes.data
      setSettings(s)
      setForm({
        api_url: s.api_url || '',
        api_token: '',
        cookie_secret: '',
        enabled: s.enabled,
        collection_interval: s.collection_interval,
        anomaly_enabled: s.anomaly_enabled || false,
        anomaly_use_custom_bot: s.anomaly_use_custom_bot || false,
        anomaly_tg_bot_token: '',
        anomaly_tg_chat_id: s.anomaly_tg_chat_id || '',
        traffic_anomaly_enabled: s.traffic_anomaly_enabled || false,
        traffic_threshold_gb: s.traffic_threshold_gb || 30,
        traffic_confirm_count: s.traffic_confirm_count || 2,
      })
      setIgnoredUsers(ignoredRes.data.ignored_users || [])
      setIgnoreLists(ignoreListsRes.data)
      setDirty(new Set())
    } catch { /* ignore */ } finally { setLoading(false) }
  }, [])

  useEffect(() => { fetchSettings() }, [fetchSettings])

  const handleSave = async () => {
    if (dirty.size === 0) return
    setSaving(true)
    try {
      const data: any = {}
      for (const key of dirty) {
        const val = form[key as keyof typeof form]
        if (typeof val === 'string' && val === '') continue
        data[key] = val
      }
      if (Object.keys(data).length === 0) return setSaving(false)
      await remnawaveApi.updateSettings(data)
      toast.success(t('remnawave.settingsSaved'))
      await fetchSettings()
    } catch { toast.error('Error') } finally { setSaving(false) }
  }

  const handleTest = async () => {
    setTesting(true)
    try {
      await handleSave()
      const res = await remnawaveApi.testConnection()
      if (res.data.success) toast.success(t('remnawave.connectionOk'))
      else toast.error(res.data.error || 'Connection failed')
    } catch { toast.error('Error') } finally { setTesting(false) }
  }

  const addIgnoredUser = async () => {
    if (!newIgnoredId) return
    try {
      await remnawaveApi.addIgnoredUser(parseInt(newIgnoredId))
      setNewIgnoredId('')
      await fetchSettings()
    } catch { toast.error('Error') }
  }

  const removeIgnoredUser = async (userId: number) => {
    try {
      await remnawaveApi.removeIgnoredUser(userId)
      await fetchSettings()
    } catch { toast.error('Error') }
  }

  const removeFromIgnoreList = async (listType: 'all' | 'ip' | 'hwid', userId: number) => {
    try {
      await remnawaveApi.removeFromIgnoreList(listType, userId)
      await fetchSettings()
    } catch { toast.error('Error') }
  }

  const clearAllStats = async () => {
    if (!confirm(t('remnawave.confirmClear'))) return
    try {
      await remnawaveApi.clearStats()
      toast.success(t('remnawave.statsCleared'))
    } catch { toast.error('Error') }
  }

  if (loading) {
    return (
      <div className="space-y-4">
        {[1,2,3].map(i => (
          <div key={i} className="card p-5 space-y-4">
            <div className="h-4 w-32 bg-dark-700/50 rounded-lg animate-pulse" />
            <div className="h-10 bg-dark-700/30 rounded-xl animate-pulse" />
            <div className="h-10 bg-dark-700/30 rounded-xl animate-pulse" />
          </div>
        ))}
      </div>
    )
  }

  return (
    <motion.div
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: -12 }}
      className="space-y-6"
    >
      <div className="grid grid-cols-[repeat(auto-fill,minmax(340px,1fr))] gap-6 items-start">
          <Section title={t('remnawave.apiSettings')} icon={<Radio className="w-4 h-4" />}>
            <div className="space-y-4">
              <div>
                <label className="block text-sm text-dark-300 mb-2">API URL</label>
                <input type="text" value={form.api_url} onChange={e => updateField('api_url', e.target.value)}
                  placeholder="https://panel.example.com"
                  className="input" />
              </div>

              <div>
                <label className="block text-sm text-dark-300 mb-2">API Token</label>
                <div className="relative">
                  <input type={showToken ? 'text' : 'password'} value={form.api_token}
                    onChange={e => updateField('api_token', e.target.value)}
                    placeholder={settings?.api_token ? '***' : 'Enter token'}
                    className="input pr-10" />
                  <button onClick={() => setShowToken(!showToken)} className="absolute right-3 top-1/2 -translate-y-1/2 text-dark-500 hover:text-dark-300 transition-colors">
                    {showToken ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                  </button>
                </div>
              </div>

              <div>
                <label className="block text-sm text-dark-300 mb-2">Cookie Secret</label>
                <div className="relative">
                  <input type={showCookie ? 'text' : 'password'} value={form.cookie_secret}
                    onChange={e => updateField('cookie_secret', e.target.value)}
                    placeholder={settings?.cookie_secret ? '***' : 'name:value (optional)'}
                    className="input pr-10" />
                  <button onClick={() => setShowCookie(!showCookie)} className="absolute right-3 top-1/2 -translate-y-1/2 text-dark-500 hover:text-dark-300 transition-colors">
                    {showCookie ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                  </button>
                </div>
              </div>

              <motion.button onClick={handleTest} disabled={testing}
                className="btn btn-secondary text-sm"
                whileHover={{ scale: 1.02 }}
                whileTap={{ scale: 0.98 }}
              >
                {testing ? <Loader2 className="w-4 h-4 animate-spin" /> : null}
                {t('remnawave.testConnection')}
              </motion.button>
            </div>
          </Section>

          <Section title={t('remnawave.collectionSettings')} icon={<Settings className="w-4 h-4" />}>
            <div className="space-y-4">
              <div className="flex items-center justify-between">
                <span className="text-dark-300 text-sm">{t('remnawave.enabled')}</span>
                <button onClick={() => updateField('enabled', !form.enabled)}
                  className={`w-11 h-6 rounded-full transition-colors relative ${form.enabled ? 'bg-accent-500' : 'bg-dark-600'}`}>
                  <motion.div
                    className="w-5 h-5 bg-white rounded-full absolute top-0.5 shadow-sm"
                    animate={{ x: form.enabled ? 22 : 2 }}
                    transition={{ type: 'spring', stiffness: 500, damping: 30 }}
                  />
                </button>
              </div>

              <div>
                <label className="block text-sm text-dark-400 mb-2">{t('remnawave.interval')}</label>
                <input type="number" value={form.collection_interval} min={60} max={900}
                  onChange={e => updateField('collection_interval', parseInt(e.target.value) || 300)}
                  className="input" />
              </div>
            </div>
          </Section>

          <Section title={t('remnawave.anomalySettings')} icon={<Shield className="w-4 h-4" />}>
            <div className="space-y-4">
              <div className="flex items-center justify-between">
                <span className="text-dark-300 text-sm">{t('remnawave.anomalyEnabled')}</span>
                <button onClick={() => updateField('anomaly_enabled', !form.anomaly_enabled)}
                  className={`w-11 h-6 rounded-full transition-colors relative ${form.anomaly_enabled ? 'bg-accent-500' : 'bg-dark-600'}`}>
                  <motion.div
                    className="w-5 h-5 bg-white rounded-full absolute top-0.5 shadow-sm"
                    animate={{ x: form.anomaly_enabled ? 22 : 2 }}
                    transition={{ type: 'spring', stiffness: 500, damping: 30 }}
                  />
                </button>
              </div>

              <div className="flex items-center justify-between">
                <div>
                  <span className="text-dark-300 text-sm">{t('remnawave.useCustomBot')}</span>
                  <p className="text-dark-500 text-xs mt-0.5">{t('remnawave.useCustomBotHint')}</p>
                </div>
                <button onClick={() => updateField('anomaly_use_custom_bot', !form.anomaly_use_custom_bot)}
                  className={`w-11 h-6 rounded-full transition-colors relative shrink-0 ${form.anomaly_use_custom_bot ? 'bg-accent-500' : 'bg-dark-600'}`}>
                  <motion.div
                    className="w-5 h-5 bg-white rounded-full absolute top-0.5 shadow-sm"
                    animate={{ x: form.anomaly_use_custom_bot ? 22 : 2 }}
                    transition={{ type: 'spring', stiffness: 500, damping: 30 }}
                  />
                </button>
              </div>

              {form.anomaly_use_custom_bot && (
                <>
                  <div>
                    <label className="block text-sm text-dark-400 mb-2">Telegram Bot Token</label>
                    <div className="relative">
                      <input type={showAnomalyToken ? 'text' : 'password'} value={form.anomaly_tg_bot_token}
                        onChange={e => updateField('anomaly_tg_bot_token', e.target.value)}
                        placeholder={settings?.anomaly_tg_bot_token ? '***' : 'Bot token'}
                        className="input pr-10" />
                      <button onClick={() => setShowAnomalyToken(!showAnomalyToken)} className="absolute right-3 top-1/2 -translate-y-1/2 text-dark-500 hover:text-dark-300 transition-colors">
                        {showAnomalyToken ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                      </button>
                    </div>
                  </div>

                  <div>
                    <label className="block text-sm text-dark-400 mb-2">Telegram Chat ID</label>
                    <input type="text" value={form.anomaly_tg_chat_id}
                      onChange={e => updateField('anomaly_tg_chat_id', e.target.value)}
                      placeholder="Chat ID"
                      className="input" />
                  </div>
                </>
              )}
            </div>
          </Section>

          <Section title={t('remnawave.trafficAnomalySettings')} icon={<BarChart3 className="w-4 h-4" />}>
            <div className="space-y-4">
              <div className="flex items-center justify-between">
                <span className="text-dark-300 text-sm">{t('remnawave.trafficAnomalyEnabled')}</span>
                <button onClick={() => updateField('traffic_anomaly_enabled', !form.traffic_anomaly_enabled)}
                  className={`w-11 h-6 rounded-full transition-colors relative ${form.traffic_anomaly_enabled ? 'bg-accent-500' : 'bg-dark-600'}`}>
                  <motion.div
                    className="w-5 h-5 bg-white rounded-full absolute top-0.5 shadow-sm"
                    animate={{ x: form.traffic_anomaly_enabled ? 22 : 2 }}
                    transition={{ type: 'spring', stiffness: 500, damping: 30 }}
                  />
                </button>
              </div>
              <div>
                <label className="block text-sm text-dark-400 mb-2">{t('remnawave.trafficThreshold')}</label>
                <input type="number" value={form.traffic_threshold_gb} min={1} max={500} step={1}
                  onChange={e => updateField('traffic_threshold_gb', parseFloat(e.target.value) || 30)}
                  className="input" />
                <p className="text-dark-500 text-xs mt-1">{t('remnawave.trafficThresholdHint')}</p>
              </div>
              <div>
                <label className="block text-sm text-dark-400 mb-2">{t('remnawave.trafficConfirmCount')}</label>
                <input type="number" value={form.traffic_confirm_count} min={1} max={10}
                  onChange={e => updateField('traffic_confirm_count', parseInt(e.target.value) || 2)}
                  className="input" />
                <p className="text-dark-500 text-xs mt-1">{t('remnawave.trafficConfirmCountHint')}</p>
              </div>
            </div>
          </Section>
      </div>

      <motion.button onClick={handleSave} disabled={saving}
        className="btn btn-primary text-sm w-full justify-center"
        whileHover={{ scale: 1.01 }}
        whileTap={{ scale: 0.99 }}
      >
        {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Check className="w-4 h-4" />}
        {t('remnawave.save')}
      </motion.button>

      <div className="grid grid-cols-[repeat(auto-fill,minmax(280px,1fr))] gap-6">
        <Section title={t('remnawave.ignoredUsers')} icon={<Users className="w-4 h-4" />}>
          <div className="space-y-3">
            <div className="flex gap-2">
              <input type="text" value={newIgnoredId} onChange={e => setNewIgnoredId(e.target.value)}
                placeholder="User ID" onKeyDown={e => e.key === 'Enter' && addIgnoredUser()}
                className="input flex-1" />
              <motion.button onClick={addIgnoredUser}
                className="btn btn-primary text-sm"
                whileHover={{ scale: 1.02 }}
                whileTap={{ scale: 0.98 }}
              >
                {t('remnawave.add')}
              </motion.button>
            </div>

            {ignoredUsers.length > 0 && (
              <div className="space-y-1">
                {ignoredUsers.map(u => (
                  <div key={u.user_id} className="flex items-center justify-between py-2 px-4 bg-dark-800/40 rounded-xl transition-colors hover:bg-dark-800/60">
                    <span className="text-dark-100 text-sm">{u.username || `#${u.user_id}`}</span>
                    <Tooltip label={t('common.remove_from_list')}>
                      <button onClick={() => removeIgnoredUser(u.user_id)} className="p-1.5 hover:bg-danger/20 rounded-lg text-danger transition-colors">
                        <X className="w-3.5 h-3.5" />
                      </button>
                    </Tooltip>
                  </div>
                ))}
              </div>
            )}
          </div>
        </Section>

        <Section title={t('remnawave.ignoreIp')} icon={<Globe className="w-4 h-4" />}>
          {ignoreLists.ip.length === 0 ? (
            <p className="text-dark-500 text-sm">{t('remnawave.noData')}</p>
          ) : (
            <div className="space-y-1">
              {ignoreLists.ip.map(u => (
                <div key={u.user_id} className="flex items-center justify-between py-2 px-4 bg-dark-800/40 rounded-xl hover:bg-dark-800/60">
                  <span className="text-dark-100 text-sm">{u.username || `#${u.user_id}`}</span>
                  <Tooltip label={t('common.remove_from_list')}>
                    <button onClick={() => removeFromIgnoreList('ip', u.user_id)} className="p-1.5 hover:bg-danger/20 rounded-lg text-danger transition-colors">
                      <X className="w-3.5 h-3.5" />
                    </button>
                  </Tooltip>
                </div>
              ))}
            </div>
          )}
        </Section>

        <Section title={t('remnawave.ignoreHwid')} icon={<Smartphone className="w-4 h-4" />}>
          {ignoreLists.hwid.length === 0 ? (
            <p className="text-dark-500 text-sm">{t('remnawave.noData')}</p>
          ) : (
            <div className="space-y-1">
              {ignoreLists.hwid.map(u => (
                <div key={u.user_id} className="flex items-center justify-between py-2 px-4 bg-dark-800/40 rounded-xl hover:bg-dark-800/60">
                  <span className="text-dark-100 text-sm">{u.username || `#${u.user_id}`}</span>
                  <Tooltip label={t('common.remove_from_list')}>
                    <button onClick={() => removeFromIgnoreList('hwid', u.user_id)} className="p-1.5 hover:bg-danger/20 rounded-lg text-danger transition-colors">
                      <X className="w-3.5 h-3.5" />
                    </button>
                  </Tooltip>
                </div>
              ))}
            </div>
          )}
        </Section>
      </div>

      <motion.div
        initial={{ opacity: 0, y: 12 }}
        animate={{ opacity: 1, y: 0 }}
        className="card !border-danger/20"
      >
        <div className="flex items-center gap-2 text-danger text-sm font-medium mb-4">
          <Trash2 className="w-4 h-4" />
          {t('remnawave.dangerZone')}
        </div>
        <motion.button onClick={clearAllStats}
          className="btn-danger flex items-center gap-2 px-4 py-2 rounded-xl text-sm font-medium"
          whileHover={{ scale: 1.02 }}
          whileTap={{ scale: 0.98 }}
        >
          <Trash2 className="w-4 h-4" />
          {t('remnawave.clearAllStats')}
        </motion.button>
      </motion.div>
    </motion.div>
  )
}
