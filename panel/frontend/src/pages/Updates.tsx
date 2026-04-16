import { useState, useEffect, useCallback, useRef } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { toast } from 'sonner'
import {
  Download,
  RefreshCw,
  CheckCircle2,
  XCircle,
  Loader2,
  Server as ServerIcon,
  Package,
  ArrowUpCircle,
  AlertTriangle,
  Clock,
  Check,
} from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { systemApi, VersionBaseInfo, SingleNodeVersion } from '../api/client'
import { Skeleton } from '../components/ui/Skeleton'
import { FAQIcon } from '../components/FAQ'

type NodeLoadState = 'pending' | 'loading' | 'loaded' | 'error'

interface NodeState {
  id: number
  name: string
  url: string
  loadState: NodeLoadState
  version: string | null
  status: 'online' | 'offline'
}

export default function Updates() {
  const { t } = useTranslation()

  const [baseInfo, setBaseInfo] = useState<VersionBaseInfo | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const [nodes, setNodes] = useState<Map<number, NodeState>>(new Map())

  const [updatingPanel, setUpdatingPanel] = useState(false)
  const [updatingNodes, setUpdatingNodes] = useState<Set<number>>(new Set())
  const [updatingAll, setUpdatingAll] = useState(false)

  const [updateResults, setUpdateResults] = useState<Record<string, { success: boolean; message: string }>>({})

  const [isChecking, setIsChecking] = useState(false)

  const abortRef = useRef(false)
  const lastActivityRef = useRef(Date.now())
  const IDLE_THRESHOLD = 5000
  const AUTO_REFRESH_INTERVAL = 12000

  useEffect(() => {
    const markActive = () => { lastActivityRef.current = Date.now() }
    const events = ['mousemove', 'mousedown', 'keydown', 'scroll', 'touchstart'] as const
    events.forEach(e => document.addEventListener(e, markActive))
    return () => { events.forEach(e => document.removeEventListener(e, markActive)) }
  }, [])

  const fetchNodeVersion = useCallback(async (nodeId: number) => {
    setNodes(prev => {
      const next = new Map(prev)
      const existing = next.get(nodeId)
      if (existing) next.set(nodeId, { ...existing, loadState: 'loading' })
      return next
    })

    try {
      const resp = await systemApi.getNodeVersionById(nodeId)
      const data: SingleNodeVersion = resp.data

      setNodes(prev => {
        const next = new Map(prev)
        next.set(nodeId, {
          id: data.id,
          name: data.name,
          url: data.url,
          loadState: 'loaded',
          version: data.version,
          status: data.status,
        })
        return next
      })
    } catch {
      setNodes(prev => {
        const next = new Map(prev)
        const existing = next.get(nodeId)
        if (existing) {
          next.set(nodeId, { ...existing, loadState: 'error', status: 'offline' })
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
      const data = resp.data
      setBaseInfo(data)

      const initialNodes = new Map<number, NodeState>()
      for (const n of data.nodes) {
        initialNodes.set(n.id, {
          id: n.id,
          name: n.name,
          url: n.url,
          loadState: 'pending',
          version: null,
          status: 'offline',
        })
      }
      setNodes(initialNodes)

      // Запускаем загрузку каждой ноды параллельно
      for (const n of data.nodes) {
        if (abortRef.current) break
        fetchNodeVersion(n.id)
      }
    } catch {
      setError(t('updates.failed_fetch'))
    } finally {
      setLoading(false)
      setIsChecking(false)
    }
  }, [t, fetchNodeVersion])

  useEffect(() => {
    fetchBase()
    return () => { abortRef.current = true }
  }, [fetchBase])

  useEffect(() => {
    const id = setInterval(() => {
      const isIdle = Date.now() - lastActivityRef.current > IDLE_THRESHOLD
      const isVisible = !document.hidden
      const isBusy = updatingPanel || updatingNodes.size > 0 || updatingAll || isChecking
      if (isIdle && isVisible && !isBusy) fetchBase()
    }, AUTO_REFRESH_INTERVAL)
    return () => clearInterval(id)
  }, [fetchBase, updatingPanel, updatingNodes, updatingAll, isChecking])

  const handleRefresh = useCallback(() => {
    abortRef.current = true
    setUpdateResults({})
    fetchBase(true)
  }, [fetchBase])

  const handleUpdatePanel = async () => {
    if (updatingPanel) return

    setUpdatingPanel(true)
    setUpdateResults(prev => ({ ...prev, panel: { success: true, message: t('updates.in_progress') } }))

    try {
      const response = await systemApi.updatePanel()
      setUpdateResults(prev => ({
        ...prev,
        panel: { success: true, message: response.data.message }
      }))
      toast.success(t('updates.panel_restarting'))
      setTimeout(() => {
        setUpdateResults(prev => ({
          ...prev,
          panel: { success: true, message: t('updates.panel_restarting') }
        }))
      }, 2000)
    } catch (err: any) {
      setUpdateResults(prev => ({
        ...prev,
        panel: { success: false, message: err.response?.data?.detail || t('updates.failed_update') }
      }))
      toast.error(err.response?.data?.detail || t('updates.failed_update'))
      setUpdatingPanel(false)
    }
  }

  const handleUpdateNode = async (nodeId: number, nodeName: string) => {
    if (updatingNodes.has(nodeId)) return

    setUpdatingNodes(prev => new Set(prev).add(nodeId))
    setUpdateResults(prev => ({
      ...prev,
      [`node-${nodeId}`]: { success: true, message: t('updates.in_progress') }
    }))

    try {
      const response = await systemApi.updateNode(nodeId)
      setUpdateResults(prev => ({
        ...prev,
        [`node-${nodeId}`]: { success: true, message: response.data.message }
      }))

      setTimeout(() => {
        fetchNodeVersion(nodeId)
        setUpdatingNodes(prev => {
          const next = new Set(prev)
          next.delete(nodeId)
          return next
        })
      }, 5000)
    } catch (err: any) {
      setUpdateResults(prev => ({
        ...prev,
        [`node-${nodeId}`]: { success: false, message: err.response?.data?.detail || t('updates.failed_update') }
      }))
      toast.error(`${nodeName}: ${err.response?.data?.detail || t('updates.failed_update')}`)
      setUpdatingNodes(prev => {
        const next = new Set(prev)
        next.delete(nodeId)
        return next
      })
    }
  }

  const handleUpdateAllNodes = async () => {
    if (updatingAll || !baseInfo) return

    setUpdatingAll(true)
    const outdated = Array.from(nodes.values()).filter(n =>
      n.loadState === 'loaded' && n.status === 'online' && getNodeNeedsUpdate(n)
    )

    await Promise.all(outdated.map(n => handleUpdateNode(n.id, n.name)))

    setUpdatingAll(false)
  }

  const getNodeNeedsUpdate = (node: NodeState): boolean => {
    if (!node.version || !baseInfo?.node.latest_version) return false
    return node.version !== baseInfo.node.latest_version
  }

  const loadedNodes = Array.from(nodes.values())
  const nodesNeedUpdate = loadedNodes.filter(n =>
    n.loadState === 'loaded' && n.status === 'online' && getNodeNeedsUpdate(n)
  ).length

  const allNodesLoaded = loadedNodes.every(n => n.loadState === 'loaded' || n.loadState === 'error')

  if (loading) {
    return (
      <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }}>
        <div className="flex items-center gap-3 mb-6">
          <Skeleton className="w-10 h-10 rounded-xl" />
          <div>
            <Skeleton className="h-6 w-44 mb-2" />
            <Skeleton className="h-4 w-64" />
          </div>
        </div>
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          {Array.from({ length: 4 }).map((_, i) => (
            <div key={i} className="card">
              <Skeleton className="h-5 w-32 mb-4" />
              <Skeleton className="h-20 w-full" />
            </div>
          ))}
        </div>
      </motion.div>
    )
  }

  return (
    <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }}>
      {/* Header */}
      <motion.div
        className="flex items-center justify-between mb-8"
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.4 }}
      >
        <div>
          <motion.h1
            className="text-2xl font-bold text-dark-50 flex items-center gap-3"
            initial={{ opacity: 0, x: -20 }}
            animate={{ opacity: 1, x: 0 }}
          >
            <Package className="w-7 h-7 text-accent-400" />
            {t('updates.title')}
            <FAQIcon screen="PAGE_UPDATES" />
          </motion.h1>
          <motion.p
            className="text-dark-400 mt-1"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            transition={{ delay: 0.1 }}
          >
            {t('updates.subtitle')}
          </motion.p>
        </div>

        <motion.button
          onClick={handleRefresh}
          className="btn btn-secondary"
          whileHover={{ scale: 1.02 }}
          whileTap={{ scale: 0.98 }}
          disabled={loading || isChecking}
        >
          <RefreshCw className={`w-4 h-4 ${isChecking ? 'animate-spin' : ''}`} />
          {isChecking ? t('updates.checking') : t('common.refresh')}
        </motion.button>
      </motion.div>

      {/* Error */}
      <AnimatePresence>
        {error && (
          <motion.div
            className="flex items-center gap-3 p-4 mb-6 bg-danger/10 border border-danger/20 rounded-xl text-danger"
            initial={{ opacity: 0, y: -10 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -10 }}
          >
            <AlertTriangle className="w-5 h-5 flex-shrink-0" />
            <span className="text-sm">{error}</span>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Panel Version Card */}
      <motion.div
        className="card mb-6 group hover:border-dark-700 transition-all"
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.4 }}
      >
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-4">
            <motion.div
              className="w-14 h-14 rounded-xl bg-gradient-to-br from-accent-500/20 to-accent-600/20
                         flex items-center justify-center border border-accent-500/20
                         group-hover:shadow-lg group-hover:shadow-accent-500/10 transition-shadow"
              whileHover={{ rotate: 5, scale: 1.05 }}
            >
              <Package className="w-6 h-6 text-accent-500" />
            </motion.div>
            <div>
              <h2 className="text-lg font-semibold text-dark-100">{t('updates.panel')}</h2>
              <div className="flex items-center gap-3 mt-1">
                <span className="text-dark-400 text-sm">
                  {t('updates.current_version')}:
                  <span className="text-dark-200 ml-1 font-mono">
                    v{baseInfo?.panel.version || '?'}
                  </span>
                </span>
                {baseInfo?.panel.latest_version && (
                  <span className="text-dark-500 text-sm">
                    {t('updates.latest')}:
                    <span className="text-dark-300 ml-1 font-mono">
                      v{baseInfo.panel.latest_version}
                    </span>
                  </span>
                )}
              </div>
            </div>
          </div>

          <div className="flex items-center gap-3">
            <AnimatePresence>
              {updateResults.panel && (
                <motion.div
                  className={`flex items-center gap-1.5 text-sm px-3 py-1.5 rounded-lg ${
                    updateResults.panel.success
                      ? 'text-success bg-success/10'
                      : 'text-danger bg-danger/10'
                  }`}
                  initial={{ opacity: 0, scale: 0.8 }}
                  animate={{ opacity: 1, scale: 1 }}
                  exit={{ opacity: 0, scale: 0.8 }}
                >
                  {updatingPanel ? (
                    <Loader2 className="w-4 h-4 animate-spin" />
                  ) : updateResults.panel.success ? (
                    <CheckCircle2 className="w-4 h-4" />
                  ) : (
                    <XCircle className="w-4 h-4" />
                  )}
                  <span className="max-w-[200px] truncate">{updateResults.panel.message}</span>
                </motion.div>
              )}
            </AnimatePresence>

            {baseInfo?.panel.update_available && !updatingPanel && (
              <motion.span
                className="px-3 py-1 text-xs font-medium bg-accent-500/20 text-accent-400 rounded-full"
                initial={{ scale: 0 }}
                animate={{ scale: 1 }}
              >
                {t('updates.update_available')}
              </motion.span>
            )}

            {!baseInfo?.panel.update_available && !updatingPanel && (
              <span className="flex items-center gap-1.5 text-sm text-success">
                <Check className="w-4 h-4" />
                {t('updates.up_to_date')}
              </span>
            )}

            <motion.button
              onClick={handleUpdatePanel}
              disabled={updatingPanel || !baseInfo?.panel.update_available}
              className="btn btn-primary"
              whileHover={{ scale: 1.02 }}
              whileTap={{ scale: 0.98 }}
            >
              {updatingPanel ? (
                <Loader2 className="w-4 h-4 animate-spin" />
              ) : (
                <Download className="w-4 h-4" />
              )}
              {t('updates.update_panel')}
            </motion.button>
          </div>
        </div>
      </motion.div>

      {/* Nodes Section */}
      <motion.div initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.4 }}>
        <div className="flex items-center justify-between mb-4">
          <div>
            <h2 className="text-lg font-semibold text-dark-100 flex items-center gap-2">
              <ServerIcon className="w-5 h-5 text-accent-500" />
              {t('updates.nodes')}
              <span className="text-dark-500 font-normal text-sm">
                ({loadedNodes.length})
              </span>
            </h2>
            {baseInfo?.node.latest_version && (
              <p className="text-sm text-dark-500 mt-1 ml-7">
                {t('updates.latest')}:
                <span className="text-dark-300 ml-1 font-mono">
                  v{baseInfo.node.latest_version}
                </span>
              </p>
            )}
          </div>

          {nodesNeedUpdate > 0 && (
            <motion.button
              onClick={handleUpdateAllNodes}
              disabled={updatingAll || nodesNeedUpdate === 0}
              className="btn btn-secondary"
              whileHover={{ scale: 1.02 }}
              whileTap={{ scale: 0.98 }}
            >
              {updatingAll ? (
                <Loader2 className="w-4 h-4 animate-spin" />
              ) : (
                <ArrowUpCircle className="w-4 h-4" />
              )}
              {t('updates.update_all_nodes')} ({nodesNeedUpdate})
            </motion.button>
          )}
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
          <AnimatePresence mode="popLayout">
            {loadedNodes.length === 0 ? (
              <motion.div
                className="card text-center py-12 col-span-full"
                initial={{ opacity: 0, scale: 0.95 }}
                animate={{ opacity: 1, scale: 1 }}
                key="empty"
              >
                <ServerIcon className="w-12 h-12 text-dark-600 mx-auto mb-3" />
                <p className="text-dark-400">{t('updates.no_nodes')}</p>
              </motion.div>
            ) : (
              loadedNodes.map((node, index) => {
                const isUpdating = updatingNodes.has(node.id)
                const updateResult = updateResults[`node-${node.id}`]
                const needsUpdate = node.loadState === 'loaded' && getNodeNeedsUpdate(node)
                const isNodeLoading = node.loadState === 'pending' || node.loadState === 'loading'

                return (
                  <motion.div
                    key={node.id}
                    className="card group hover:border-dark-700 transition-all overflow-visible flex flex-col"
                    initial={{ opacity: 0, y: 20 }}
                    animate={{ opacity: 1, y: 0 }}
                    exit={{ opacity: 0, scale: 0.95 }}
                    transition={{ delay: index * 0.03 }}
                    layout
                  >
                    {/* Шапка: иконка + имя + статус */}
                    <div className="flex items-center gap-3">
                      <motion.div
                        className={`w-10 h-10 rounded-xl flex items-center justify-center border flex-shrink-0
                          ${isNodeLoading
                            ? 'bg-dark-800/50 border-dark-700/30 animate-pulse'
                            : node.status === 'online'
                              ? 'bg-gradient-to-br from-dark-700 to-dark-800 border-dark-700/50 group-hover:border-accent-500/30'
                              : 'bg-dark-800/50 border-dark-700/30'
                          } transition-colors`}
                        whileHover={{ rotate: 5, scale: 1.05 }}
                      >
                        {isNodeLoading ? (
                          <Loader2 className="w-4 h-4 text-dark-500 animate-spin" />
                        ) : (
                          <ServerIcon className={`w-4 h-4 ${
                            node.status === 'online' ? 'text-accent-500' : 'text-dark-500'
                          }`} />
                        )}
                      </motion.div>
                      <div className="min-w-0 flex-1">
                        <h3 className="font-semibold text-dark-100 flex items-center gap-2 truncate">
                          <span className="truncate">{node.name}</span>
                          {!isNodeLoading && (
                            <span className={`w-2 h-2 rounded-full flex-shrink-0 ${
                              node.status === 'online' ? 'bg-success' : 'bg-dark-500'
                            }`} />
                          )}
                        </h3>
                        {isNodeLoading ? (
                          <Skeleton className="h-4 w-20 mt-0.5" />
                        ) : (
                          <span className="text-xs text-dark-500">
                            <span className={`font-mono ${
                              node.version ? 'text-dark-300' : 'text-dark-500'
                            }`}>
                              {node.version ? `v${node.version}` : t('updates.unknown')}
                            </span>
                          </span>
                        )}
                      </div>
                    </div>

                    {/* Статус + кнопка */}
                    <div className="flex items-center justify-between mt-3 pt-3 border-t border-dark-700/30">
                      <div className="flex-1 min-w-0">
                        <AnimatePresence>
                          {updateResult && (
                            <motion.div
                              className={`flex items-center gap-1.5 text-xs px-2 py-1 rounded-lg w-fit ${
                                updateResult.success
                                  ? 'text-success bg-success/10'
                                  : 'text-danger bg-danger/10'
                              }`}
                              initial={{ opacity: 0, scale: 0.8 }}
                              animate={{ opacity: 1, scale: 1 }}
                              exit={{ opacity: 0, scale: 0.8 }}
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
                          )}
                        </AnimatePresence>

                        {!updateResult && isNodeLoading && (
                          <span className="flex items-center gap-1.5 text-xs text-dark-500">
                            <Loader2 className="w-3.5 h-3.5 animate-spin" />
                          </span>
                        )}

                        {!updateResult && !isNodeLoading && node.status === 'offline' && (
                          <span className="flex items-center gap-1.5 text-xs text-dark-500">
                            <Clock className="w-3.5 h-3.5" />
                            {t('updates.offline')}
                          </span>
                        )}

                        {!updateResult && !isNodeLoading && node.status === 'online' && needsUpdate && !isUpdating && (
                          <motion.span
                            className="px-2 py-0.5 text-xs font-medium bg-accent-500/20 text-accent-400 rounded-full"
                            initial={{ scale: 0 }}
                            animate={{ scale: 1 }}
                          >
                            {t('updates.update_available')}
                          </motion.span>
                        )}

                        {!updateResult && !isNodeLoading && node.status === 'online' && !needsUpdate && !isUpdating && (
                          <span className="flex items-center gap-1.5 text-xs text-success">
                            <Check className="w-3.5 h-3.5" />
                            {t('updates.up_to_date')}
                          </span>
                        )}
                      </div>

                      <motion.button
                        onClick={() => handleUpdateNode(node.id, node.name)}
                        disabled={isUpdating || isNodeLoading || node.status === 'offline' || !needsUpdate}
                        className="btn btn-secondary text-xs px-3 py-1.5 flex-shrink-0"
                        whileHover={{ scale: 1.05 }}
                        whileTap={{ scale: 0.95 }}
                      >
                        {isUpdating ? (
                          <Loader2 className="w-3.5 h-3.5 animate-spin" />
                        ) : (
                          <Download className="w-3.5 h-3.5" />
                        )}
                        {t('updates.update')}
                      </motion.button>
                    </div>
                  </motion.div>
                )
              })
            )}
          </AnimatePresence>
        </div>
      </motion.div>

      {/* All Up To Date Card */}
      {baseInfo && allNodesLoaded && !baseInfo.panel.update_available && nodesNeedUpdate === 0 && (
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.4 }}
          className="card bg-success/5 border-success/20 mt-6"
        >
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-full bg-success/10 flex items-center justify-center">
              <CheckCircle2 className="w-5 h-5 text-success" />
            </div>
            <div>
              <p className="text-sm text-dark-200 font-medium">
                {t('updates.all_up_to_date')}
              </p>
              <p className="text-sm text-dark-500">
                {t('updates.all_up_to_date_desc')}
              </p>
            </div>
          </div>
        </motion.div>
      )}

      {/* Info Card */}
      <motion.div
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.4 }}
        className="card bg-dark-800/30 border-dark-700/30 mt-6"
      >
        <div className="flex items-start gap-3">
          <AlertTriangle className="w-5 h-5 text-warning flex-shrink-0 mt-0.5" />
          <div>
            <p className="text-sm text-dark-300 font-medium mb-1">
              {t('updates.warning_title')}
            </p>
            <p className="text-sm text-dark-500">
              {t('updates.warning_text')}
            </p>
          </div>
        </div>

        <div className="flex items-start gap-3 mt-4 pt-4 border-t border-dark-700/30">
          <Clock className="w-5 h-5 text-accent-400 flex-shrink-0 mt-0.5" />
          <div>
            <p className="text-sm text-dark-300 font-medium mb-1">
              {t('updates.duration_title')}
            </p>
            <p className="text-sm text-dark-500">
              {t('updates.duration_text')}
            </p>
          </div>
        </div>
      </motion.div>
    </motion.div>
  )
}
