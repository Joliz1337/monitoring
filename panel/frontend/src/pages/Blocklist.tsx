import { useEffect, useState, useCallback } from 'react'
import { Shield, Plus, Trash2, RefreshCw, Server, Globe, List, Loader2, ExternalLink, AlertCircle, Check, X, ArrowDownToLine, ArrowUpFromLine, CheckCircle2, XCircle, ChevronDown } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { motion, AnimatePresence } from 'framer-motion'
import { toast } from 'sonner'
import { blocklistApi, serversApi, Server as ServerType, BlocklistRule, BlocklistSource, BlocklistDirection } from '../api/client'
import { Skeleton } from '../components/ui/Skeleton'
import { Tooltip } from '../components/ui/Tooltip'
import { FAQIcon } from '../components/FAQ'

type TabType = 'global' | 'servers' | 'sources'

interface SimpleSyncToast {
  id: number
  status: 'syncing' | 'success' | 'error'
  message: string
}

interface ServerData {
  in: BlocklistRule[]
  out: BlocklistRule[]
  globalCountIn: number
  globalCountOut: number
  loading: boolean
  loaded: boolean
}

let toastIdCounter = 0

function RulesList({ rules, onDelete, emptyMessage }: {
  rules: BlocklistRule[]
  onDelete: (id: number) => void
  emptyMessage: string
}) {
  const { t } = useTranslation()
  if (rules.length === 0) {
    return <p className="text-dark-400 text-center py-6 text-sm">{emptyMessage}</p>
  }

  return (
    <div className="space-y-1.5 max-h-80 overflow-y-auto">
      {rules.map((rule) => (
        <div
          key={rule.id}
          className="flex items-center justify-between px-3 py-2 bg-dark-800/50 rounded-lg border border-dark-700/50"
        >
          <div className="flex items-center gap-3 min-w-0">
            <code className="text-sm text-dark-200 font-mono truncate">{rule.ip_cidr}</code>
            {rule.comment && (
              <span className="text-xs text-dark-500 truncate">{rule.comment}</span>
            )}
          </div>
          <Tooltip label={t('common.delete')}>
            <button
              onClick={() => onDelete(rule.id)}
              className="p-1 text-dark-400 hover:text-danger transition-colors shrink-0"
            >
              <Trash2 className="w-3.5 h-3.5" />
            </button>
          </Tooltip>
        </div>
      ))}
    </div>
  )
}

export default function Blocklist() {
  const { t } = useTranslation()

  const [activeTab, setActiveTab] = useState<TabType>('global')
  const [loading, setLoading] = useState(true)

  // Global rules (both directions)
  const [globalRulesIn, setGlobalRulesIn] = useState<BlocklistRule[]>([])
  const [globalRulesOut, setGlobalRulesOut] = useState<BlocklistRule[]>([])
  const [newGlobalIps, setNewGlobalIps] = useState('')
  const [newGlobalDirection, setNewGlobalDirection] = useState<'in' | 'out' | 'both'>('both')
  const [addingGlobal, setAddingGlobal] = useState(false)

  // Server rules (accordion)
  const [servers, setServers] = useState<ServerType[]>([])
  const [expandedServerIds, setExpandedServerIds] = useState<Set<number>>(new Set())
  const [serverDataMap, setServerDataMap] = useState<Record<number, ServerData>>({})
  const [serverAddForms, setServerAddForms] = useState<Record<number, { ip: string; direction: BlocklistDirection }>>({})
  const [addingServerId, setAddingServerId] = useState<number | null>(null)

  // Sources (both directions)
  const [sourcesIn, setSourcesIn] = useState<BlocklistSource[]>([])
  const [sourcesOut, setSourcesOut] = useState<BlocklistSource[]>([])
  const [newSourceName, setNewSourceName] = useState('')
  const [newSourceUrl, setNewSourceUrl] = useState('')
  const [newSourceDirection, setNewSourceDirection] = useState<BlocklistDirection>('in')
  const [addingSource, setAddingSource] = useState(false)
  const [refreshingSource, setRefreshingSource] = useState<number | null>(null)

  // Sync toasts
  const [syncToasts, setSyncToasts] = useState<SimpleSyncToast[]>([])

  // === Data fetching ===

  const fetchGlobalRulesIn = useCallback(async () => {
    try {
      const response = await blocklistApi.getGlobal('in')
      setGlobalRulesIn(response.data.rules)
    } catch (err) {
      console.error('Failed to fetch global rules (in):', err)
    }
  }, [])

  const fetchGlobalRulesOut = useCallback(async () => {
    try {
      const response = await blocklistApi.getGlobal('out')
      setGlobalRulesOut(response.data.rules)
    } catch (err) {
      console.error('Failed to fetch global rules (out):', err)
    }
  }, [])

  const fetchAllGlobalRules = useCallback(async () => {
    await Promise.all([fetchGlobalRulesIn(), fetchGlobalRulesOut()])
  }, [fetchGlobalRulesIn, fetchGlobalRulesOut])

  const fetchServers = useCallback(async () => {
    try {
      const response = await serversApi.list()
      const serverList = response.data.servers
      setServers(serverList)
      return serverList
    } catch (err) {
      console.error('Failed to fetch servers:', err)
      return []
    }
  }, [])

  const fetchServerRules = useCallback(async (serverId: number) => {
    setServerDataMap(prev => ({
      ...prev,
      [serverId]: {
        in: prev[serverId]?.in ?? [],
        out: prev[serverId]?.out ?? [],
        globalCountIn: prev[serverId]?.globalCountIn ?? 0,
        globalCountOut: prev[serverId]?.globalCountOut ?? 0,
        loading: true,
        loaded: prev[serverId]?.loaded ?? false,
      }
    }))

    try {
      const [inResp, outResp] = await Promise.all([
        blocklistApi.getServer(serverId, 'in'),
        blocklistApi.getServer(serverId, 'out')
      ])

      setServerDataMap(prev => ({
        ...prev,
        [serverId]: {
          in: inResp.data.rules,
          out: outResp.data.rules,
          globalCountIn: inResp.data.global_count,
          globalCountOut: outResp.data.global_count,
          loading: false,
          loaded: true,
        }
      }))
    } catch (err) {
      console.error('Failed to fetch server rules:', err)
      setServerDataMap(prev => ({
        ...prev,
        [serverId]: { ...prev[serverId], loading: false, loaded: true }
      }))
    }
  }, [])

  const fetchSourcesIn = useCallback(async () => {
    try {
      const response = await blocklistApi.getSources('in')
      setSourcesIn(response.data.sources)
    } catch (err) {
      console.error('Failed to fetch sources (in):', err)
    }
  }, [])

  const fetchSourcesOut = useCallback(async () => {
    try {
      const response = await blocklistApi.getSources('out')
      setSourcesOut(response.data.sources)
    } catch (err) {
      console.error('Failed to fetch sources (out):', err)
    }
  }, [])

  const fetchAllSources = useCallback(async () => {
    await Promise.all([fetchSourcesIn(), fetchSourcesOut()])
  }, [fetchSourcesIn, fetchSourcesOut])

  // === Sync toast ===

  const startSyncToast = useCallback(() => {
    const id = ++toastIdCounter
    const newToast: SimpleSyncToast = { id, status: 'syncing', message: t('blocklist.sync_applying') }
    setSyncToasts(prev => [newToast, ...prev].slice(0, 3))

    let attempts = 0
    const poll = setInterval(async () => {
      attempts++
      try {
        const resp = await blocklistApi.getSyncStatus()
        const data = resp.data
        if (!data.in_progress && data.servers && Object.keys(data.servers).length > 0) {
          clearInterval(poll)

          const failedServers = Object.values(data.servers).filter(s => !s.success)

          if (failedServers.length === 0) {
            setSyncToasts(prev =>
              prev.map(st => st.id === id ? { ...st, status: 'success' as const, message: t('blocklist.sync_success') } : st)
            )
          } else {
            const names = failedServers.map(s => s.server_name).join(', ')
            setSyncToasts(prev =>
              prev.map(st => st.id === id ? { ...st, status: 'error' as const, message: t('blocklist.sync_error', { servers: names }) } : st)
            )
          }

          setTimeout(() => {
            setSyncToasts(prev => prev.filter(st => st.id !== id))
          }, 15000)
        }
      } catch {
        // ignore polling errors
      }
      if (attempts > 30) {
        clearInterval(poll)
        setSyncToasts(prev => prev.filter(st => st.id !== id))
      }
    }, 1500)

    return () => clearInterval(poll)
  }, [t])

  const dismissToast = useCallback((id: number) => {
    setSyncToasts(prev => prev.filter(st => st.id !== id))
  }, [])

  const fetchAllServerRules = useCallback(async (serverList: ServerType[]) => {
    if (serverList.length === 0) return

    const results = await Promise.all(
      serverList.map(async (server) => {
        try {
          const [inResp, outResp] = await Promise.all([
            blocklistApi.getServer(server.id, 'in'),
            blocklistApi.getServer(server.id, 'out')
          ])
          return {
            serverId: server.id,
            data: {
              in: inResp.data.rules,
              out: outResp.data.rules,
              globalCountIn: inResp.data.global_count,
              globalCountOut: outResp.data.global_count,
              loading: false,
              loaded: true,
            } as ServerData
          }
        } catch {
          return {
            serverId: server.id,
            data: { in: [], out: [], globalCountIn: 0, globalCountOut: 0, loading: false, loaded: true } as ServerData
          }
        }
      })
    )

    const map: Record<number, ServerData> = {}
    for (const r of results) map[r.serverId] = r.data
    setServerDataMap(map)
  }, [])

  // === Initial load ===

  useEffect(() => {
    const loadData = async () => {
      setLoading(true)
      const [, serverList] = await Promise.all([
        fetchAllGlobalRules(),
        fetchServers(),
        fetchAllSources()
      ])
      await fetchAllServerRules(serverList as ServerType[])
      setLoading(false)
    }
    loadData()
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  // === Server accordion ===

  const toggleServer = useCallback((serverId: number) => {
    setExpandedServerIds(prev => {
      const next = new Set(prev)
      if (next.has(serverId)) {
        next.delete(serverId)
      } else {
        next.add(serverId)
      }
      return next
    })
  }, [])

  // === Handlers: Global Rules ===

  const handleAddGlobalRules = async () => {
    if (!newGlobalIps.trim()) return
    setAddingGlobal(true)
    try {
      const ips = newGlobalIps.split('\n').map(ip => ip.trim()).filter(Boolean)

      if (newGlobalDirection === 'both') {
        await Promise.all([
          blocklistApi.addGlobalBulk(ips, true, 'in'),
          blocklistApi.addGlobalBulk(ips, true, 'out')
        ])
      } else {
        await blocklistApi.addGlobalBulk(ips, true, newGlobalDirection)
      }

      setNewGlobalIps('')
      await fetchAllGlobalRules()
      startSyncToast()
      toast.success(t('blocklist.sync_success'))
    } catch (err: any) {
      console.error('Failed to add rules:', err)
      toast.error(err.response?.data?.detail || 'Failed to add rules')
    } finally {
      setAddingGlobal(false)
    }
  }

  const handleDeleteGlobalRule = async (ruleId: number) => {
    try {
      await blocklistApi.deleteGlobal(ruleId)
      await fetchAllGlobalRules()
      startSyncToast()
      toast.success(t('common.deleted'))
    } catch (err: any) {
      console.error('Failed to delete rule:', err)
      toast.error(t('common.action_failed'))
    }
  }

  // === Handlers: Server Rules ===

  const getServerForm = (serverId: number) =>
    serverAddForms[serverId] || { ip: '', direction: 'in' as BlocklistDirection }

  const updateServerForm = (serverId: number, patch: Partial<{ ip: string; direction: BlocklistDirection }>) => {
    setServerAddForms(prev => ({
      ...prev,
      [serverId]: { ...getServerForm(serverId), ...patch }
    }))
  }

  const handleAddServerRule = async (serverId: number) => {
    const form = getServerForm(serverId)
    if (!form.ip.trim()) return
    setAddingServerId(serverId)
    try {
      await blocklistApi.addServer(serverId, { ip_cidr: form.ip.trim(), direction: form.direction })
      updateServerForm(serverId, { ip: '' })
      await fetchServerRules(serverId)
      startSyncToast()
      toast.success(t('blocklist.sync_success'))
    } catch (err: any) {
      console.error('Failed to add rule:', err)
      toast.error(err.response?.data?.detail || 'Failed to add rule')
    } finally {
      setAddingServerId(null)
    }
  }

  const handleDeleteServerRule = async (serverId: number, ruleId: number) => {
    try {
      await blocklistApi.deleteServer(serverId, ruleId)
      await fetchServerRules(serverId)
      startSyncToast()
      toast.success(t('common.deleted'))
    } catch (err: any) {
      console.error('Failed to delete rule:', err)
      toast.error(t('common.action_failed'))
    }
  }

  // === Handlers: Sources ===

  const handleAddSource = async () => {
    if (!newSourceName.trim() || !newSourceUrl.trim()) return
    setAddingSource(true)
    try {
      await blocklistApi.addSource({ name: newSourceName.trim(), url: newSourceUrl.trim(), direction: newSourceDirection })
      setNewSourceName('')
      setNewSourceUrl('')
      await fetchAllSources()
      startSyncToast()
      toast.success(t('common.added'))
    } catch (err: any) {
      console.error('Failed to add source:', err)
      toast.error(err.response?.data?.detail || 'Failed to add source')
    } finally {
      setAddingSource(false)
    }
  }

  const handleToggleSource = async (sourceId: number, enabled: boolean) => {
    try {
      await blocklistApi.updateSource(sourceId, { enabled })
      await fetchAllSources()
      startSyncToast()
      toast.success(t('common.updated'))
    } catch (err: any) {
      console.error('Failed to update source:', err)
      toast.error(t('common.action_failed'))
    }
  }

  const handleRefreshSource = async (sourceId: number) => {
    setRefreshingSource(sourceId)
    try {
      const resp = await blocklistApi.refreshSource(sourceId)
      await fetchAllSources()
      if (resp.data.changed) {
        startSyncToast()
        toast.success(t('blocklist.sync_success'))
      } else {
        toast.success(t('common.refresh'))
      }
    } catch (err: any) {
      console.error('Failed to refresh source:', err)
      toast.error(err.response?.data?.detail || 'Failed to refresh')
    } finally {
      setRefreshingSource(null)
    }
  }

  const handleRefreshAllSources = async () => {
    setRefreshingSource(-1)
    try {
      const resp = await blocklistApi.refreshAll()
      await fetchAllSources()
      if (resp.data.any_changed) {
        startSyncToast()
        toast.success(t('blocklist.sync_success'))
      } else {
        toast.success(t('common.refresh'))
      }
    } catch (err: any) {
      console.error('Failed to refresh sources:', err)
      toast.error(t('common.action_failed'))
    } finally {
      setRefreshingSource(null)
    }
  }

  const handleDeleteSource = async (sourceId: number) => {
    if (!confirm(t('blocklist.confirm_delete_source'))) return
    try {
      await blocklistApi.deleteSource(sourceId)
      await fetchAllSources()
      startSyncToast()
      toast.success(t('common.deleted'))
    } catch (err: any) {
      console.error('Failed to delete source:', err)
      toast.error(err.response?.data?.detail || 'Cannot delete default source')
    }
  }

  // === Tab definitions ===

  const tabs = [
    { id: 'global' as TabType, icon: Globe, label: t('blocklist.global_rules') },
    { id: 'servers' as TabType, icon: Server, label: t('blocklist.server_rules') },
    { id: 'sources' as TabType, icon: List, label: t('blocklist.auto_lists') }
  ]

  const directionPills: { id: 'in' | 'out' | 'both'; label: string; color: string }[] = [
    { id: 'in', label: t('blocklist.direction_incoming'), color: 'bg-blue-500 text-white' },
    { id: 'out', label: t('blocklist.direction_outgoing'), color: 'bg-orange-500 text-white' },
    { id: 'both', label: t('blocklist.direction_both'), color: 'bg-accent-500 text-dark-950' }
  ]

  // === Loading skeleton ===

  if (loading) {
    return (
      <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} className="space-y-6">
        <div className="flex items-center gap-3">
          <Skeleton className="w-10 h-10 rounded-xl" />
          <div>
            <Skeleton className="h-6 w-40 mb-2" />
            <Skeleton className="h-4 w-64" />
          </div>
        </div>
        <div className="flex gap-2">
          {Array.from({ length: 3 }).map((_, i) => (
            <Skeleton key={i} className="h-10 w-28 rounded-xl" />
          ))}
        </div>
        <div className="card">
          <Skeleton className="h-5 w-36 mb-4" />
          <div className="space-y-3">
            {Array.from({ length: 3 }).map((_, i) => (
              <Skeleton key={i} className="h-12 w-full rounded-lg" />
            ))}
          </div>
        </div>
      </motion.div>
    )
  }

  // === Render ===

  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      className="space-y-6"
    >
      {/* Header */}
      <motion.div initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.4 }} className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <Shield className="w-7 h-7 text-accent-400" />
          <div>
            <h1 className="text-2xl font-bold text-dark-50 flex items-center gap-2">
              {t('blocklist.title')}
              <FAQIcon screen="PAGE_BLOCKLIST" />
            </h1>
            <p className="text-dark-400 text-sm">{t('blocklist.subtitle')}</p>
          </div>
        </div>
      </motion.div>

      {/* Sync Toasts */}
      <AnimatePresence>
        {syncToasts.map((st) => (
          <motion.div
            key={st.id}
            initial={{ opacity: 0, y: -10 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -10 }}
            className={`p-5 rounded-xl border flex items-center gap-3 ${
              st.status === 'syncing'
                ? 'bg-blue-500/10 border-blue-500/30'
                : st.status === 'success'
                ? 'bg-emerald-500/10 border-emerald-500/30'
                : 'bg-red-500/10 border-red-500/30'
            }`}
          >
            {st.status === 'syncing' && <Loader2 className="w-5 h-5 text-blue-400 animate-spin shrink-0" />}
            {st.status === 'success' && <CheckCircle2 className="w-5 h-5 text-success shrink-0" />}
            {st.status === 'error' && <XCircle className="w-5 h-5 text-danger shrink-0" />}
            <span className="text-base font-medium text-dark-100">{st.message}</span>
            {st.status !== 'syncing' && (
              <button onClick={() => dismissToast(st.id)} className="ml-auto text-dark-500 hover:text-dark-300">
                <X className="w-4 h-4" />
              </button>
            )}
          </motion.div>
        ))}
      </AnimatePresence>

      {/* Tabs */}
      <motion.div initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.4 }} className="flex gap-2 border-b border-dark-700 pb-2">
        {tabs.map((tab) => (
          <button
            key={tab.id}
            onClick={() => setActiveTab(tab.id)}
            className={`flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition-all ${
              activeTab === tab.id
                ? 'bg-accent-500 text-dark-950'
                : 'text-dark-400 hover:text-dark-200 hover:bg-dark-800'
            }`}
          >
            <tab.icon className="w-4 h-4" />
            {tab.label}
          </button>
        ))}
      </motion.div>

      {/* Tab Content */}
      <AnimatePresence mode="wait">

        {/* ========== GLOBAL RULES TAB ========== */}
        {activeTab === 'global' && (
          <motion.div
            key="global"
            initial={{ opacity: 0, x: -20 }}
            animate={{ opacity: 1, x: 0 }}
            exit={{ opacity: 0, x: 20 }}
            className="space-y-4"
          >
            {/* Add Form */}
            <div className="card">
              <h3 className="text-lg font-semibold text-dark-100 mb-2">
                {t('blocklist.add_global')}
              </h3>
              <p className="text-sm text-dark-400 mb-4">
                {t('blocklist.add_global_desc')}
              </p>

              <div className="space-y-3">
                <textarea
                  value={newGlobalIps}
                  onChange={(e) => setNewGlobalIps(e.target.value)}
                  placeholder={t('blocklist.ip_placeholder')}
                  rows={3}
                  className="input w-full resize-none font-mono text-sm"
                />

                <div className="flex items-center gap-3 flex-wrap">
                  <div className="flex gap-1.5">
                    {directionPills.map((pill) => (
                      <button
                        key={pill.id}
                        onClick={() => setNewGlobalDirection(pill.id)}
                        className={`px-3 py-1.5 rounded-lg text-sm font-medium transition-all ${
                          newGlobalDirection === pill.id
                            ? pill.color
                            : 'text-dark-400 hover:text-dark-200 bg-dark-800 border border-dark-700'
                        }`}
                      >
                        {pill.label}
                      </button>
                    ))}
                  </div>

                  <motion.button
                    onClick={handleAddGlobalRules}
                    disabled={addingGlobal || !newGlobalIps.trim()}
                    className="btn btn-primary"
                    whileHover={{ scale: 1.02 }}
                    whileTap={{ scale: 0.98 }}
                  >
                    {addingGlobal ? (
                      <Loader2 className="w-4 h-4 animate-spin" />
                    ) : (
                      <Plus className="w-4 h-4" />
                    )}
                    {t('blocklist.add')}
                  </motion.button>
                </div>
              </div>
            </div>

            {/* Two columns: IN and OUT */}
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              {/* Incoming rules */}
              <div className="card">
                <div className="flex items-center justify-between mb-3">
                  <div className="flex items-center gap-2">
                    <ArrowDownToLine className="w-4 h-4 text-blue-400" />
                    <h3 className="font-semibold text-dark-100">{t('blocklist.rules_in')}</h3>
                  </div>
                  <span className="text-sm text-dark-400">
                    {globalRulesIn.length} {t('blocklist.rules')}
                  </span>
                </div>
                <RulesList
                  rules={globalRulesIn}
                  onDelete={handleDeleteGlobalRule}
                  emptyMessage={t('blocklist.no_rules')}
                />
              </div>

              {/* Outgoing rules */}
              <div className="card">
                <div className="flex items-center justify-between mb-3">
                  <div className="flex items-center gap-2">
                    <ArrowUpFromLine className="w-4 h-4 text-orange-400" />
                    <h3 className="font-semibold text-dark-100">{t('blocklist.rules_out')}</h3>
                  </div>
                  <span className="text-sm text-dark-400">
                    {globalRulesOut.length} {t('blocklist.rules')}
                  </span>
                </div>
                <RulesList
                  rules={globalRulesOut}
                  onDelete={handleDeleteGlobalRule}
                  emptyMessage={t('blocklist.no_rules')}
                />
              </div>
            </div>
          </motion.div>
        )}

        {/* ========== SERVER RULES TAB ========== */}
        {activeTab === 'servers' && (
          <motion.div
            key="servers"
            initial={{ opacity: 0, x: -20 }}
            animate={{ opacity: 1, x: 0 }}
            exit={{ opacity: 0, x: 20 }}
          >
            {servers.length === 0 ? (
              <div className="card text-center py-8">
                <p className="text-dark-400">{t('dashboard.no_servers')}</p>
              </div>
            ) : (
              <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
                {servers.map((server) => {
                  const isExpanded = expandedServerIds.has(server.id)
                  const data = serverDataMap[server.id]
                  const form = getServerForm(server.id)

                  return (
                    <div
                      key={server.id}
                      className="card p-0 overflow-hidden self-start"
                    >
                      {/* Header (clickable) */}
                      <button
                        onClick={() => toggleServer(server.id)}
                        className="w-full flex items-center justify-between px-4 py-3.5 text-left hover:bg-dark-800/30 transition-colors"
                      >
                        <div className="flex items-center gap-2.5 min-w-0">
                          <Server className="w-4 h-4 text-accent-400 shrink-0" />
                          <span className="font-semibold text-dark-100 truncate">{server.name}</span>
                        </div>
                        <div className="flex items-center gap-2 shrink-0">
                          {data?.loaded && (
                            <div className="flex items-center gap-2 text-xs text-dark-400">
                              <span className="text-blue-400/70">{data.in.length} IN</span>
                              <span className="text-orange-400/70">{data.out.length} OUT</span>
                              <span className="text-dark-600">|</span>
                              <span>{data.globalCountIn + data.globalCountOut} G</span>
                            </div>
                          )}
                          <ChevronDown className={`w-4 h-4 text-dark-400 transition-transform duration-200 ${isExpanded ? 'rotate-180' : ''}`} />
                        </div>
                      </button>

                      {/* Body (collapsible) */}
                      <div className={`collapse-grid ${isExpanded ? 'open' : ''}`}>
                        <div className="collapse-content">
                          <div className="px-4 pb-4 pt-1 border-t border-dark-700/50 space-y-4">
                            {data?.loaded ? (
                              <>
                                {/* Add Rule Form */}
                                <div className="flex gap-2 items-center pt-3">
                                  <input
                                    type="text"
                                    value={form.ip}
                                    onChange={(e) => updateServerForm(server.id, { ip: e.target.value })}
                                    placeholder="192.168.1.0/24"
                                    className="input flex-1 font-mono text-sm"
                                    onKeyDown={(e) => e.key === 'Enter' && handleAddServerRule(server.id)}
                                  />
                                  <select
                                    value={form.direction}
                                    onChange={(e) => updateServerForm(server.id, { direction: e.target.value as BlocklistDirection })}
                                    className="input w-28 text-sm"
                                  >
                                    <option value="in">{t('blocklist.direction_incoming')}</option>
                                    <option value="out">{t('blocklist.direction_outgoing')}</option>
                                  </select>
                                  <motion.button
                                    onClick={() => handleAddServerRule(server.id)}
                                    disabled={addingServerId === server.id || !form.ip.trim()}
                                    className="btn btn-primary text-sm"
                                    whileHover={{ scale: 1.02 }}
                                    whileTap={{ scale: 0.98 }}
                                  >
                                    {addingServerId === server.id ? (
                                      <Loader2 className="w-4 h-4 animate-spin" />
                                    ) : (
                                      <Plus className="w-4 h-4" />
                                    )}
                                    {t('blocklist.add')}
                                  </motion.button>
                                </div>

                                {/* Two columns: IN and OUT */}
                                <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                                  <div>
                                    <div className="flex items-center gap-2 mb-2">
                                      <ArrowDownToLine className="w-3.5 h-3.5 text-blue-400" />
                                      <span className="text-sm font-medium text-dark-200">{t('blocklist.rules_in')}</span>
                                      <span className="text-xs text-dark-500">{data.in.length}</span>
                                    </div>
                                    <RulesList
                                      rules={data.in}
                                      onDelete={(ruleId) => handleDeleteServerRule(server.id, ruleId)}
                                      emptyMessage={t('blocklist.no_server_rules')}
                                    />
                                  </div>
                                  <div>
                                    <div className="flex items-center gap-2 mb-2">
                                      <ArrowUpFromLine className="w-3.5 h-3.5 text-orange-400" />
                                      <span className="text-sm font-medium text-dark-200">{t('blocklist.rules_out')}</span>
                                      <span className="text-xs text-dark-500">{data.out.length}</span>
                                    </div>
                                    <RulesList
                                      rules={data.out}
                                      onDelete={(ruleId) => handleDeleteServerRule(server.id, ruleId)}
                                      emptyMessage={t('blocklist.no_server_rules')}
                                    />
                                  </div>
                                </div>
                              </>
                            ) : (
                              <div className="flex items-center gap-2 py-4 justify-center text-dark-400">
                                <Loader2 className="w-4 h-4 animate-spin" />
                                <span className="text-sm">{t('blocklist.loading_rules')}</span>
                              </div>
                            )}
                          </div>
                        </div>
                      </div>
                    </div>
                  )
                })}
              </div>
            )}
          </motion.div>
        )}

        {/* ========== SOURCES TAB ========== */}
        {activeTab === 'sources' && (
          <motion.div
            key="sources"
            initial={{ opacity: 0, x: -20 }}
            animate={{ opacity: 1, x: 0 }}
            exit={{ opacity: 0, x: 20 }}
            className="space-y-4"
          >
            {/* Add Source Form */}
            <div className="card">
              <h3 className="text-lg font-semibold text-dark-100 mb-4">
                {t('blocklist.add_source')}
              </h3>

              <div className="space-y-3">
                <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                  <div>
                    <label className="block text-sm text-dark-400 mb-1">{t('common.name')}</label>
                    <input
                      type="text"
                      value={newSourceName}
                      onChange={(e) => setNewSourceName(e.target.value)}
                      placeholder="My Blocklist"
                      className="input w-full"
                    />
                  </div>

                  <div>
                    <label className="block text-sm text-dark-400 mb-1">URL</label>
                    <input
                      type="text"
                      value={newSourceUrl}
                      onChange={(e) => setNewSourceUrl(e.target.value)}
                      placeholder="https://example.com/blocklist.txt"
                      className="input w-full font-mono text-sm"
                    />
                  </div>
                </div>

                <div className="flex items-center gap-3">
                  <div className="flex gap-1.5">
                    {(['in', 'out'] as BlocklistDirection[]).map((dir) => (
                      <button
                        key={dir}
                        onClick={() => setNewSourceDirection(dir)}
                        className={`px-3 py-1.5 rounded-lg text-sm font-medium transition-all ${
                          newSourceDirection === dir
                            ? dir === 'in' ? 'bg-blue-500 text-white' : 'bg-orange-500 text-white'
                            : 'text-dark-400 hover:text-dark-200 bg-dark-800 border border-dark-700'
                        }`}
                      >
                        {dir === 'in' ? t('blocklist.direction_incoming') : t('blocklist.direction_outgoing')}
                      </button>
                    ))}
                  </div>

                  <motion.button
                    onClick={handleAddSource}
                    disabled={addingSource || !newSourceName.trim() || !newSourceUrl.trim()}
                    className="btn btn-primary"
                    whileHover={{ scale: 1.02 }}
                    whileTap={{ scale: 0.98 }}
                  >
                    {addingSource ? (
                      <Loader2 className="w-4 h-4 animate-spin" />
                    ) : (
                      <Plus className="w-4 h-4" />
                    )}
                    {t('blocklist.add_source')}
                  </motion.button>

                  <motion.button
                    onClick={handleRefreshAllSources}
                    disabled={refreshingSource !== null}
                    className="btn btn-secondary ml-auto"
                    whileHover={{ scale: 1.02 }}
                    whileTap={{ scale: 0.98 }}
                  >
                    {refreshingSource === -1 ? (
                      <Loader2 className="w-4 h-4 animate-spin" />
                    ) : (
                      <RefreshCw className="w-4 h-4" />
                    )}
                    {t('blocklist.refresh_all')}
                  </motion.button>
                </div>
              </div>
            </div>

            {/* Two columns: IN and OUT sources */}
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              {/* Incoming sources */}
              <div className="space-y-3">
                <div className="flex items-center gap-2">
                  <ArrowDownToLine className="w-4 h-4 text-blue-400" />
                  <h3 className="font-semibold text-dark-100">{t('blocklist.sources_in')}</h3>
                </div>
                {sourcesIn.length === 0 ? (
                  <div className="card py-6">
                    <p className="text-dark-400 text-center text-sm">{t('blocklist.no_sources')}</p>
                  </div>
                ) : (
                  sourcesIn.map((source) => (
                    <SourceCard
                      key={source.id}
                      source={source}
                      refreshingSource={refreshingSource}
                      onToggle={handleToggleSource}
                      onRefresh={handleRefreshSource}
                      onDelete={handleDeleteSource}
                      t={t}
                    />
                  ))
                )}
              </div>

              {/* Outgoing sources */}
              <div className="space-y-3">
                <div className="flex items-center gap-2">
                  <ArrowUpFromLine className="w-4 h-4 text-orange-400" />
                  <h3 className="font-semibold text-dark-100">{t('blocklist.sources_out')}</h3>
                </div>
                {sourcesOut.length === 0 ? (
                  <div className="card py-6">
                    <p className="text-dark-400 text-center text-sm">{t('blocklist.no_sources')}</p>
                  </div>
                ) : (
                  sourcesOut.map((source) => (
                    <SourceCard
                      key={source.id}
                      source={source}
                      refreshingSource={refreshingSource}
                      onToggle={handleToggleSource}
                      onRefresh={handleRefreshSource}
                      onDelete={handleDeleteSource}
                      t={t}
                    />
                  ))
                )}
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  )
}

function SourceCard({ source, refreshingSource, onToggle, onRefresh, onDelete, t }: {
  source: BlocklistSource
  refreshingSource: number | null
  onToggle: (id: number, enabled: boolean) => void
  onRefresh: (id: number) => void
  onDelete: (id: number) => void
  t: (key: string) => string
}) {
  return (
    <div className={`card ${!source.enabled ? 'opacity-60' : ''}`}>
      <div className="flex items-start justify-between">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-3 mb-2">
            <h3 className="font-semibold text-dark-100">{source.name}</h3>
            {source.is_default && (
              <span className="px-2 py-0.5 text-xs bg-accent-500/20 text-accent-400 rounded">
                {t('blocklist.default')}
              </span>
            )}
            {source.error_message && (
              <span className="flex items-center gap-1 text-xs text-danger">
                <AlertCircle className="w-3 h-3" />
                {t('common.error')}
              </span>
            )}
          </div>

          <a
            href={source.url}
            target="_blank"
            rel="noopener noreferrer"
            className="flex items-center gap-1 text-xs text-dark-400 hover:text-accent-400 transition-colors mb-3"
          >
            <ExternalLink className="w-3 h-3 shrink-0" />
            <span className="truncate">{source.url}</span>
          </a>

          <div className="flex items-center gap-4 text-sm text-dark-400">
            <span>{source.ip_count} IPs</span>
            {source.last_updated && (
              <span>
                {t('blocklist.last_updated')}: {new Date(source.last_updated).toLocaleDateString()}
              </span>
            )}
          </div>

          {source.error_message && (
            <p className="text-xs text-danger mt-2">{source.error_message}</p>
          )}
        </div>

        <div className="flex items-center gap-2 shrink-0">
          <Tooltip label={source.enabled ? t('blocklist.disable_source') : t('blocklist.enable_source')}>
            <button
              onClick={() => onToggle(source.id, !source.enabled)}
              className={`p-2 rounded-lg transition-colors ${
                source.enabled
                  ? 'bg-success/20 text-success'
                  : 'bg-dark-700 text-dark-400'
              }`}
            >
              {source.enabled ? <Check className="w-4 h-4" /> : <X className="w-4 h-4" />}
            </button>
          </Tooltip>

          <Tooltip label={t('common.refresh_data')}>
            <button
              onClick={() => onRefresh(source.id)}
              disabled={refreshingSource !== null}
              className="p-2 text-dark-400 hover:text-accent-400 transition-colors"
            >
              {refreshingSource === source.id ? (
                <Loader2 className="w-4 h-4 animate-spin" />
              ) : (
                <RefreshCw className="w-4 h-4" />
              )}
            </button>
          </Tooltip>

          {!source.is_default && (
            <Tooltip label={t('common.delete')}>
              <button
                onClick={() => onDelete(source.id)}
                className="p-2 text-dark-400 hover:text-danger transition-colors"
              >
                <Trash2 className="w-4 h-4" />
              </button>
            </Tooltip>
          )}
        </div>
      </div>
    </div>
  )
}
