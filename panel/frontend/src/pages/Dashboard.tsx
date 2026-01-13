import { useEffect, useMemo, useRef, useState } from 'react'
import {
  DndContext,
  closestCenter,
  KeyboardSensor,
  PointerSensor,
  TouchSensor,
  useSensor,
  useSensors,
  DragEndEvent,
  DragStartEvent,
  DragOverlay,
} from '@dnd-kit/core'
import {
  arrayMove,
  SortableContext,
  sortableKeyboardCoordinates,
  rectSortingStrategy,
} from '@dnd-kit/sortable'
import { motion, AnimatePresence } from 'framer-motion'
import { 
  Server as ServerIcon, 
  LayoutGrid, 
  List,
  Plus,
  Activity,
  Wifi,
  WifiOff,
  Zap,
  Database,
  Minus,
  Equal,
  AlignJustify,
  Grid3x3,
  Square
} from 'lucide-react'
import { useNavigate, useParams } from 'react-router-dom'
import { useServersStore } from '../stores/serversStore'
import { useSettingsStore } from '../stores/settingsStore'
import { useSmartRefresh } from '../hooks/useAutoRefresh'
import ServerCard from '../components/Dashboard/ServerCard'
import { useTranslation } from 'react-i18next'

const containerVariants = {
  hidden: { opacity: 0 },
  visible: {
    opacity: 1,
    transition: {
      staggerChildren: 0.05,
      delayChildren: 0.1
    }
  }
}

const itemVariants = {
  hidden: { opacity: 0, y: 20 },
  visible: { 
    opacity: 1, 
    y: 0,
    transition: { duration: 0.4, ease: 'easeOut' }
  }
}

export default function Dashboard() {
  const { uid } = useParams()
  const navigate = useNavigate()
  const { servers, fetchServersWithMetrics, fetchAllMetrics, fetchAllTraffic, reorderServers, isLoading } = useServersStore()
  const { refreshInterval, compactView, trafficPeriod, setCompactView, fetchSettings, detailLevel, cardScale, setDetailLevel, setCardScale } = useSettingsStore()
  const { t } = useTranslation()
  
  const initialLoadDone = useRef(false)
  const [activeId, setActiveId] = useState<number | null>(null)
  
  const sensors = useSensors(
    useSensor(PointerSensor, {
      activationConstraint: {
        distance: 8,
      },
    }),
    useSensor(TouchSensor, {
      activationConstraint: {
        delay: 200,
        tolerance: 8,
      },
    }),
    useSensor(KeyboardSensor, {
      coordinateGetter: sortableKeyboardCoordinates,
    })
  )
  
  // Initial load - fetch servers with cached metrics (fast), then traffic in background
  useEffect(() => {
    fetchSettings()
    fetchServersWithMetrics().then(() => {
      initialLoadDone.current = true
    })
    // Load traffic separately (longer cache, less frequent)
    fetchAllTraffic(trafficPeriod)
  }, [fetchServersWithMetrics, fetchAllTraffic, fetchSettings, trafficPeriod])
  
  
  // Smart refresh: always use cached metrics from panel DB (no direct node requests)
  // Traffic is fetched separately with longer interval
  const { isPageVisible } = useSmartRefresh(
    async () => {
      await fetchAllMetrics() // Always use cached data from panel DB
    },
    async () => {
      await fetchAllMetrics() // Same for background - cached data
    },
    { immediate: false }
  )
  
  const handleDragStart = (event: DragStartEvent) => {
    setActiveId(event.active.id as number)
  }
  
  const handleDragEnd = (event: DragEndEvent) => {
    const { active, over } = event
    
    if (over && active.id !== over.id) {
      const oldIndex = servers.findIndex(s => s.id === active.id)
      const newIndex = servers.findIndex(s => s.id === over.id)
      const newOrder = arrayMove(servers, oldIndex, newIndex)
      reorderServers(newOrder.map(s => s.id))
    }
    
    setActiveId(null)
  }
  
  const activeServer = activeId ? servers.find(s => s.id === activeId) : null
  
  // Memoize server counts to avoid recalculating on every render
  const { onlineCount, offlineCount, serverIds } = useMemo(() => ({
    onlineCount: servers.filter(s => s.status === 'online').length,
    offlineCount: servers.filter(s => s.status === 'offline').length,
    serverIds: servers.map(s => s.id)
  }), [servers])
  
  const subtitle = servers.length === 1 
    ? t('dashboard.subtitle_one', { count: servers.length })
    : t('dashboard.subtitle_other', { count: servers.length })

  return (
    <motion.div 
      variants={containerVariants}
      initial="hidden"
      animate="visible"
    >
      {/* Header */}
      <motion.div 
        className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4 mb-8"
        variants={itemVariants}
      >
        <div>
          <motion.h1 
            className="text-2xl font-bold text-dark-50 flex items-center gap-3"
            initial={{ opacity: 0, x: -20 }}
            animate={{ opacity: 1, x: 0 }}
            transition={{ duration: 0.5 }}
          >
            <Activity className="w-7 h-7 text-accent-400" />
            {t('dashboard.title')}
          </motion.h1>
          <motion.p 
            className="text-dark-400 mt-1 flex items-center gap-3"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            transition={{ delay: 0.2 }}
          >
            <span>{subtitle}</span>
            <span className="flex items-center gap-1.5">
              <Wifi className="w-3.5 h-3.5 text-success" />
              <span className="text-success">{onlineCount}</span>
            </span>
            {offlineCount > 0 && (
              <span className="flex items-center gap-1.5">
                <WifiOff className="w-3.5 h-3.5 text-danger" />
                <span className="text-danger">{offlineCount}</span>
              </span>
            )}
          </motion.p>
        </div>
        
        <motion.div 
          className="flex items-center gap-3"
          initial={{ opacity: 0, x: 20 }}
          animate={{ opacity: 1, x: 0 }}
          transition={{ delay: 0.3 }}
        >
          {/* View toggle */}
          <div className="flex items-center bg-dark-800/60 backdrop-blur-sm rounded-xl p-1 border border-dark-700/50">
            <motion.button
              onClick={() => setCompactView(false)}
              className={`p-2.5 rounded-lg transition-all ${
                !compactView 
                  ? 'bg-accent-500/20 text-accent-400 shadow-lg shadow-accent-500/10' 
                  : 'text-dark-400 hover:text-dark-200'
              }`}
              whileHover={{ scale: 1.05 }}
              whileTap={{ scale: 0.95 }}
              title={t('dashboard.grid_view')}
            >
              <LayoutGrid className="w-4 h-4" />
            </motion.button>
            <motion.button
              onClick={() => setCompactView(true)}
              className={`p-2.5 rounded-lg transition-all ${
                compactView 
                  ? 'bg-accent-500/20 text-accent-400 shadow-lg shadow-accent-500/10' 
                  : 'text-dark-400 hover:text-dark-200'
              }`}
              whileHover={{ scale: 1.05 }}
              whileTap={{ scale: 0.95 }}
              title={t('dashboard.list_view')}
            >
              <List className="w-4 h-4" />
            </motion.button>
          </div>
          
          {/* Detail level toggle - only visible in grid view */}
          {!compactView && (
            <div className="hidden md:flex items-center bg-dark-800/60 backdrop-blur-sm rounded-xl p-1 border border-dark-700/50">
              <motion.button
                onClick={() => setDetailLevel('minimal')}
                className={`p-2.5 rounded-lg transition-all ${
                  detailLevel === 'minimal'
                    ? 'bg-accent-500/20 text-accent-400 shadow-lg shadow-accent-500/10' 
                    : 'text-dark-400 hover:text-dark-200'
                }`}
                whileHover={{ scale: 1.05 }}
                whileTap={{ scale: 0.95 }}
                title={t('dashboard.detail_minimal')}
              >
                <Minus className="w-4 h-4" />
              </motion.button>
              <motion.button
                onClick={() => setDetailLevel('standard')}
                className={`p-2.5 rounded-lg transition-all ${
                  detailLevel === 'standard'
                    ? 'bg-accent-500/20 text-accent-400 shadow-lg shadow-accent-500/10' 
                    : 'text-dark-400 hover:text-dark-200'
                }`}
                whileHover={{ scale: 1.05 }}
                whileTap={{ scale: 0.95 }}
                title={t('dashboard.detail_standard')}
              >
                <Equal className="w-4 h-4" />
              </motion.button>
              <motion.button
                onClick={() => setDetailLevel('detailed')}
                className={`p-2.5 rounded-lg transition-all ${
                  detailLevel === 'detailed'
                    ? 'bg-accent-500/20 text-accent-400 shadow-lg shadow-accent-500/10' 
                    : 'text-dark-400 hover:text-dark-200'
                }`}
                whileHover={{ scale: 1.05 }}
                whileTap={{ scale: 0.95 }}
                title={t('dashboard.detail_detailed')}
              >
                <AlignJustify className="w-4 h-4" />
              </motion.button>
            </div>
          )}
          
          {/* Card scale toggle - only visible in grid view */}
          {!compactView && (
            <div className="hidden lg:flex items-center bg-dark-800/60 backdrop-blur-sm rounded-xl p-1 border border-dark-700/50">
              <motion.button
                onClick={() => setCardScale('small')}
                className={`p-2.5 rounded-lg transition-all ${
                  cardScale === 'small'
                    ? 'bg-accent-500/20 text-accent-400 shadow-lg shadow-accent-500/10' 
                    : 'text-dark-400 hover:text-dark-200'
                }`}
                whileHover={{ scale: 1.05 }}
                whileTap={{ scale: 0.95 }}
                title={t('dashboard.scale_small')}
              >
                <Grid3x3 className="w-4 h-4" />
              </motion.button>
              <motion.button
                onClick={() => setCardScale('medium')}
                className={`p-2.5 rounded-lg transition-all ${
                  cardScale === 'medium'
                    ? 'bg-accent-500/20 text-accent-400 shadow-lg shadow-accent-500/10' 
                    : 'text-dark-400 hover:text-dark-200'
                }`}
                whileHover={{ scale: 1.05 }}
                whileTap={{ scale: 0.95 }}
                title={t('dashboard.scale_medium')}
              >
                <LayoutGrid className="w-4 h-4" />
              </motion.button>
              <motion.button
                onClick={() => setCardScale('large')}
                className={`p-2.5 rounded-lg transition-all ${
                  cardScale === 'large'
                    ? 'bg-accent-500/20 text-accent-400 shadow-lg shadow-accent-500/10' 
                    : 'text-dark-400 hover:text-dark-200'
                }`}
                whileHover={{ scale: 1.05 }}
                whileTap={{ scale: 0.95 }}
                title={t('dashboard.scale_large')}
              >
                <Square className="w-4 h-4" />
              </motion.button>
            </div>
          )}
          
          <motion.div 
            className="text-xs text-dark-500 hidden sm:flex items-center gap-1.5 bg-dark-800/40 px-3 py-2 rounded-lg"
            animate={{ opacity: [0.5, 1, 0.5] }}
            transition={{ duration: 2, repeat: Infinity }}
          >
            {isPageVisible ? (
              <>
                <Zap className="w-3.5 h-3.5 text-accent-500" />
                <span className="text-accent-400">{t('dashboard.live_mode')}</span>
                <span className="text-dark-600">•</span>
                <span>{refreshInterval}s</span>
              </>
            ) : (
              <>
                <Database className="w-3.5 h-3.5 text-dark-500" />
                <span>{t('dashboard.background_mode')}</span>
              </>
            )}
          </motion.div>
          
          <motion.button
            onClick={() => navigate(`/${uid}/servers`)}
            className="btn btn-primary"
            whileHover={{ scale: 1.02 }}
            whileTap={{ scale: 0.98 }}
          >
            <Plus className="w-4 h-4" />
            <span className="hidden sm:inline">{t('common.add_server')}</span>
          </motion.button>
        </motion.div>
      </motion.div>
      
      {/* Content */}
      <AnimatePresence mode="wait">
        {isLoading && servers.length === 0 ? (
          <motion.div 
            className="flex flex-col items-center justify-center h-64 gap-4"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            key="loading"
          >
            <div className="relative">
              <motion.div
                className="w-12 h-12 border-2 border-accent-500/30 rounded-full"
                animate={{ rotate: 360 }}
                transition={{ duration: 2, repeat: Infinity, ease: 'linear' }}
              />
              <motion.div
                className="absolute inset-0 w-12 h-12 border-2 border-transparent border-t-accent-500 rounded-full"
                animate={{ rotate: 360 }}
                transition={{ duration: 1, repeat: Infinity, ease: 'linear' }}
              />
            </div>
            <p className="text-dark-400">{t('dashboard.loading_servers')}</p>
          </motion.div>
        ) : servers.length === 0 ? (
          <motion.div 
            className="card text-center py-20"
            initial={{ opacity: 0, scale: 0.9 }}
            animate={{ opacity: 1, scale: 1 }}
            transition={{ duration: 0.5 }}
            key="empty"
          >
            <motion.div
              initial={{ y: 20, opacity: 0 }}
              animate={{ y: 0, opacity: 1 }}
              transition={{ delay: 0.2 }}
            >
              <motion.div
                animate={{ y: [0, -10, 0] }}
                transition={{ duration: 3, repeat: Infinity, ease: 'easeInOut' }}
              >
                <ServerIcon className="w-20 h-20 text-dark-600 mx-auto mb-6" />
              </motion.div>
              <h2 className="text-xl font-semibold text-dark-200 mb-2">{t('dashboard.no_servers')}</h2>
              <p className="text-dark-400 mb-8">{t('dashboard.add_first')}</p>
              <motion.button
                onClick={() => navigate(`/${uid}/servers`)}
                className="btn btn-primary mx-auto"
                whileHover={{ scale: 1.05 }}
                whileTap={{ scale: 0.95 }}
              >
                <Plus className="w-4 h-4" />
                {t('common.add_server')}
              </motion.button>
            </motion.div>
          </motion.div>
        ) : (
          <DndContext
            sensors={sensors}
            collisionDetection={closestCenter}
            onDragStart={handleDragStart}
            onDragEnd={handleDragEnd}
          >
            <SortableContext
              items={serverIds}
              strategy={rectSortingStrategy}
            >
              <motion.div 
                className={
                  compactView 
                    ? 'space-y-3' 
                    : cardScale === 'small'
                      ? 'grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4'
                      : cardScale === 'large'
                        ? 'grid grid-cols-1 lg:grid-cols-2 gap-6'
                        : 'grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-5'
                }
                variants={containerVariants}
                key="servers"
              >
                {servers.map((server, index) => (
                  <ServerCard
                    key={server.id}
                    server={server}
                    compact={compactView}
                    detailLevel={detailLevel}
                    index={index}
                  />
                ))}
              </motion.div>
            </SortableContext>
            
            <DragOverlay>
              {activeServer && (
                <div className="opacity-90">
                  <ServerCard
                    server={activeServer}
                    compact={compactView}
                    detailLevel={detailLevel}
                    index={0}
                  />
                </div>
              )}
            </DragOverlay>
          </DndContext>
        )}
      </AnimatePresence>
    </motion.div>
  )
}
