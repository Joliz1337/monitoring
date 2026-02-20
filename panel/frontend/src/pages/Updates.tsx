import { useState, useEffect, useCallback } from 'react'
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
  Settings2
} from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { systemApi, VersionInfo, NodeVersionInfo } from '../api/client'
import { Skeleton } from '../components/ui/Skeleton'

interface OptimizationsNodeInfo {
  id: number
  name: string
  installed: boolean
  version: string | null
  status: 'online' | 'offline'
  update_available: boolean
}

export default function Updates() {
  const { t } = useTranslation()
  
  const [versionInfo, setVersionInfo] = useState<VersionInfo | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  
  const [updatingPanel, setUpdatingPanel] = useState(false)
  const [updatingNodes, setUpdatingNodes] = useState<Set<number>>(new Set())
  const [updatingAll, setUpdatingAll] = useState(false)
  
  const [updateResults, setUpdateResults] = useState<Record<string, { success: boolean; message: string }>>({})
  
  const [isChecking, setIsChecking] = useState(false)
  
  // System Optimizations state
  const [applyingOptimizations, setApplyingOptimizations] = useState<Set<number>>(new Set())
  const [applyingAllOptimizations, setApplyingAllOptimizations] = useState(false)
  const [optimizationsResults, setOptimizationsResults] = useState<Record<string, { success: boolean; message: string }>>({})
  
  const fetchVersionInfo = useCallback(async (showCheckingState = false) => {
    try {
      setError('')
      if (showCheckingState) setIsChecking(true)
      
      // Single request now contains all version info including optimizations
      const versionResponse = await systemApi.getVersion()
      setVersionInfo(versionResponse.data)
    } catch (err) {
      setError(t('updates.failed_fetch'))
      console.error('Failed to fetch version info:', err)
    } finally {
      setLoading(false)
      setIsChecking(false)
    }
  }, [t])
  
  useEffect(() => {
    fetchVersionInfo()
  }, [fetchVersionInfo])
  
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
  
  const handleUpdateNode = async (node: NodeVersionInfo) => {
    if (updatingNodes.has(node.id)) return
    
    setUpdatingNodes(prev => new Set(prev).add(node.id))
    setUpdateResults(prev => ({ 
      ...prev, 
      [`node-${node.id}`]: { success: true, message: t('updates.in_progress') } 
    }))
    
    try {
      const response = await systemApi.updateNode(node.id)
      setUpdateResults(prev => ({ 
        ...prev, 
        [`node-${node.id}`]: { success: true, message: response.data.message } 
      }))
      
      // Refresh version info after a delay
      setTimeout(() => {
        fetchVersionInfo()
        setUpdatingNodes(prev => {
          const next = new Set(prev)
          next.delete(node.id)
          return next
        })
      }, 5000)
    } catch (err: any) {
      setUpdateResults(prev => ({ 
        ...prev, 
        [`node-${node.id}`]: { success: false, message: err.response?.data?.detail || t('updates.failed_update') } 
      }))
      toast.error(`${node.name}: ${err.response?.data?.detail || t('updates.failed_update')}`)
      setUpdatingNodes(prev => {
        const next = new Set(prev)
        next.delete(node.id)
        return next
      })
    }
  }
  
  const handleUpdateAllNodes = async () => {
    if (updatingAll || !versionInfo) return
    
    setUpdatingAll(true)
    const outdatedNodes = versionInfo.nodes.filter(n => 
      n.status === 'online' && getNodeUpdateAvailable(n)
    )
    
    for (const node of outdatedNodes) {
      await handleUpdateNode(node)
    }
    
    setUpdatingAll(false)
  }
  
  // Build optimizations info from versionInfo (combined data)
  const getOptimizationsNodes = (): OptimizationsNodeInfo[] => {
    if (!versionInfo) return []
    const latestOptVersion = versionInfo.optimizations?.latest_version
    
    return versionInfo.nodes.map(node => {
      const opt = node.optimizations || { installed: false, version: null }
      
      // Calculate update_available based on combined data
      let update_available = false
      if (node.status === 'online' && latestOptVersion) {
        if (opt.installed) {
          // If installed but version unknown (legacy) or version mismatch - update available
          update_available = !opt.version || opt.version !== latestOptVersion
        } else {
          // Not installed - update available (install)
          update_available = true
        }
      }
      
      return {
        id: node.id,
        name: node.name,
        installed: opt.installed,
        version: opt.version,
        status: node.status,
        update_available
      }
    })
  }
  
  const optimizationsNodes = getOptimizationsNodes()
  
  // System Optimizations handlers
  const handleApplyOptimizations = async (node: OptimizationsNodeInfo) => {
    if (applyingOptimizations.has(node.id)) return
    
    setApplyingOptimizations(prev => new Set(prev).add(node.id))
    setOptimizationsResults(prev => ({ 
      ...prev, 
      [`opt-${node.id}`]: { success: true, message: t('updates.in_progress') } 
    }))
    
    try {
      const response = await systemApi.applyNodeOptimizations(node.id)
      setOptimizationsResults(prev => ({ 
        ...prev, 
        [`opt-${node.id}`]: { success: true, message: response.data.message } 
      }))
      
      // Refresh version info after a delay
      setTimeout(() => {
        fetchVersionInfo()
        setApplyingOptimizations(prev => {
          const next = new Set(prev)
          next.delete(node.id)
          return next
        })
      }, 3000)
    } catch (err: any) {
      setOptimizationsResults(prev => ({ 
        ...prev, 
        [`opt-${node.id}`]: { success: false, message: err.response?.data?.detail || t('updates.failed_update') } 
      }))
      toast.error(`${node.name}: ${err.response?.data?.detail || t('updates.failed_update')}`)
      setApplyingOptimizations(prev => {
        const next = new Set(prev)
        next.delete(node.id)
        return next
      })
    }
  }
  
  const handleApplyAllOptimizations = async () => {
    if (applyingAllOptimizations || !versionInfo) return
    
    setApplyingAllOptimizations(true)
    const nodesToUpdate = optimizationsNodes.filter(n => 
      n.status === 'online' && n.update_available
    )
    
    await Promise.all(nodesToUpdate.map(node => handleApplyOptimizations(node)))
    
    setApplyingAllOptimizations(false)
  }
  
  const optimizationsNeedUpdate = optimizationsNodes.filter(n => 
    n.status === 'online' && n.update_available
  ).length
  
  const getNodeUpdateAvailable = (node: NodeVersionInfo): boolean => {
    if (!node.version || !versionInfo?.node.latest_version) return false
    return node.version !== versionInfo.node.latest_version
  }
  
  const nodesNeedUpdate = versionInfo?.nodes.filter(n => 
    n.status === 'online' && getNodeUpdateAvailable(n)
  ).length || 0
  
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
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
    >
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
          onClick={() => fetchVersionInfo(true)}
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
                    v{versionInfo?.panel.version || '?'}
                  </span>
                </span>
                {versionInfo?.panel.latest_version && (
                  <span className="text-dark-500 text-sm">
                    {t('updates.latest')}: 
                    <span className="text-dark-300 ml-1 font-mono">
                      v{versionInfo.panel.latest_version}
                    </span>
                  </span>
                )}
              </div>
            </div>
          </div>
          
          <div className="flex items-center gap-3">
            {/* Update result */}
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
            
            {/* Update available badge */}
            {versionInfo?.panel.update_available && !updatingPanel && (
              <motion.span 
                className="px-3 py-1 text-xs font-medium bg-accent-500/20 text-accent-400 rounded-full"
                initial={{ scale: 0 }}
                animate={{ scale: 1 }}
              >
                {t('updates.update_available')}
              </motion.span>
            )}
            
            {/* No update needed */}
            {!versionInfo?.panel.update_available && !updatingPanel && (
              <span className="flex items-center gap-1.5 text-sm text-success">
                <Check className="w-4 h-4" />
                {t('updates.up_to_date')}
              </span>
            )}
            
            <motion.button
              onClick={handleUpdatePanel}
              disabled={updatingPanel || !versionInfo?.panel.update_available}
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
                ({versionInfo?.nodes.length || 0})
              </span>
            </h2>
            {versionInfo?.node.latest_version && (
              <p className="text-sm text-dark-500 mt-1 ml-7">
                {t('updates.latest')}: 
                <span className="text-dark-300 ml-1 font-mono">
                  v{versionInfo.node.latest_version}
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
        
        <div className="space-y-3">
          <AnimatePresence mode="popLayout">
            {!versionInfo?.nodes.length ? (
              <motion.div 
                className="card text-center py-12"
                initial={{ opacity: 0, scale: 0.95 }}
                animate={{ opacity: 1, scale: 1 }}
                key="empty"
              >
                <ServerIcon className="w-12 h-12 text-dark-600 mx-auto mb-3" />
                <p className="text-dark-400">{t('updates.no_nodes')}</p>
              </motion.div>
            ) : (
              versionInfo.nodes.map((node, index) => {
                const isUpdating = updatingNodes.has(node.id)
                const updateResult = updateResults[`node-${node.id}`]
                const needsUpdate = getNodeUpdateAvailable(node)
                
                return (
                  <motion.div 
                    key={node.id}
                    className="card group hover:border-dark-700 transition-all overflow-visible"
                    initial={{ opacity: 0, y: 20 }}
                    animate={{ opacity: 1, y: 0 }}
                    exit={{ opacity: 0, x: -100 }}
                    transition={{ delay: index * 0.05 }}
                    layout
                  >
                    <div className="flex items-center justify-between">
                      <div className="flex items-center gap-4">
                        <motion.div 
                          className={`w-12 h-12 rounded-xl flex items-center justify-center border
                            ${node.status === 'online' 
                              ? 'bg-gradient-to-br from-dark-700 to-dark-800 border-dark-700/50 group-hover:border-accent-500/30' 
                              : 'bg-dark-800/50 border-dark-700/30'
                            } transition-colors`}
                          whileHover={{ rotate: 5, scale: 1.05 }}
                        >
                          <ServerIcon className={`w-5 h-5 ${
                            node.status === 'online' ? 'text-accent-500' : 'text-dark-500'
                          }`} />
                        </motion.div>
                        <div>
                          <h3 className="font-semibold text-dark-100 flex items-center gap-2">
                            {node.name}
                            <span className={`w-2 h-2 rounded-full ${
                              node.status === 'online' ? 'bg-success' : 'bg-dark-500'
                            }`} />
                          </h3>
                          <div className="flex items-center gap-3 mt-0.5">
                            <span className="text-sm text-dark-500">
                              {t('updates.version')}: 
                              <span className={`ml-1 font-mono ${
                                node.version ? 'text-dark-300' : 'text-dark-500'
                              }`}>
                                {node.version ? `v${node.version}` : t('updates.unknown')}
                              </span>
                            </span>
                          </div>
                        </div>
                      </div>
                      
                      <div className="flex items-center gap-3">
                        {/* Update result */}
                        <AnimatePresence>
                          {updateResult && (
                            <motion.div 
                              className={`flex items-center gap-1.5 text-sm px-3 py-1.5 rounded-lg ${
                                updateResult.success 
                                  ? 'text-success bg-success/10' 
                                  : 'text-danger bg-danger/10'
                              }`}
                              initial={{ opacity: 0, scale: 0.8 }}
                              animate={{ opacity: 1, scale: 1 }}
                              exit={{ opacity: 0, scale: 0.8 }}
                            >
                              {isUpdating ? (
                                <Loader2 className="w-4 h-4 animate-spin" />
                              ) : updateResult.success ? (
                                <CheckCircle2 className="w-4 h-4" />
                              ) : (
                                <XCircle className="w-4 h-4" />
                              )}
                              <span className="max-w-[150px] truncate">{updateResult.message}</span>
                            </motion.div>
                          )}
                        </AnimatePresence>
                        
                        {/* Status indicators */}
                        {node.status === 'offline' && (
                          <span className="flex items-center gap-1.5 text-sm text-dark-500">
                            <Clock className="w-4 h-4" />
                            {t('updates.offline')}
                          </span>
                        )}
                        
                        {node.status === 'online' && needsUpdate && !isUpdating && !updateResult && (
                          <motion.span 
                            className="px-2.5 py-1 text-xs font-medium bg-accent-500/20 text-accent-400 rounded-full"
                            initial={{ scale: 0 }}
                            animate={{ scale: 1 }}
                          >
                            {t('updates.update_available')}
                          </motion.span>
                        )}
                        
                        {node.status === 'online' && !needsUpdate && !isUpdating && !updateResult && (
                          <span className="flex items-center gap-1.5 text-sm text-success">
                            <Check className="w-4 h-4" />
                            {t('updates.up_to_date')}
                          </span>
                        )}
                        
                        <motion.button
                          onClick={() => handleUpdateNode(node)}
                          disabled={isUpdating || node.status === 'offline' || !needsUpdate}
                          className="btn btn-secondary text-sm"
                          whileHover={{ scale: 1.05 }}
                          whileTap={{ scale: 0.95 }}
                        >
                          {isUpdating ? (
                            <Loader2 className="w-4 h-4 animate-spin" />
                          ) : (
                            <Download className="w-4 h-4" />
                          )}
                          {t('updates.update')}
                        </motion.button>
                      </div>
                    </div>
                  </motion.div>
                )
              })
            )}
          </AnimatePresence>
        </div>
      </motion.div>
      
      {/* System Optimizations Section */}
      {versionInfo && optimizationsNodes.length > 0 && (
        <motion.div initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.4 }} className="mt-8">
          <div className="flex items-center justify-between mb-4">
            <div>
              <h2 className="text-lg font-semibold text-dark-100 flex items-center gap-2">
                <Settings2 className="w-5 h-5 text-accent-500" />
                {t('updates.optimizations')}
                <span className="text-dark-500 font-normal text-sm">
                  ({optimizationsNodes.length})
                </span>
              </h2>
              {versionInfo.optimizations?.latest_version && (
                <p className="text-sm text-dark-500 mt-1 ml-7">
                  {t('updates.latest')}: 
                  <span className="text-dark-300 ml-1 font-mono">
                    v{versionInfo.optimizations.latest_version}
                  </span>
                </p>
              )}
            </div>
            
            {optimizationsNeedUpdate > 0 && (
              <motion.button
                onClick={handleApplyAllOptimizations}
                disabled={applyingAllOptimizations || optimizationsNeedUpdate === 0}
                className="btn btn-secondary"
                whileHover={{ scale: 1.02 }}
                whileTap={{ scale: 0.98 }}
              >
                {applyingAllOptimizations ? (
                  <Loader2 className="w-4 h-4 animate-spin" />
                ) : (
                  <ArrowUpCircle className="w-4 h-4" />
                )}
                {t('updates.update_all_optimizations')} ({optimizationsNeedUpdate})
              </motion.button>
            )}
          </div>
          
          <div className="space-y-3">
            <AnimatePresence mode="popLayout">
              {optimizationsNodes.map((node, index) => {
                const isApplying = applyingOptimizations.has(node.id)
                const applyResult = optimizationsResults[`opt-${node.id}`]
                
                return (
                  <motion.div 
                    key={`opt-${node.id}`}
                    className="card group hover:border-dark-700 transition-all"
                    initial={{ opacity: 0, y: 20 }}
                    animate={{ opacity: 1, y: 0 }}
                    exit={{ opacity: 0, x: -100 }}
                    transition={{ delay: index * 0.05 }}
                    layout
                  >
                    <div className="flex items-center justify-between">
                      <div className="flex items-center gap-4">
                        <motion.div 
                          className={`w-12 h-12 rounded-xl flex items-center justify-center border
                            ${node.status === 'online' 
                              ? 'bg-gradient-to-br from-dark-700 to-dark-800 border-dark-700/50 group-hover:border-accent-500/30' 
                              : 'bg-dark-800/50 border-dark-700/30'
                            } transition-colors`}
                          whileHover={{ rotate: 5, scale: 1.05 }}
                        >
                          <Settings2 className={`w-5 h-5 ${
                            node.status === 'online' ? 'text-accent-500' : 'text-dark-500'
                          }`} />
                        </motion.div>
                        <div>
                          <h3 className="font-semibold text-dark-100 flex items-center gap-2">
                            {node.name}
                            <span className={`w-2 h-2 rounded-full ${
                              node.status === 'online' ? 'bg-success' : 'bg-dark-500'
                            }`} />
                          </h3>
                          <div className="flex items-center gap-3 mt-0.5">
                            <span className="text-sm text-dark-500">
                              {t('updates.version')}: 
                              <span className={`ml-1 font-mono ${
                                node.version ? 'text-dark-300' : 'text-dark-500'
                              }`}>
                                {node.installed 
                                  ? (node.version ? `v${node.version}` : t('updates.legacy_version'))
                                  : t('updates.not_installed')
                                }
                              </span>
                            </span>
                          </div>
                        </div>
                      </div>
                      
                      <div className="flex items-center gap-3">
                        {/* Apply result */}
                        <AnimatePresence>
                          {applyResult && (
                            <motion.div 
                              className={`flex items-center gap-1.5 text-sm px-3 py-1.5 rounded-lg ${
                                applyResult.success 
                                  ? 'text-success bg-success/10' 
                                  : 'text-danger bg-danger/10'
                              }`}
                              initial={{ opacity: 0, scale: 0.8 }}
                              animate={{ opacity: 1, scale: 1 }}
                              exit={{ opacity: 0, scale: 0.8 }}
                            >
                              {isApplying ? (
                                <Loader2 className="w-4 h-4 animate-spin" />
                              ) : applyResult.success ? (
                                <CheckCircle2 className="w-4 h-4" />
                              ) : (
                                <XCircle className="w-4 h-4" />
                              )}
                              <span className="max-w-[150px] truncate">{applyResult.message}</span>
                            </motion.div>
                          )}
                        </AnimatePresence>
                        
                        {/* Status indicators */}
                        {node.status === 'offline' && (
                          <span className="flex items-center gap-1.5 text-sm text-dark-500">
                            <Clock className="w-4 h-4" />
                            {t('updates.offline')}
                          </span>
                        )}
                        
                        {node.status === 'online' && node.update_available && !isApplying && !applyResult && (
                          <motion.span 
                            className="px-2.5 py-1 text-xs font-medium bg-accent-500/20 text-accent-400 rounded-full"
                            initial={{ scale: 0 }}
                            animate={{ scale: 1 }}
                          >
                            {t('updates.update_available')}
                          </motion.span>
                        )}
                        
                        {node.status === 'online' && !node.update_available && !isApplying && !applyResult && (
                          <span className="flex items-center gap-1.5 text-sm text-success">
                            <Check className="w-4 h-4" />
                            {t('updates.up_to_date')}
                          </span>
                        )}
                        
                        <motion.button
                          onClick={() => handleApplyOptimizations(node)}
                          disabled={isApplying || node.status === 'offline' || !node.update_available}
                          className="btn btn-secondary text-sm"
                          whileHover={{ scale: 1.05 }}
                          whileTap={{ scale: 0.95 }}
                        >
                          {isApplying ? (
                            <Loader2 className="w-4 h-4 animate-spin" />
                          ) : (
                            <Download className="w-4 h-4" />
                          )}
                          {t('updates.update')}
                        </motion.button>
                      </div>
                    </div>
                  </motion.div>
                )
              })}
            </AnimatePresence>
          </div>
        </motion.div>
      )}
      
      {/* All Up To Date Card */}
      {versionInfo && !versionInfo.panel.update_available && nodesNeedUpdate === 0 && optimizationsNeedUpdate === 0 && (
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
        
        {/* Duration notice */}
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
