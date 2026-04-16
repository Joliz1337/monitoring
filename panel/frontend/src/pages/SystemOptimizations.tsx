import { useState, useEffect, useLayoutEffect, useCallback, useRef } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { toast } from 'sonner'
import {
  Settings2,
  RefreshCw,
  CheckCircle2,
  XCircle,
  Loader2,
  Server as ServerIcon,
  AlertTriangle,
  Clock,
  Check,
  Download,
  Trash2,
  Cpu,
  ChevronDown,
  Shield,
  Monitor,
} from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { systemApi, VersionBaseInfo, SingleNodeVersion, NicInfo } from '../api/client'
import { Skeleton } from '../components/ui/Skeleton'
import { FAQIcon } from '../components/FAQ'
import { Tooltip } from '../components/ui/Tooltip'

type LoadState = 'pending' | 'loading' | 'loaded' | 'error'

interface NodeState {
  id: number
  name: string
  url: string
  loadState: LoadState
  status: 'online' | 'offline'
  version: string | null
  installed: boolean
  nicMode: string
  optProfile: string | null
  nicInfo: NicInfo | null
  nicInfoLoading: boolean
  nodeOutdated: boolean
}

export default function SystemOptimizations() {
  const { t } = useTranslation()

  const [baseInfo, setBaseInfo] = useState<VersionBaseInfo | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [nodes, setNodes] = useState<Map<number, NodeState>>(new Map())
  const [isChecking, setIsChecking] = useState(false)

  const [applyingNodes, setApplyingNodes] = useState<Set<number>>(new Set())
  const [removingNodes, setRemovingNodes] = useState<Set<number>>(new Set())
  const [results, setResults] = useState<Record<string, { success: boolean; message: string }>>({})

  const [modeDropdown, setModeDropdown] = useState<number | null>(null)
  const [confirmRemove, setConfirmRemove] = useState<number | null>(null)
  const [profileChoice, setProfileChoice] = useState<string | null>(null)

  const abortRef = useRef(false)
  const applyTriggerRefs = useRef<Map<number, HTMLButtonElement>>(new Map())
  const removeTriggerRefs = useRef<Map<number, HTMLButtonElement>>(new Map())
  const [dropdownUp, setDropdownUp] = useState(true)

  const DROPDOWN_MAX_HEIGHT = 280

  useLayoutEffect(() => {
    const activeId = modeDropdown ?? confirmRemove
    if (activeId == null) return
    const refsMap = modeDropdown != null ? applyTriggerRefs.current : removeTriggerRefs.current
    const el = refsMap.get(activeId)
    if (!el) { setDropdownUp(true); return }
    const rect = el.getBoundingClientRect()
    setDropdownUp(rect.top > DROPDOWN_MAX_HEIGHT)
  }, [modeDropdown, confirmRemove, profileChoice])

  const fetchNodeData = useCallback(async (nodeId: number) => {
    setNodes(prev => {
      const next = new Map(prev)
      const existing = next.get(nodeId)
      if (existing) next.set(nodeId, { ...existing, loadState: 'loading' })
      return next
    })

    try {
      const resp = await systemApi.getNodeVersionById(nodeId)
      const data: SingleNodeVersion = resp.data
      const opt = data.optimizations ?? { installed: false, version: null }
      const hasNicMode = opt.nic_mode !== undefined

      setNodes(prev => {
        const next = new Map(prev)
        const existing = next.get(nodeId)
        next.set(nodeId, {
          id: data.id, name: data.name, url: data.url,
          loadState: 'loaded', status: data.status,
          version: opt.version, installed: opt.installed,
          nicMode: opt.nic_mode || 'none',
          optProfile: opt.profile || (opt.installed ? 'vpn' : null),
          nicInfo: existing?.nicInfo ?? null,
          nicInfoLoading: existing?.nicInfoLoading ?? false,
          nodeOutdated: !hasNicMode,
        })
        return next
      })

      if (data.status === 'online' && hasNicMode) {
        fetchNicInfo(nodeId)
      }
    } catch {
      setNodes(prev => {
        const next = new Map(prev)
        const existing = next.get(nodeId)
        if (existing) next.set(nodeId, { ...existing, loadState: 'error', status: 'offline' })
        return next
      })
    }
  }, [])

  const fetchNicInfo = useCallback(async (nodeId: number) => {
    setNodes(prev => {
      const next = new Map(prev)
      const existing = next.get(nodeId)
      if (existing) next.set(nodeId, { ...existing, nicInfoLoading: true })
      return next
    })

    try {
      const resp = await systemApi.getNodeNicInfo(nodeId)
      setNodes(prev => {
        const next = new Map(prev)
        const existing = next.get(nodeId)
        if (existing) next.set(nodeId, { ...existing, nicInfo: resp.data, nicInfoLoading: false })
        return next
      })
    } catch (err: any) {
      const is404 = err.response?.status === 404
      setNodes(prev => {
        const next = new Map(prev)
        const existing = next.get(nodeId)
        if (existing) {
          next.set(nodeId, { ...existing, nicInfoLoading: false, nodeOutdated: is404 ? true : existing.nodeOutdated })
        }
        return next
      })
    }
  }, [])

  const fetchBase = useCallback(async (showCheckingState = false) => {
    try {
      setError('')
      if (showCheckingState) setIsChecking(true)
      abortRef.current = false

      const resp = await systemApi.getVersionBase()
      setBaseInfo(resp.data)

      const initialNodes = new Map<number, NodeState>()
      for (const n of resp.data.nodes) {
        initialNodes.set(n.id, {
          id: n.id, name: n.name, url: n.url,
          loadState: 'pending', status: 'offline',
          version: null, installed: false, nicMode: 'none', optProfile: null,
          nicInfo: null, nicInfoLoading: false, nodeOutdated: false,
        })
      }
      setNodes(initialNodes)

      for (const n of resp.data.nodes) {
        if (abortRef.current) break
        fetchNodeData(n.id)
      }
    } catch {
      setError(t('sys_opt.failed_fetch'))
    } finally {
      setLoading(false)
      setIsChecking(false)
    }
  }, [t, fetchNodeData])

  useEffect(() => {
    fetchBase()
    return () => { abortRef.current = true }
  }, [fetchBase])

  const handleRefresh = useCallback(() => {
    abortRef.current = true
    setResults({})
    setModeDropdown(null)
    setConfirmRemove(null)
    setProfileChoice(null)
    fetchBase(true)
  }, [fetchBase])

  const handleApply = async (nodeId: number, nodeName: string, nicMode: string, optProfile: string) => {
    if (applyingNodes.has(nodeId)) return
    setModeDropdown(null)

    setApplyingNodes(prev => new Set(prev).add(nodeId))
    setResults(prev => ({ ...prev, [`node-${nodeId}`]: { success: true, message: t('sys_opt.applying') } }))

    try {
      const response = await systemApi.applyNodeOptimizations(nodeId, nicMode, optProfile)
      setResults(prev => ({
        ...prev,
        [`node-${nodeId}`]: { success: true, message: response.data.message }
      }))

      setTimeout(() => {
        fetchNodeData(nodeId)
        setApplyingNodes(prev => { const next = new Set(prev); next.delete(nodeId); return next })
      }, 3000)
    } catch (err: any) {
      const detail = err.response?.data?.detail
      const msg = typeof detail === 'object' ? detail.message : (detail || t('sys_opt.failed_apply'))
      setResults(prev => ({ ...prev, [`node-${nodeId}`]: { success: false, message: msg } }))
      toast.error(`${nodeName}: ${msg}`)
      setApplyingNodes(prev => { const next = new Set(prev); next.delete(nodeId); return next })
    }
  }

  const handleRemove = async (nodeId: number, nodeName: string) => {
    if (removingNodes.has(nodeId)) return
    setConfirmRemove(null)

    setRemovingNodes(prev => new Set(prev).add(nodeId))
    setResults(prev => ({ ...prev, [`node-${nodeId}`]: { success: true, message: t('sys_opt.removing') } }))

    try {
      const response = await systemApi.removeNodeOptimizations(nodeId)
      setResults(prev => ({
        ...prev,
        [`node-${nodeId}`]: { success: response.data.success, message: response.data.message }
      }))
      if (response.data.success) toast.success(`${nodeName}: ${t('sys_opt.removed_success')}`)

      setTimeout(() => {
        fetchNodeData(nodeId)
        setRemovingNodes(prev => { const next = new Set(prev); next.delete(nodeId); return next })
      }, 2000)
    } catch (err: any) {
      const msg = err.response?.data?.detail || t('sys_opt.failed_remove')
      setResults(prev => ({ ...prev, [`node-${nodeId}`]: { success: false, message: msg } }))
      toast.error(`${nodeName}: ${msg}`)
      setRemovingNodes(prev => { const next = new Set(prev); next.delete(nodeId); return next })
    }
  }

  const needsUpdate = (node: NodeState): boolean => {
    if (!baseInfo?.optimizations?.latest_version) return false
    if (!node.installed) return true
    return !node.version || node.version !== baseInfo.optimizations.latest_version
  }

  const getNicModeLabel = (mode: string): string => {
    switch (mode) {
      case 'hybrid': return t('sys_opt.nic_hybrid')
      case 'multiqueue': return t('sys_opt.nic_multiqueue')
      case 'rps': return t('sys_opt.nic_rps')
      default: return t('sys_opt.nic_none')
    }
  }

  const getNicModeBadgeClass = (mode: string): string => {
    switch (mode) {
      case 'hybrid': return 'bg-teal-500/20 text-teal-400 border-teal-500/30'
      case 'multiqueue': return 'bg-purple/20 text-purple-400 border-purple/30'
      case 'rps': return 'bg-accent-500/20 text-accent-400 border-accent-500/30'
      default: return 'bg-dark-700/50 text-dark-400 border-dark-600/30'
    }
  }

  // Группировка нод
  const nodesList = Array.from(nodes.values())
  const unassigned = nodesList.filter(n => n.loadState === 'loaded' && n.status === 'online' && !n.installed)
  const vpnNodes = nodesList.filter(n => n.installed && (n.optProfile === 'vpn' || !n.optProfile))
  const panelNodes = nodesList.filter(n => n.installed && n.optProfile === 'panel')
  const loadingOrOffline = nodesList.filter(n => (n.loadState !== 'loaded' || n.status === 'offline') && !n.installed)

  // Рендер карточки ноды
  const renderNodeCard = (node: NodeState, _defaultProfile?: string) => {
    const isApplying = applyingNodes.has(node.id)
    const isRemoving = removingNodes.has(node.id)
    const isBusy = isApplying || isRemoving
    const result = results[`node-${node.id}`]
    const isNodeLoading = node.loadState === 'pending' || node.loadState === 'loading'
    const updateAvailable = node.loadState === 'loaded' && node.status === 'online' && needsUpdate(node)
    const showModeDropdown = modeDropdown === node.id
    const showConfirmRemove = confirmRemove === node.id
    const mqSupported = node.nicInfo?.multiqueue_supported ?? false
    const hybridRecommended = node.nicInfo?.hybrid_recommended ?? false
    const currentProfile = node.optProfile || 'vpn'
    const hasOpenDropdown = showModeDropdown || showConfirmRemove

    return (
      <motion.div
        key={node.id}
        className={`card group hover:border-dark-700 transition-all overflow-visible ${hasOpenDropdown ? 'relative z-50' : ''}`}
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        layout
      >
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-3 min-w-0">
            <div className={`w-10 h-10 rounded-xl flex items-center justify-center border shrink-0
              ${isNodeLoading ? 'bg-dark-800/50 border-dark-700/30 animate-pulse'
                : node.status === 'online' ? 'bg-gradient-to-br from-dark-700 to-dark-800 border-dark-700/50'
                : 'bg-dark-800/50 border-dark-700/30'} transition-colors`}
            >
              {isNodeLoading ? <Loader2 className="w-4 h-4 text-dark-500 animate-spin" />
                : <ServerIcon className={`w-4 h-4 ${node.status === 'online' ? 'text-accent-500' : 'text-dark-500'}`} />}
            </div>
            <div className="min-w-0">
              <h3 className="font-semibold text-dark-100 flex items-center gap-2 text-sm">
                {node.name}
                {!isNodeLoading && <span className={`w-1.5 h-1.5 rounded-full ${node.status === 'online' ? 'bg-success' : 'bg-dark-500'}`} />}
              </h3>
              <div className="flex items-center gap-2 mt-0.5 flex-wrap">
                {isNodeLoading ? <Skeleton className="h-3 w-20" /> : (
                  <>
                    <span className="text-xs text-dark-500">
                      {node.installed
                        ? (node.version ? `v${node.version}` : t('sys_opt.legacy'))
                        : t('sys_opt.not_installed')}
                    </span>
                    {node.installed && !node.nodeOutdated && (
                      <span className={`inline-flex items-center gap-0.5 px-1.5 py-0.5 text-[10px] font-medium rounded-full border ${getNicModeBadgeClass(node.nicMode)}`}>
                        <Cpu className="w-2.5 h-2.5" />
                        {getNicModeLabel(node.nicMode)}
                      </span>
                    )}
                    {node.installed && !node.nodeOutdated && node.nicInfo && (() => {
                      const { multiqueue_supported, hybrid_recommended } = node.nicInfo
                      if (hybrid_recommended && node.nicMode !== 'hybrid') {
                        return (
                          <Tooltip label={t('sys_opt.mq_available_hint')}>
                            <span className="inline-flex items-center gap-0.5 px-1.5 py-0.5 text-[10px] font-medium rounded-full border bg-success/10 text-success border-success/30 cursor-help">
                              {t('sys_opt.recommended')}: {getNicModeLabel('hybrid')}
                            </span>
                          </Tooltip>
                        )
                      }
                      if (multiqueue_supported && (!node.nicMode || node.nicMode === 'rps' || node.nicMode === 'none')) {
                        return (
                          <Tooltip label={t('sys_opt.mq_available_hint')}>
                            <span className="inline-flex items-center gap-0.5 px-1.5 py-0.5 text-[10px] font-medium rounded-full border bg-purple-500/10 text-purple-400 border-purple-500/30 cursor-help">
                              {t('sys_opt.mq_available')}
                            </span>
                          </Tooltip>
                        )
                      }
                      return null
                    })()}
                    {node.nodeOutdated && (
                      <span className="inline-flex items-center gap-0.5 px-1.5 py-0.5 text-[10px] font-medium rounded-full border bg-warning/20 text-warning border-warning/30">
                        <AlertTriangle className="w-2.5 h-2.5" />
                        {t('sys_opt.node_outdated')}
                      </span>
                    )}
                    {node.nicInfoLoading && <Loader2 className="w-2.5 h-2.5 text-dark-500 animate-spin" />}
                  </>
                )}
              </div>
            </div>
          </div>

          <div className="flex items-center gap-2 shrink-0 relative">
            <AnimatePresence>
              {result && (
                <motion.div
                  className={`flex items-center gap-1 text-xs px-2 py-1 rounded-lg ${result.success ? 'text-success bg-success/10' : 'text-danger bg-danger/10'}`}
                  initial={{ opacity: 0, scale: 0.8 }}
                  animate={{ opacity: 1, scale: 1 }}
                  exit={{ opacity: 0, scale: 0.8 }}
                >
                  {isBusy ? <Loader2 className="w-3 h-3 animate-spin" />
                    : result.success ? <CheckCircle2 className="w-3 h-3" /> : <XCircle className="w-3 h-3" />}
                  <span className="max-w-[120px] truncate">{result.message}</span>
                </motion.div>
              )}
            </AnimatePresence>

            {isNodeLoading && <Loader2 className="w-3.5 h-3.5 text-dark-500 animate-spin" />}

            {!isNodeLoading && node.status === 'offline' && (
              <span className="flex items-center gap-1 text-xs text-dark-500"><Clock className="w-3 h-3" />{t('sys_opt.offline')}</span>
            )}

            {!isNodeLoading && node.status === 'online' && updateAvailable && !isBusy && !result && (
              <span className="px-2 py-0.5 text-[10px] font-medium bg-accent-500/20 text-accent-400 rounded-full">
                {t('sys_opt.update_available')}
              </span>
            )}

            {!isNodeLoading && node.status === 'online' && !updateAvailable && node.installed && !isBusy && !result && (
              <span className="flex items-center gap-1 text-xs text-success"><Check className="w-3 h-3" />{t('sys_opt.up_to_date')}</span>
            )}

            {/* Apply button */}
            {!isNodeLoading && node.status === 'online' && !node.nodeOutdated && (
              <div className="relative">
                <motion.button
                  ref={(el) => { if (el) applyTriggerRefs.current.set(node.id, el) }}
                  onClick={() => { setProfileChoice(null); setModeDropdown(showModeDropdown ? null : node.id) }}
                  disabled={isBusy}
                  className="btn btn-secondary text-xs px-2.5 py-1.5"
                  whileHover={{ scale: 1.05 }}
                  whileTap={{ scale: 0.95 }}
                >
                  {isApplying ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Download className="w-3.5 h-3.5" />}
                  {t('sys_opt.apply')}
                  <ChevronDown className="w-2.5 h-2.5" />
                </motion.button>

                <AnimatePresence>
                  {showModeDropdown && !isBusy && (
                    <motion.div
                      className={`absolute right-0 ${dropdownUp ? 'bottom-full mb-1' : 'top-full mt-1'} z-50 bg-dark-800 border border-dark-700 rounded-xl shadow-xl overflow-hidden min-w-[240px]`}
                      initial={{ opacity: 0, y: dropdownUp ? 5 : -5, scale: 0.95 }}
                      animate={{ opacity: 1, y: 0, scale: 1 }}
                      exit={{ opacity: 0, y: dropdownUp ? 5 : -5, scale: 0.95 }}
                      transition={{ duration: 0.15 }}
                    >
                      {!profileChoice ? (
                        <>
                          <button
                            onClick={() => setProfileChoice('vpn')}
                            className={`w-full px-3 py-2.5 text-left text-xs hover:bg-dark-700 transition-colors flex items-center gap-2 ${currentProfile === 'vpn' ? 'bg-dark-700/50' : ''}`}
                          >
                            <Shield className="w-4 h-4 text-accent-400" />
                            <div className="flex-1">
                              <div className="text-dark-200 font-medium flex items-center gap-1.5">
                                {t('sys_opt.profile_vpn')}
                                {currentProfile === 'vpn' && <span className="text-[9px] text-success">{t('sys_opt.current')}</span>}
                              </div>
                              <div className="text-[10px] text-dark-500">{t('sys_opt.profile_vpn_desc')}</div>
                            </div>
                          </button>
                          <button
                            onClick={() => setProfileChoice('panel')}
                            className={`w-full px-3 py-2.5 text-left text-xs hover:bg-dark-700 transition-colors flex items-center gap-2 border-t border-dark-700 ${currentProfile === 'panel' ? 'bg-dark-700/50' : ''}`}
                          >
                            <Monitor className="w-4 h-4 text-emerald-400" />
                            <div className="flex-1">
                              <div className="text-dark-200 font-medium flex items-center gap-1.5">
                                {t('sys_opt.profile_panel')}
                                {currentProfile === 'panel' && <span className="text-[9px] text-success">{t('sys_opt.current')}</span>}
                              </div>
                              <div className="text-[10px] text-dark-500">{t('sys_opt.profile_panel_desc')}</div>
                            </div>
                          </button>
                        </>
                      ) : (
                        <>
                          <div className="px-3 py-1.5 text-[10px] text-dark-500 border-b border-dark-700 flex items-center gap-1">
                            <button onClick={() => setProfileChoice(null)} className="text-accent-400 hover:underline">&larr;</button>
                            {profileChoice === 'vpn' ? t('sys_opt.profile_vpn') : t('sys_opt.profile_panel')}
                          </div>
                          {mqSupported && (
                            <button
                              onClick={() => handleApply(node.id, node.name, 'hybrid', profileChoice)}
                              className={`w-full px-3 py-2 text-left text-xs hover:bg-dark-700 transition-colors flex items-center gap-2 ${node.nicMode === 'hybrid' ? 'bg-dark-700/50' : ''}`}
                            >
                              <Cpu className="w-3.5 h-3.5 text-teal-400" />
                              <div className="flex-1">
                                <div className="text-dark-200 flex items-center gap-1.5">
                                  {t('sys_opt.nic_hybrid')}
                                  {node.nicMode === 'hybrid' && <span className="text-[9px] text-success">{t('sys_opt.current')}</span>}
                                  {hybridRecommended && node.nicMode !== 'hybrid' && <span className="text-[9px] text-success">{t('sys_opt.recommended')}</span>}
                                </div>
                                <div className="text-[10px] text-dark-500">{t('sys_opt.hybrid_desc')}</div>
                              </div>
                            </button>
                          )}
                          {mqSupported && (
                            <button
                              onClick={() => handleApply(node.id, node.name, 'multiqueue', profileChoice)}
                              className={`w-full px-3 py-2 text-left text-xs hover:bg-dark-700 transition-colors flex items-center gap-2 border-t border-dark-700 ${node.nicMode === 'multiqueue' ? 'bg-dark-700/50' : ''}`}
                            >
                              <Cpu className="w-3.5 h-3.5 text-purple-400" />
                              <div className="flex-1">
                                <div className="text-dark-200 flex items-center gap-1.5">
                                  {t('sys_opt.nic_multiqueue')}
                                  {node.nicMode === 'multiqueue' && <span className="text-[9px] text-success">{t('sys_opt.current')}</span>}
                                </div>
                                <div className="text-[10px] text-dark-500">{t('sys_opt.mq_desc')}</div>
                              </div>
                            </button>
                          )}
                          <button
                            onClick={() => handleApply(node.id, node.name, 'rps', profileChoice)}
                            className={`w-full px-3 py-2 text-left text-xs hover:bg-dark-700 transition-colors flex items-center gap-2 ${mqSupported ? 'border-t border-dark-700' : ''} ${node.nicMode === 'rps' ? 'bg-dark-700/50' : ''}`}
                          >
                            <Cpu className="w-3.5 h-3.5 text-accent-400" />
                            <div className="flex-1">
                              <div className="text-dark-200 flex items-center gap-1.5">
                                {t('sys_opt.nic_rps')}
                                {node.nicMode === 'rps' && <span className="text-[9px] text-success">{t('sys_opt.current')}</span>}
                              </div>
                              <div className="text-[10px] text-dark-500">{t('sys_opt.rps_desc')}</div>
                            </div>
                          </button>
                        </>
                      )}
                    </motion.div>
                  )}
                </AnimatePresence>
              </div>
            )}

            {/* Remove button */}
            {!isNodeLoading && node.status === 'online' && node.installed && (
              <div className="relative">
                <motion.button
                  ref={(el) => { if (el) removeTriggerRefs.current.set(node.id, el) }}
                  onClick={() => setConfirmRemove(showConfirmRemove ? null : node.id)}
                  disabled={isBusy}
                  className="btn btn-secondary text-xs px-1.5 py-1.5 !text-danger hover:!bg-danger/10"
                  whileHover={{ scale: 1.05 }}
                  whileTap={{ scale: 0.95 }}
                >
                  {isRemoving ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Trash2 className="w-3.5 h-3.5" />}
                </motion.button>

                <AnimatePresence>
                  {showConfirmRemove && !isBusy && (
                    <motion.div
                      className={`absolute right-0 ${dropdownUp ? 'bottom-full mb-1' : 'top-full mt-1'} z-50 bg-dark-800 border border-danger/30 rounded-xl shadow-xl p-3 min-w-[240px]`}
                      initial={{ opacity: 0, y: dropdownUp ? 5 : -5, scale: 0.95 }}
                      animate={{ opacity: 1, y: 0, scale: 1 }}
                      exit={{ opacity: 0, y: dropdownUp ? 5 : -5, scale: 0.95 }}
                      transition={{ duration: 0.15 }}
                    >
                      <p className="text-xs text-dark-300 mb-2">{t('sys_opt.remove_confirm')}</p>
                      <div className="flex gap-2">
                        <button onClick={() => setConfirmRemove(null)} className="btn btn-secondary text-xs flex-1">{t('common.cancel')}</button>
                        <button onClick={() => handleRemove(node.id, node.name)} className="btn text-xs flex-1 bg-danger/20 text-danger hover:bg-danger/30 border border-danger/30">{t('sys_opt.remove')}</button>
                      </div>
                    </motion.div>
                  )}
                </AnimatePresence>
              </div>
            )}
          </div>
        </div>
      </motion.div>
    )
  }

  // Рендер карточки неназначенной ноды — с выбором профиля
  const renderUnassignedCard = (node: NodeState) => {
    const isApplying = applyingNodes.has(node.id)
    const result = results[`node-${node.id}`]
    const showModeDropdown = modeDropdown === node.id
    const mqSupported = node.nicInfo?.multiqueue_supported ?? false
    const hybridRecommended = node.nicInfo?.hybrid_recommended ?? false

    return (
      <motion.div
        key={node.id}
        className={`card group hover:border-dark-700 transition-all overflow-visible ${showModeDropdown ? 'relative z-50' : ''}`}
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        layout
      >
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-3 min-w-0">
            <div className="w-10 h-10 rounded-xl flex items-center justify-center border bg-gradient-to-br from-dark-700 to-dark-800 border-dark-700/50 shrink-0">
              <ServerIcon className="w-4 h-4 text-accent-500" />
            </div>
            <div>
              <h3 className="font-semibold text-dark-100 flex items-center gap-2 text-sm">
                {node.name}
                <span className="w-1.5 h-1.5 rounded-full bg-success" />
              </h3>
              <div className="flex items-center gap-2 mt-0.5 flex-wrap">
                <span className="text-xs text-dark-500">{t('sys_opt.not_installed')}</span>
                {node.nicInfo && (() => {
                  const { multiqueue_supported, hybrid_recommended } = node.nicInfo
                  if (hybrid_recommended) {
                    return (
                      <Tooltip label={t('sys_opt.mq_available_hint')}>
                        <span className="inline-flex items-center gap-0.5 px-1.5 py-0.5 text-[10px] font-medium rounded-full border bg-success/10 text-success border-success/30 cursor-help">
                          {t('sys_opt.recommended')}: {getNicModeLabel('hybrid')}
                        </span>
                      </Tooltip>
                    )
                  }
                  if (multiqueue_supported) {
                    return (
                      <Tooltip label={t('sys_opt.mq_available_hint')}>
                        <span className="inline-flex items-center gap-0.5 px-1.5 py-0.5 text-[10px] font-medium rounded-full border bg-purple-500/10 text-purple-400 border-purple-500/30 cursor-help">
                          {t('sys_opt.mq_available')}
                        </span>
                      </Tooltip>
                    )
                  }
                  return null
                })()}
              </div>
            </div>
          </div>

          <div className="flex items-center gap-2 shrink-0 relative">
            <AnimatePresence>
              {result && (
                <motion.div
                  className={`flex items-center gap-1 text-xs px-2 py-1 rounded-lg ${result.success ? 'text-success bg-success/10' : 'text-danger bg-danger/10'}`}
                  initial={{ opacity: 0, scale: 0.8 }}
                  animate={{ opacity: 1, scale: 1 }}
                  exit={{ opacity: 0, scale: 0.8 }}
                >
                  {isApplying ? <Loader2 className="w-3 h-3 animate-spin" />
                    : result.success ? <CheckCircle2 className="w-3 h-3" /> : <XCircle className="w-3 h-3" />}
                  <span className="max-w-[120px] truncate">{result.message}</span>
                </motion.div>
              )}
            </AnimatePresence>

            {!node.nodeOutdated && (
              <div className="relative">
                <motion.button
                  ref={(el) => { if (el) applyTriggerRefs.current.set(node.id, el) }}
                  onClick={() => {
                    setProfileChoice(null)
                    setModeDropdown(showModeDropdown ? null : node.id)
                  }}
                  disabled={isApplying}
                  className="btn btn-primary text-xs px-2.5 py-1.5"
                  whileHover={{ scale: 1.05 }}
                  whileTap={{ scale: 0.95 }}
                >
                  {isApplying ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Download className="w-3.5 h-3.5" />}
                  {t('sys_opt.apply')}
                  <ChevronDown className="w-2.5 h-2.5" />
                </motion.button>

                <AnimatePresence>
                  {showModeDropdown && !isApplying && (
                    <motion.div
                      className={`absolute right-0 ${dropdownUp ? 'bottom-full mb-1' : 'top-full mt-1'} z-50 bg-dark-800 border border-dark-700 rounded-xl shadow-xl overflow-hidden min-w-[240px]`}
                      initial={{ opacity: 0, y: dropdownUp ? 5 : -5, scale: 0.95 }}
                      animate={{ opacity: 1, y: 0, scale: 1 }}
                      exit={{ opacity: 0, y: dropdownUp ? 5 : -5, scale: 0.95 }}
                      transition={{ duration: 0.15 }}
                    >
                      {!profileChoice ? (
                        <>
                          <button
                            onClick={() => setProfileChoice('vpn')}
                            className="w-full px-3 py-2.5 text-left text-xs hover:bg-dark-700 transition-colors flex items-center gap-2"
                          >
                            <Shield className="w-4 h-4 text-accent-400" />
                            <div>
                              <div className="text-dark-200 font-medium">{t('sys_opt.profile_vpn')}</div>
                              <div className="text-[10px] text-dark-500">{t('sys_opt.profile_vpn_desc')}</div>
                            </div>
                          </button>
                          <button
                            onClick={() => setProfileChoice('panel')}
                            className="w-full px-3 py-2.5 text-left text-xs hover:bg-dark-700 transition-colors flex items-center gap-2 border-t border-dark-700"
                          >
                            <Monitor className="w-4 h-4 text-emerald-400" />
                            <div>
                              <div className="text-dark-200 font-medium">{t('sys_opt.profile_panel')}</div>
                              <div className="text-[10px] text-dark-500">{t('sys_opt.profile_panel_desc')}</div>
                            </div>
                          </button>
                        </>
                      ) : (
                        <>
                          <div className="px-3 py-1.5 text-[10px] text-dark-500 border-b border-dark-700 flex items-center gap-1">
                            <button onClick={() => setProfileChoice(null)} className="text-accent-400 hover:underline">&larr;</button>
                            {profileChoice === 'vpn' ? t('sys_opt.profile_vpn') : t('sys_opt.profile_panel')}
                          </div>
                          {mqSupported && (
                            <button
                              onClick={() => handleApply(node.id, node.name, 'hybrid', profileChoice)}
                              className="w-full px-3 py-2 text-left text-xs hover:bg-dark-700 transition-colors flex items-center gap-2"
                            >
                              <Cpu className="w-3.5 h-3.5 text-teal-400" />
                              <div>
                                <div className="text-dark-200 flex items-center gap-1.5">
                                  {t('sys_opt.nic_hybrid')}
                                  {hybridRecommended && <span className="text-[9px] text-success">{t('sys_opt.recommended')}</span>}
                                </div>
                                <div className="text-[10px] text-dark-500">{t('sys_opt.hybrid_desc')}</div>
                              </div>
                            </button>
                          )}
                          {mqSupported && (
                            <button
                              onClick={() => handleApply(node.id, node.name, 'multiqueue', profileChoice)}
                              className="w-full px-3 py-2 text-left text-xs hover:bg-dark-700 transition-colors flex items-center gap-2 border-t border-dark-700"
                            >
                              <Cpu className="w-3.5 h-3.5 text-purple-400" />
                              <div>
                                <div className="text-dark-200">{t('sys_opt.nic_multiqueue')}</div>
                                <div className="text-[10px] text-dark-500">{t('sys_opt.mq_desc')}</div>
                              </div>
                            </button>
                          )}
                          <button
                            onClick={() => handleApply(node.id, node.name, 'rps', profileChoice)}
                            className={`w-full px-3 py-2 text-left text-xs hover:bg-dark-700 transition-colors flex items-center gap-2 ${mqSupported ? 'border-t border-dark-700' : ''}`}
                          >
                            <Cpu className="w-3.5 h-3.5 text-accent-400" />
                            <div>
                              <div className="text-dark-200">{t('sys_opt.nic_rps')}</div>
                              <div className="text-[10px] text-dark-500">{t('sys_opt.rps_desc')}</div>
                            </div>
                          </button>
                        </>
                      )}
                    </motion.div>
                  )}
                </AnimatePresence>
              </div>
            )}

            {node.nodeOutdated && (
              <span className="inline-flex items-center gap-1 px-2 py-1 text-[10px] font-medium rounded-full border bg-warning/20 text-warning border-warning/30">
                <AlertTriangle className="w-3 h-3" />
                {t('sys_opt.node_outdated')}
              </span>
            )}
          </div>
        </div>
      </motion.div>
    )
  }

  if (loading) {
    return (
      <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }}>
        <div className="flex items-center gap-3 mb-6">
          <Skeleton className="w-10 h-10 rounded-xl" />
          <div><Skeleton className="h-6 w-56 mb-2" /><Skeleton className="h-4 w-72" /></div>
        </div>
        <div className="space-y-3">
          {Array.from({ length: 3 }).map((_, i) => (
            <div key={i} className="card"><Skeleton className="h-16 w-full" /></div>
          ))}
        </div>
      </motion.div>
    )
  }

  return (
    <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }}>
      {/* Header */}
      <motion.div
        className="flex items-center justify-between mb-6"
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
      >
        <div>
          <h1 className="text-2xl font-bold text-dark-50 flex items-center gap-3">
            <Settings2 className="w-7 h-7 text-accent-400" />
            {t('sys_opt.title')}
            <FAQIcon screen="PAGE_SYSTEM_OPTIMIZATIONS" />
          </h1>
          <p className="text-dark-400 mt-1">{t('sys_opt.subtitle')}</p>
        </div>

        <div className="flex items-center gap-3">
          {baseInfo?.optimizations?.latest_version && (
            <span className="text-sm text-dark-500">
              {t('sys_opt.latest_version')}: <span className="font-mono text-dark-300">v{baseInfo.optimizations.latest_version}</span>
            </span>
          )}
          <motion.button
            onClick={handleRefresh}
            className="btn btn-secondary"
            whileHover={{ scale: 1.02 }}
            whileTap={{ scale: 0.98 }}
            disabled={isChecking}
          >
            <RefreshCw className={`w-4 h-4 ${isChecking ? 'animate-spin' : ''}`} />
            {isChecking ? t('sys_opt.checking') : t('common.refresh')}
          </motion.button>
        </div>
      </motion.div>

      {/* Error */}
      <AnimatePresence>
        {error && (
          <motion.div className="flex items-center gap-3 p-4 mb-6 bg-danger/10 border border-danger/20 rounded-xl text-danger"
            initial={{ opacity: 0, y: -10 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -10 }}>
            <AlertTriangle className="w-5 h-5 flex-shrink-0" />
            <span className="text-sm">{error}</span>
          </motion.div>
        )}
      </AnimatePresence>

      {nodesList.length === 0 ? (
        <div className="card text-center py-12">
          <ServerIcon className="w-12 h-12 text-dark-600 mx-auto mb-3" />
          <p className="text-dark-400">{t('sys_opt.no_nodes')}</p>
        </div>
      ) : (
        <>
          {/* Unassigned nodes — top */}
          {(unassigned.length > 0 || loadingOrOffline.length > 0) && (
            <div className="mb-6">
              <h2 className="text-sm font-semibold text-dark-400 mb-3 flex items-center gap-2">
                <ServerIcon className="w-4 h-4" />
                {t('sys_opt.unassigned')}
                <span className="text-dark-500 font-normal">({unassigned.length + loadingOrOffline.length})</span>
              </h2>
              <div className="space-y-2">
                {unassigned.map(n => renderUnassignedCard(n))}
                {loadingOrOffline.map(n => renderNodeCard(n))}
              </div>
            </div>
          )}

          {/* Two columns: VPN | Panel */}
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
            {/* VPN column */}
            <div>
              <h2 className="text-sm font-semibold text-dark-300 mb-3 flex items-center gap-2">
                <Shield className="w-4 h-4 text-accent-400" />
                {t('sys_opt.vpn_nodes')}
                <span className="text-dark-500 font-normal">({vpnNodes.length})</span>
              </h2>
              {vpnNodes.length === 0 ? (
                <div className="card text-center py-8 border-dashed">
                  <p className="text-xs text-dark-500">{t('sys_opt.no_nodes')}</p>
                </div>
              ) : (
                <div className="space-y-2">
                  {vpnNodes.map(n => renderNodeCard(n, 'vpn'))}
                </div>
              )}
            </div>

            {/* Panel column */}
            <div>
              <h2 className="text-sm font-semibold text-dark-300 mb-3 flex items-center gap-2">
                <Monitor className="w-4 h-4 text-emerald-400" />
                {t('sys_opt.panel_nodes')}
                <span className="text-dark-500 font-normal">({panelNodes.length})</span>
              </h2>
              {panelNodes.length === 0 ? (
                <div className="card text-center py-8 border-dashed">
                  <p className="text-xs text-dark-500">{t('sys_opt.no_nodes')}</p>
                </div>
              ) : (
                <div className="space-y-2">
                  {panelNodes.map(n => renderNodeCard(n, 'panel'))}
                </div>
              )}
            </div>
          </div>
        </>
      )}

      {/* Info card */}
      <motion.div
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        className="card bg-dark-800/30 border-dark-700/30 mt-6"
      >
        <div className="flex items-start gap-3">
          <AlertTriangle className="w-5 h-5 text-warning flex-shrink-0 mt-0.5" />
          <div>
            <p className="text-sm text-dark-300 font-medium mb-1">{t('sys_opt.info_title')}</p>
            <p className="text-sm text-dark-500">{t('sys_opt.info_text')}</p>
          </div>
        </div>
      </motion.div>

      {/* Click outside to close dropdowns */}
      {(modeDropdown !== null || confirmRemove !== null) && (
        <div className="fixed inset-0 z-40" onClick={() => { setModeDropdown(null); setConfirmRemove(null) }} />
      )}
    </motion.div>
  )
}
