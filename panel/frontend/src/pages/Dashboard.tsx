import { useEffect, useMemo, useRef, useState, useCallback } from 'react'
import {
  DndContext,
  closestCenter,
  pointerWithin,
  KeyboardSensor,
  PointerSensor,
  TouchSensor,
  useSensor,
  useSensors,
  DragEndEvent,
  DragStartEvent,
  DragOverEvent,
  DragOverlay,
  useDroppable,
  type CollisionDetection,
} from '@dnd-kit/core'
import {
  arrayMove,
  SortableContext,
  sortableKeyboardCoordinates,
  rectSortingStrategy,
  verticalListSortingStrategy,
  useSortable,
} from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'
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
  Square,
  PowerOff,
  FolderPlus,
  Folder,
  FolderOpen,
  ChevronRight,
  ChevronDown,
  Pencil,
  Trash2,
  X,
  Loader2,
  GripVertical,
} from 'lucide-react'
import { useNavigate, useParams } from 'react-router-dom'
import { useServersStore } from '../stores/serversStore'
import { useSettingsStore } from '../stores/settingsStore'
import { useAutoRefresh } from '../hooks/useAutoRefresh'
import ServerCard from '../components/Dashboard/ServerCard'
import { ServerCardSkeleton } from '../components/ui/Skeleton'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'

const COLLAPSED_KEY = 'dashboard_collapsed_folders'
const FOLDER_ORDER_KEY = 'dashboard_folder_order'

function loadCollapsed(): Set<string> {
  try {
    const raw = localStorage.getItem(COLLAPSED_KEY)
    return raw ? new Set(JSON.parse(raw)) : new Set()
  } catch { return new Set() }
}

function saveCollapsed(set: Set<string>) {
  localStorage.setItem(COLLAPSED_KEY, JSON.stringify([...set]))
}

function loadFolderOrder(): string[] {
  try {
    return JSON.parse(localStorage.getItem(FOLDER_ORDER_KEY) || '[]')
  } catch { return [] }
}

function saveFolderOrder(order: string[]) {
  localStorage.setItem(FOLDER_ORDER_KEY, JSON.stringify(order))
}

export default function Dashboard() {
  const { uid } = useParams()
  const navigate = useNavigate()
  const { servers, fetchServersWithMetrics, reorderServers, moveToFolder, renameFolder, deleteFolder, isLoading } = useServersStore()
  const { refreshInterval, compactView, setCompactView, fetchSettings, detailLevel, cardScale, setDetailLevel, setCardScale } = useSettingsStore()
  const { t } = useTranslation()
  
  const initialLoadDone = useRef(false)
  const [dragType, setDragType] = useState<'server' | 'folder' | null>(null)
  const [activeId, setActiveId] = useState<string | number | null>(null)
  const [overFolderId, setOverFolderId] = useState<string | null>(null)
  const [collapsed, setCollapsed] = useState<Set<string>>(loadCollapsed)
  const [emptyFolders, setEmptyFolders] = useState<string[]>([])
  const [folderOrder, setFolderOrder] = useState<string[]>(loadFolderOrder)
  const [modalState, setModalState] = useState<
    | { kind: 'none' }
    | { kind: 'create-folder' }
    | { kind: 'rename-folder'; folderName: string }
  >({ kind: 'none' })
  
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 8 } }),
    useSensor(TouchSensor, { activationConstraint: { delay: 200, tolerance: 8 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates })
  )
  
  useEffect(() => {
    fetchSettings()
    fetchServersWithMetrics().then(() => { initialLoadDone.current = true })
  }, [fetchServersWithMetrics, fetchSettings])
  
  const { isPageVisible } = useAutoRefresh(
    fetchServersWithMetrics,
    { immediate: false, pauseWhenHidden: true, refreshOnVisible: true }
  )

  const { activeServers, onlineCount, offlineCount, disabledCount } = useMemo(() => {
    const active = servers.filter(s => s.is_active)
    return {
      activeServers: active,
      onlineCount: active.filter(s => s.status === 'online').length,
      offlineCount: active.filter(s => s.status === 'offline').length,
      disabledCount: servers.filter(s => !s.is_active).length,
    }
  }, [servers])

  const folders = useMemo(() => {
    const allFolders = new Set<string>()
    for (const s of activeServers) if (s.folder) allFolders.add(s.folder)
    for (const f of emptyFolders) allFolders.add(f)

    const ordered = folderOrder.filter(f => allFolders.has(f))
    const remaining = [...allFolders].filter(f => !folderOrder.includes(f)).sort()
    return [...ordered, ...remaining]
  }, [activeServers, emptyFolders, folderOrder])

  const folderSortableIds = useMemo(
    () => folders.map(f => `sortable-folder:${f}`),
    [folders]
  )

  const grouped = useMemo(() => {
    const map = new Map<string | null, typeof activeServers>()
    for (const s of activeServers) {
      const key = s.folder || null
      if (!map.has(key)) map.set(key, [])
      map.get(key)!.push(s)
    }
    return map
  }, [activeServers])

  const serverFolderMap = useMemo(() => {
    const map = new Map<number, string | null>()
    for (const s of activeServers) map.set(s.id, s.folder || null)
    return map
  }, [activeServers])

  const toggleCollapsed = useCallback((folder: string) => {
    setCollapsed(prev => {
      const next = new Set(prev)
      if (next.has(folder)) next.delete(folder)
      else next.add(folder)
      saveCollapsed(next)
      return next
    })
  }, [])

  const collisionDetection: CollisionDetection = useCallback((args) => {
    if (dragType === 'folder') {
      return closestCenter({
        ...args,
        droppableContainers: args.droppableContainers.filter(c =>
          String(c.id).startsWith('sortable-folder:')
        ),
      })
    }

    const draggedId = args.active.id as number
    const draggedFolder = serverFolderMap.get(draggedId) ?? null

    const withoutFolderSortables = args.droppableContainers.filter(c =>
      !String(c.id).startsWith('sortable-folder:')
    )

    const hits = pointerWithin({ ...args, droppableContainers: withoutFolderSortables })

    const sameFolderServerHits: typeof hits = []
    const folderZoneHits: typeof hits = []

    for (const hit of hits) {
      const idStr = String(hit.id)
      if (idStr.startsWith('folder:') || idStr === 'drop:unfolder') {
        folderZoneHits.push(hit)
      } else if (typeof hit.id === 'number' && serverFolderMap.get(hit.id) === draggedFolder) {
        sameFolderServerHits.push(hit)
      }
    }

    if (sameFolderServerHits.length > 0) return sameFolderServerHits
    if (folderZoneHits.length > 0) return folderZoneHits

    const relevantContainers = withoutFolderSortables.filter(c => {
      const idStr = String(c.id)
      if (idStr.startsWith('folder:') || idStr === 'drop:unfolder') return true
      if (typeof c.id === 'number') return serverFolderMap.get(c.id) === draggedFolder
      return false
    })
    return closestCenter({ ...args, droppableContainers: relevantContainers })
  }, [dragType, serverFolderMap])
  
  const handleDragStart = (event: DragStartEvent) => {
    const id = String(event.active.id)
    if (id.startsWith('sortable-folder:')) {
      setDragType('folder')
      setActiveId(id)
    } else {
      setDragType('server')
      setActiveId(event.active.id as number)
    }
  }

  const handleDragOver = (event: DragOverEvent) => {
    if (dragType === 'folder') return
    const { over } = event
    if (!over) { setOverFolderId(null); return }
    const overId = String(over.id)
    if (overId.startsWith('folder:')) setOverFolderId(overId.replace('folder:', ''))
    else if (overId === 'drop:unfolder') setOverFolderId('__unfolder__')
    else setOverFolderId(null)
  }
  
  const handleDragEnd = async (event: DragEndEvent) => {
    const { active, over } = event
    const prevDragType = dragType
    setDragType(null)
    setActiveId(null)
    setOverFolderId(null)
    
    if (!over) return

    const activeStr = String(active.id)
    const overStr = String(over.id)

    // Folder reorder
    if (prevDragType === 'folder' && activeStr.startsWith('sortable-folder:') && overStr.startsWith('sortable-folder:')) {
      const af = activeStr.replace('sortable-folder:', '')
      const of_ = overStr.replace('sortable-folder:', '')
      if (af !== of_) {
        const oldIdx = folders.indexOf(af)
        const newIdx = folders.indexOf(of_)
        if (oldIdx !== -1 && newIdx !== -1) {
          const newOrder = arrayMove([...folders], oldIdx, newIdx)
          setFolderOrder(newOrder)
          saveFolderOrder(newOrder)
        }
      }
      return
    }

    // Server → folder
    if (prevDragType === 'server') {
      const draggedId = active.id as number

      if (overStr.startsWith('folder:')) {
        const targetFolder = overStr.replace('folder:', '')
        const srv = activeServers.find(s => s.id === draggedId)
        if (srv && srv.folder !== targetFolder) {
          try {
            await moveToFolder([draggedId], targetFolder)
            toast.success(t('dashboard.server_moved'))
          } catch { toast.error(t('common.action_failed')) }
        }
        return
      }

      if (overStr === 'drop:unfolder') {
        const srv = activeServers.find(s => s.id === draggedId)
        if (srv && srv.folder) {
          try {
            await moveToFolder([draggedId], null)
            toast.success(t('dashboard.server_moved'))
          } catch { toast.error(t('common.action_failed')) }
        }
        return
      }

      // Server reorder
      if (active.id !== over.id) {
        const oldIndex = activeServers.findIndex(s => s.id === active.id)
        const newIndex = activeServers.findIndex(s => s.id === over.id)
        if (oldIndex !== -1 && newIndex !== -1) {
          const newOrder = arrayMove(activeServers, oldIndex, newIndex)
          reorderServers(newOrder.map(s => s.id))
        }
      }
    }
  }

  const handleDeleteFolder = async (folderName: string) => {
    if (!confirm(t('dashboard.confirm_delete_folder'))) return
    try {
      await deleteFolder(folderName)
      setEmptyFolders(prev => prev.filter(f => f !== folderName))
      setFolderOrder(prev => { const next = prev.filter(f => f !== folderName); saveFolderOrder(next); return next })
      toast.success(t('dashboard.folder_deleted'))
    } catch { toast.error(t('common.action_failed')) }
  }
  
  const activeServer = dragType === 'server' && typeof activeId === 'number'
    ? activeServers.find(s => s.id === activeId) : null
  const activeFolderName = dragType === 'folder' && typeof activeId === 'string'
    ? activeId.replace('sortable-folder:', '') : null
  
  const subtitle = activeServers.length === 1 
    ? t('dashboard.subtitle_one', { count: activeServers.length })
    : t('dashboard.subtitle_other', { count: activeServers.length })

  const gridClass = compactView 
    ? 'space-y-3' 
    : cardScale === 'small'
      ? 'grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4'
      : cardScale === 'large'
        ? 'grid grid-cols-1 lg:grid-cols-2 gap-6'
        : 'grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-5'

  const unfolderedServers = grouped.get(null) || []

  return (
    <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }}>
      {/* Header */}
      <motion.div 
        className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4 mb-8"
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.4 }}
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
            {disabledCount > 0 && (
              <span className="flex items-center gap-1.5">
                <PowerOff className="w-3.5 h-3.5 text-dark-500" />
                <span className="text-dark-500">{disabledCount}</span>
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
          <div className="flex items-center bg-dark-800/60 backdrop-blur-sm rounded-xl p-1 border border-dark-700/50">
            <motion.button
              onClick={() => setCompactView(false)}
              className={`p-2.5 rounded-lg transition-all ${!compactView ? 'bg-accent-500/20 text-accent-400 shadow-lg shadow-accent-500/10' : 'text-dark-400 hover:text-dark-200'}`}
              whileHover={{ scale: 1.05 }} whileTap={{ scale: 0.95 }}
              title={t('dashboard.grid_view')}
            >
              <LayoutGrid className="w-4 h-4" />
            </motion.button>
            <motion.button
              onClick={() => setCompactView(true)}
              className={`p-2.5 rounded-lg transition-all ${compactView ? 'bg-accent-500/20 text-accent-400 shadow-lg shadow-accent-500/10' : 'text-dark-400 hover:text-dark-200'}`}
              whileHover={{ scale: 1.05 }} whileTap={{ scale: 0.95 }}
              title={t('dashboard.list_view')}
            >
              <List className="w-4 h-4" />
            </motion.button>
          </div>
          
          {!compactView && (
            <div className="hidden md:flex items-center bg-dark-800/60 backdrop-blur-sm rounded-xl p-1 border border-dark-700/50">
              {(['minimal', 'standard', 'detailed'] as const).map(level => (
                <motion.button
                  key={level}
                  onClick={() => setDetailLevel(level)}
                  className={`p-2.5 rounded-lg transition-all ${detailLevel === level ? 'bg-accent-500/20 text-accent-400 shadow-lg shadow-accent-500/10' : 'text-dark-400 hover:text-dark-200'}`}
                  whileHover={{ scale: 1.05 }} whileTap={{ scale: 0.95 }}
                  title={t(`dashboard.detail_${level}`)}
                >
                  {level === 'minimal' ? <Minus className="w-4 h-4" /> : level === 'standard' ? <Equal className="w-4 h-4" /> : <AlignJustify className="w-4 h-4" />}
                </motion.button>
              ))}
            </div>
          )}
          
          {!compactView && (
            <div className="hidden lg:flex items-center bg-dark-800/60 backdrop-blur-sm rounded-xl p-1 border border-dark-700/50">
              {(['small', 'medium', 'large'] as const).map(scale => (
                <motion.button
                  key={scale}
                  onClick={() => setCardScale(scale)}
                  className={`p-2.5 rounded-lg transition-all ${cardScale === scale ? 'bg-accent-500/20 text-accent-400 shadow-lg shadow-accent-500/10' : 'text-dark-400 hover:text-dark-200'}`}
                  whileHover={{ scale: 1.05 }} whileTap={{ scale: 0.95 }}
                  title={t(`dashboard.scale_${scale}`)}
                >
                  {scale === 'small' ? <Grid3x3 className="w-4 h-4" /> : scale === 'medium' ? <LayoutGrid className="w-4 h-4" /> : <Square className="w-4 h-4" />}
                </motion.button>
              ))}
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
            onClick={() => setModalState({ kind: 'create-folder' })}
            className="p-2.5 bg-dark-800/60 backdrop-blur-sm rounded-xl border border-dark-700/50 text-dark-400 hover:text-white transition"
            whileHover={{ scale: 1.05 }} whileTap={{ scale: 0.95 }}
            title={t('dashboard.create_folder')}
          >
            <FolderPlus className="w-4 h-4" />
          </motion.button>
          
          <motion.button
            onClick={() => navigate(`/${uid}/servers`)}
            className="btn btn-primary"
            whileHover={{ scale: 1.02 }} whileTap={{ scale: 0.98 }}
          >
            <Plus className="w-4 h-4" />
            <span className="hidden sm:inline">{t('common.add_server')}</span>
          </motion.button>
        </motion.div>
      </motion.div>
      
      {/* Content */}
      <AnimatePresence mode="wait">
        {isLoading && activeServers.length === 0 ? (
          <motion.div className={gridClass} initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} key="loading">
            {Array.from({ length: 6 }).map((_, i) => (
              <ServerCardSkeleton key={i} compact={compactView} />
            ))}
          </motion.div>
        ) : activeServers.length === 0 ? (
          <motion.div className="card text-center py-20" initial={{ opacity: 0, scale: 0.9 }} animate={{ opacity: 1, scale: 1 }} transition={{ duration: 0.5 }} key="empty">
            <motion.div initial={{ y: 20, opacity: 0 }} animate={{ y: 0, opacity: 1 }} transition={{ delay: 0.2 }}>
              <motion.div animate={{ y: [0, -10, 0] }} transition={{ duration: 3, repeat: Infinity, ease: 'easeInOut' }}>
                <ServerIcon className="w-20 h-20 text-dark-600 mx-auto mb-6" />
              </motion.div>
              <h2 className="text-xl font-semibold text-dark-200 mb-2">{t('dashboard.no_servers')}</h2>
              <p className="text-dark-400 mb-8">{t('dashboard.add_first')}</p>
              <motion.button onClick={() => navigate(`/${uid}/servers`)} className="btn btn-primary mx-auto" whileHover={{ scale: 1.05 }} whileTap={{ scale: 0.95 }}>
                <Plus className="w-4 h-4" />
                {t('common.add_server')}
              </motion.button>
            </motion.div>
          </motion.div>
        ) : (
          <DndContext
            sensors={sensors}
            collisionDetection={collisionDetection}
            onDragStart={handleDragStart}
            onDragOver={handleDragOver}
            onDragEnd={handleDragEnd}
          >
            <motion.div className="space-y-6" initial={{ opacity: 0 }} animate={{ opacity: 1 }} key="servers">
              {/* Sortable folder list */}
              <SortableContext items={folderSortableIds} strategy={verticalListSortingStrategy}>
                {folders.map(folderName => {
                  const isCollapsed = collapsed.has(folderName)
                  const folderServers = grouped.get(folderName) || []
                  return (
                    <SortableFolderItem
                      key={folderName}
                      folderId={folderName}
                      isDropOver={overFolderId === folderName && dragType === 'server'}
                    >
                      {(handleProps) => (
                        <>
                          <div className="flex items-center justify-between px-4 py-3">
                            <div className="flex items-center gap-1 flex-1 min-w-0">
                              <div
                                ref={handleProps.ref}
                                {...handleProps.listeners}
                                {...handleProps.attributes}
                                className="p-1 text-dark-600 hover:text-dark-400 cursor-grab active:cursor-grabbing transition rounded flex-shrink-0"
                              >
                                <GripVertical className="w-4 h-4" />
                              </div>
                              <button
                                onClick={() => toggleCollapsed(folderName)}
                                className="flex items-center gap-2.5 flex-1 min-w-0 group"
                              >
                                <div className="w-8 h-8 rounded-lg bg-blue-500/15 flex items-center justify-center flex-shrink-0">
                                  {isCollapsed ? <Folder className="w-4 h-4 text-blue-400" /> : <FolderOpen className="w-4 h-4 text-blue-400" />}
                                </div>
                                <span className="text-sm font-semibold text-white truncate group-hover:text-blue-300 transition">{folderName}</span>
                                <span className="text-xs text-dark-500 flex-shrink-0">{folderServers.length}</span>
                                {isCollapsed ? <ChevronRight className="w-3.5 h-3.5 text-dark-600 flex-shrink-0" /> : <ChevronDown className="w-3.5 h-3.5 text-dark-600 flex-shrink-0" />}
                              </button>
                            </div>
                            <div className="flex items-center gap-1 flex-shrink-0 ml-2">
                              <button onClick={() => setModalState({ kind: 'rename-folder', folderName })} className="p-1.5 text-dark-500 hover:text-dark-300 transition rounded-lg hover:bg-dark-800/50">
                                <Pencil className="w-3.5 h-3.5" />
                              </button>
                              <button onClick={() => handleDeleteFolder(folderName)} className="p-1.5 text-dark-500 hover:text-red-400 transition rounded-lg hover:bg-dark-800/50">
                                <Trash2 className="w-3.5 h-3.5" />
                              </button>
                            </div>
                          </div>
                          <AnimatePresence initial={false}>
                            {!isCollapsed && (
                              <motion.div
                                initial={{ height: 0, opacity: 0 }}
                                animate={{ height: 'auto', opacity: 1 }}
                                exit={{ height: 0, opacity: 0 }}
                                transition={{ duration: 0.2 }}
                                className="overflow-hidden"
                              >
                                <div className="px-3 pb-3">
                                  {folderServers.length > 0 ? (
                                    <SortableContext items={folderServers.map(s => s.id)} strategy={rectSortingStrategy}>
                                      <div className={gridClass}>
                                        {folderServers.map((server, index) => (
                                          <ServerCard key={server.id} server={server} compact={compactView} detailLevel={detailLevel} index={index} />
                                        ))}
                                      </div>
                                    </SortableContext>
                                  ) : (
                                    <div className="py-6 text-center text-dark-500 text-xs">{t('dashboard.no_servers')}</div>
                                  )}
                                </div>
                              </motion.div>
                            )}
                          </AnimatePresence>
                        </>
                      )}
                    </SortableFolderItem>
                  )
                })}
              </SortableContext>

              {/* Servers without folder */}
              <UnfolderDropZone isOver={overFolderId === '__unfolder__' && dragType === 'server'} hasServers={unfolderedServers.length > 0} hasFolders={folders.length > 0}>
                <SortableContext items={unfolderedServers.map(s => s.id)} strategy={rectSortingStrategy}>
                  <div className={gridClass}>
                    {unfolderedServers.map((server, index) => (
                      <ServerCard key={server.id} server={server} compact={compactView} detailLevel={detailLevel} index={index} />
                    ))}
                  </div>
                </SortableContext>
              </UnfolderDropZone>
            </motion.div>
            
            <DragOverlay>
              {activeServer && (
                <div className="opacity-90">
                  <ServerCard server={activeServer} compact={compactView} detailLevel={detailLevel} index={0} />
                </div>
              )}
              {activeFolderName && (
                <div className="opacity-90 bg-dark-900 border border-blue-500/40 rounded-xl px-4 py-3 flex items-center gap-2.5 shadow-2xl">
                  <GripVertical className="w-4 h-4 text-dark-500" />
                  <div className="w-8 h-8 rounded-lg bg-blue-500/15 flex items-center justify-center">
                    <Folder className="w-4 h-4 text-blue-400" />
                  </div>
                  <span className="text-sm font-semibold text-white">{activeFolderName}</span>
                  <span className="text-xs text-dark-500">{(grouped.get(activeFolderName) || []).length}</span>
                </div>
              )}
            </DragOverlay>
          </DndContext>
        )}
      </AnimatePresence>

      {/* Modals */}
      {modalState.kind === 'create-folder' && (
        <FolderModal
          t={t}
          title={t('dashboard.create_folder')}
          initialValue=""
          existingFolders={folders}
          onClose={() => setModalState({ kind: 'none' })}
          onSubmit={(name) => {
            setEmptyFolders(prev => prev.includes(name) ? prev : [...prev, name])
            setModalState({ kind: 'none' })
            toast.success(t('dashboard.folder_created'))
          }}
        />
      )}
      {modalState.kind === 'rename-folder' && (
        <RenameFolderModal
          t={t}
          folderName={modalState.folderName}
          onClose={() => setModalState({ kind: 'none' })}
          onRenamed={(oldName, newName) => {
            renameFolder(oldName, newName)
            setEmptyFolders(prev => prev.map(f => f === oldName ? newName : f))
            setFolderOrder(prev => { const next = prev.map(f => f === oldName ? newName : f); saveFolderOrder(next); return next })
            setModalState({ kind: 'none' })
            toast.success(t('dashboard.folder_renamed'))
          }}
        />
      )}
    </motion.div>
  )
}

/* ------------------------------------------------------------------ */
/*  Sortable folder with droppable zone                                */
/* ------------------------------------------------------------------ */

function SortableFolderItem({ folderId, isDropOver, children }: {
  folderId: string
  isDropOver: boolean
  children: (handleProps: { ref: (node: HTMLElement | null) => void; listeners: ReturnType<typeof useSortable>['listeners']; attributes: ReturnType<typeof useSortable>['attributes'] }) => React.ReactNode
}) {
  const {
    setNodeRef: setSortableRef,
    setActivatorNodeRef,
    attributes,
    listeners,
    transform,
    transition,
    isDragging,
  } = useSortable({ id: `sortable-folder:${folderId}` })
  const { setNodeRef: setDropRef } = useDroppable({ id: `folder:${folderId}` })

  const style = {
    transform: CSS.Transform.toString(transform),
    transition,
    opacity: isDragging ? 0.3 : 1,
  }

  const combinedRef = useCallback((node: HTMLDivElement | null) => {
    setSortableRef(node)
    setDropRef(node)
  }, [setSortableRef, setDropRef])

  return (
    <div
      ref={combinedRef}
      style={style}
      className={`rounded-xl border overflow-hidden transition-colors duration-150 ${
        isDropOver && !isDragging
          ? 'bg-blue-500/10 border-blue-500/40 ring-2 ring-blue-500/30'
          : 'bg-dark-900/50 border-dark-800/50'
      }`}
    >
      {children({ ref: setActivatorNodeRef, listeners, attributes })}
    </div>
  )
}

/* ------------------------------------------------------------------ */
/*  Unfolder drop zone                                                 */
/* ------------------------------------------------------------------ */

function UnfolderDropZone({ isOver, hasServers, hasFolders, children }: {
  isOver: boolean
  hasServers: boolean
  hasFolders: boolean
  children: React.ReactNode
}) {
  const { setNodeRef } = useDroppable({ id: 'drop:unfolder' })

  if (!hasServers && !hasFolders) return <>{children}</>

  return (
    <div
      ref={setNodeRef}
      className={`rounded-xl transition-all duration-150 min-h-[40px] ${
        isOver
          ? 'bg-accent-500/5 ring-2 ring-accent-500/30 p-3'
          : ''
      }`}
    >
      {children}
    </div>
  )
}

/* ------------------------------------------------------------------ */
/*  Modals                                                             */
/* ------------------------------------------------------------------ */

function ModalOverlay({ children, onClose }: { children: React.ReactNode; onClose: () => void }) {
  const mouseDownTarget = useRef<EventTarget | null>(null)
  return (
    <AnimatePresence>
      <motion.div
        className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm p-4"
        initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
        onMouseDown={e => { mouseDownTarget.current = e.target }}
        onClick={e => { if (e.target === e.currentTarget && mouseDownTarget.current === e.currentTarget) onClose() }}
      >
        <motion.div
          initial={{ opacity: 0, scale: 0.95, y: 20 }}
          animate={{ opacity: 1, scale: 1, y: 0 }}
          exit={{ opacity: 0, scale: 0.95, y: 20 }}
          transition={{ duration: 0.2 }}
          className="bg-dark-900 border border-dark-800 rounded-2xl shadow-2xl w-full max-w-md"
          onClick={e => e.stopPropagation()}
        >
          {children}
        </motion.div>
      </motion.div>
    </AnimatePresence>
  )
}

function FolderModal({ t, title, initialValue, existingFolders, onClose, onSubmit }: {
  t: (k: string) => string
  title: string
  initialValue: string
  existingFolders: string[]
  onClose: () => void
  onSubmit: (name: string) => void
}) {
  const [name, setName] = useState(initialValue)
  const trimmed = name.trim()
  const duplicate = trimmed !== initialValue && existingFolders.includes(trimmed)

  const handleSubmit = () => {
    if (!trimmed || duplicate) return
    onSubmit(trimmed)
  }

  return (
    <ModalOverlay onClose={onClose}>
      <div className="p-6">
        <div className="flex items-center justify-between mb-5">
          <h2 className="text-lg font-semibold text-white">{title}</h2>
          <button onClick={onClose} className="text-dark-500 hover:text-dark-300 transition"><X className="w-5 h-5" /></button>
        </div>
        <div className="space-y-1.5">
          <label className="text-sm text-dark-300">{t('dashboard.folder_name')}</label>
          <input
            value={name} onChange={e => setName(e.target.value)}
            placeholder={t('dashboard.folder_name_placeholder')}
            className="w-full bg-dark-800 border border-dark-700 rounded-lg px-3 py-2 text-sm text-dark-200 placeholder-dark-600 focus:border-accent-500/50 focus:outline-none transition"
            autoFocus onKeyDown={e => { if (e.key === 'Enter') handleSubmit() }}
          />
        </div>
        {duplicate && <p className="text-xs text-red-400 mt-2">{trimmed} — already exists</p>}
        <div className="flex gap-3 mt-6">
          <button onClick={onClose} className="flex-1 py-2.5 bg-dark-800 text-dark-300 rounded-xl text-sm font-medium hover:bg-dark-700 transition">{t('common.cancel')}</button>
          <button onClick={handleSubmit} disabled={!trimmed || duplicate} className="flex-1 py-2.5 bg-accent-500 text-white rounded-xl text-sm font-medium hover:bg-accent-600 transition disabled:opacity-40 disabled:cursor-not-allowed">{t('common.create')}</button>
        </div>
      </div>
    </ModalOverlay>
  )
}

function RenameFolderModal({ t, folderName, onClose, onRenamed }: {
  t: (k: string) => string
  folderName: string
  onClose: () => void
  onRenamed: (oldName: string, newName: string) => void
}) {
  const [name, setName] = useState(folderName)
  const [saving, setSaving] = useState(false)

  const submit = async () => {
    const trimmed = name.trim()
    if (!trimmed || trimmed === folderName) return
    setSaving(true)
    try { onRenamed(folderName, trimmed) } finally { setSaving(false) }
  }

  return (
    <ModalOverlay onClose={onClose}>
      <div className="p-6">
        <div className="flex items-center justify-between mb-5">
          <h2 className="text-lg font-semibold text-white">{t('dashboard.rename_folder')}</h2>
          <button onClick={onClose} className="text-dark-500 hover:text-dark-300 transition"><X className="w-5 h-5" /></button>
        </div>
        <div className="space-y-1.5">
          <label className="text-sm text-dark-300">{t('dashboard.folder_name')}</label>
          <input
            value={name} onChange={e => setName(e.target.value)}
            className="w-full bg-dark-800 border border-dark-700 rounded-lg px-3 py-2 text-sm text-dark-200 placeholder-dark-600 focus:border-accent-500/50 focus:outline-none transition"
            autoFocus onKeyDown={e => { if (e.key === 'Enter') submit() }}
          />
        </div>
        <div className="flex gap-3 mt-6">
          <button onClick={onClose} className="flex-1 py-2.5 bg-dark-800 text-dark-300 rounded-xl text-sm font-medium hover:bg-dark-700 transition">{t('common.cancel')}</button>
          <button onClick={submit} disabled={!name.trim() || name.trim() === folderName || saving} className="flex-1 py-2.5 bg-accent-500 text-white rounded-xl text-sm font-medium hover:bg-accent-600 transition disabled:opacity-40 flex items-center justify-center gap-2">
            {saving && <Loader2 className="w-4 h-4 animate-spin" />}
            {t('common.save')}
          </button>
        </div>
      </div>
    </ModalOverlay>
  )
}
