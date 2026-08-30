import { useEffect, useState, useCallback, useMemo } from 'react'
import { useTranslation } from 'react-i18next'
import { motion, AnimatePresence } from 'framer-motion'
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
  type CollisionDetection,
} from '@dnd-kit/core'
import {
  arrayMove,
  SortableContext,
  sortableKeyboardCoordinates,
  rectSortingStrategy,
  verticalListSortingStrategy,
} from '@dnd-kit/sortable'
import {
  CreditCard, Plus, Pencil, Trash2, Bell, ChevronDown, ChevronRight,
  Box, FolderPlus, Folder, FolderOpen, GripVertical, RefreshCw, Loader2,
} from 'lucide-react'
import { toast } from 'sonner'
import { billingApi, BillingServerData, BillingSettingsData } from '../api/client'
import { Tooltip } from '../components/ui/Tooltip'
import { FAQIcon } from '../components/FAQ'
import { BillingSummary } from '../components/billing/BillingSummary'
import { ProjectCard } from '../components/billing/ProjectCard'
import {
  AddModal, CloudPlanModal, EditModal, ExtendModal, TopupModal,
} from '../components/billing/ServerModals'
import {
  CreateFolderModal, MoveToFolderModal, RenameFolderModal,
  SortableFolderItem, UnfolderDropZone,
} from '../components/billing/FolderModals'
import {
  ToggleRow, formatDays, sortServers, statusColor, useBillingDateFormat,
} from '../components/billing/shared'

type ModalState =
  | { kind: 'none' }
  | { kind: 'add' }
  | { kind: 'edit'; server: BillingServerData }
  | { kind: 'extend'; server: BillingServerData }
  | { kind: 'topup'; server: BillingServerData }
  | { kind: 'plan'; server: BillingServerData }
  | { kind: 'create-folder' }
  | { kind: 'rename-folder'; folderName: string }
  | { kind: 'move-to-folder'; server: BillingServerData }

const COLLAPSED_KEY = 'billing_collapsed_folders'
const FOLDER_ORDER_KEY = 'billing_folder_order'
const SERVER_ORDER_KEY = 'billing_server_order'
const NOTIFY_DAY_OPTIONS = [1, 3, 7, 14, 30]
const CHECK_INTERVAL_OPTIONS = [30, 60, 120, 360, 720]

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

function loadServerOrder(): number[] {
  try {
    return JSON.parse(localStorage.getItem(SERVER_ORDER_KEY) || '[]')
  } catch { return [] }
}

function saveServerOrder(order: number[]) {
  localStorage.setItem(SERVER_ORDER_KEY, JSON.stringify(order))
}

export default function Billing() {
  const { t } = useTranslation()
  const { formatDateTime: formatBillingDateTime } = useBillingDateFormat()

  const [servers, setServers] = useState<BillingServerData[]>([])
  const [settings, setSettings] = useState<BillingSettingsData | null>(null)
  const [loading, setLoading] = useState(true)
  const [modal, setModal] = useState<ModalState>({ kind: 'none' })
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [collapsed, setCollapsed] = useState<Set<string>>(loadCollapsed)
  const [emptyFolders, setEmptyFolders] = useState<string[]>([])
  const [folderOrder, setFolderOrder] = useState<string[]>(loadFolderOrder)
  const [serverOrder, setServerOrder] = useState<number[]>(loadServerOrder)
  const [dragType, setDragType] = useState<'server' | 'folder' | null>(null)
  const [activeId, setActiveId] = useState<string | number | null>(null)
  const [overFolderId, setOverFolderId] = useState<string | null>(null)
  const [syncingIds, setSyncingIds] = useState<Set<number>>(new Set())

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 8 } }),
    useSensor(TouchSensor, { activationConstraint: { delay: 200, tolerance: 8 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates })
  )

  const folders = useMemo(() => {
    const allFolders = new Set<string>()
    for (const s of servers) if (s.folder) allFolders.add(s.folder)
    for (const f of emptyFolders) allFolders.add(f)
    const ordered = folderOrder.filter(f => allFolders.has(f))
    const remaining = [...allFolders].filter(f => !folderOrder.includes(f)).sort()
    return [...ordered, ...remaining]
  }, [servers, emptyFolders, folderOrder])

  const folderSortableIds = useMemo(
    () => folders.map(f => `sortable-folder:${f}`),
    [folders]
  )

  const grouped = useMemo(() => {
    const map = new Map<string | null, BillingServerData[]>()
    for (const s of servers) {
      const key = s.folder || null
      if (!map.has(key)) map.set(key, [])
      map.get(key)!.push(s)
    }
    const orderSet = new Set(serverOrder)
    for (const arr of map.values()) {
      arr.sort((a, b) => {
        const ai = orderSet.has(a.id) ? serverOrder.indexOf(a.id) : Infinity
        const bi = orderSet.has(b.id) ? serverOrder.indexOf(b.id) : Infinity
        if (ai !== Infinity || bi !== Infinity) return ai - bi
        return sortServers(a, b)
      })
    }
    return map
  }, [servers, serverOrder])

  const serverFolderMap = useMemo(() => {
    const map = new Map<number, string | null>()
    for (const s of servers) map.set(s.id, s.folder || null)
    return map
  }, [servers])

  const cloudServers = useMemo(
    () => servers.filter(s => s.billing_type === 'cloud'),
    [servers]
  )

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

    if (prevDragType === 'server') {
      const draggedId = active.id as number

      if (overStr.startsWith('folder:')) {
        const targetFolder = overStr.replace('folder:', '')
        const srv = servers.find(s => s.id === draggedId)
        if (srv && srv.folder !== targetFolder) {
          try {
            await billingApi.moveToFolder([draggedId], targetFolder)
            setServers(prev => prev.map(s => s.id === draggedId ? { ...s, folder: targetFolder } : s))
            toast.success(t('billing.items_moved'))
          } catch { toast.error(t('common.action_failed')) }
        }
        return
      }

      if (overStr === 'drop:unfolder') {
        const srv = servers.find(s => s.id === draggedId)
        if (srv && srv.folder) {
          try {
            await billingApi.moveToFolder([draggedId], null)
            setServers(prev => prev.map(s => s.id === draggedId ? { ...s, folder: null } : s))
            toast.success(t('billing.items_moved'))
          } catch { toast.error(t('common.action_failed')) }
        }
        return
      }

      if (typeof over.id === 'number' && active.id !== over.id) {
        const draggedFolder = serverFolderMap.get(draggedId) ?? null
        const overFolder = serverFolderMap.get(over.id as number) ?? null
        if (draggedFolder === overFolder) {
          const folderServers = grouped.get(draggedFolder) || []
          const ids = folderServers.map(s => s.id)
          const oldIdx = ids.indexOf(draggedId)
          const newIdx = ids.indexOf(over.id as number)
          if (oldIdx !== -1 && newIdx !== -1) {
            const reordered = arrayMove(ids, oldIdx, newIdx)
            const otherIds = serverOrder.filter(id => !reordered.includes(id))
            const newOrder = [...reordered, ...otherIds]
            setServerOrder(newOrder)
            saveServerOrder(newOrder)
          }
        }
      }
    }
  }

  const fetchAll = useCallback(async () => {
    try {
      const [srvRes, setRes] = await Promise.all([
        billingApi.getServers(),
        billingApi.getSettings(),
      ])
      setServers(srvRes.data.servers)
      setSettings(setRes.data)
    } catch {
      toast.error(t('common.error'))
    } finally {
      setLoading(false)
    }
  }, [t])

  useEffect(() => { fetchAll() }, [fetchAll])

  const handleDelete = async (id: number) => {
    if (!confirm(t('billing.confirm_delete'))) return
    try {
      await billingApi.deleteServer(id)
      setServers(prev => prev.filter(s => s.id !== id))
      toast.success(t('common.deleted'))
    } catch {
      toast.error(t('common.action_failed'))
    }
  }

  const handleDeleteFolder = async (folderName: string) => {
    if (!confirm(t('billing.confirm_delete_folder'))) return
    try {
      await billingApi.deleteFolder(folderName)
      setServers(prev => prev.map(s => s.folder === folderName ? { ...s, folder: null } : s))
      setEmptyFolders(prev => prev.filter(f => f !== folderName))
      setFolderOrder(prev => { const next = prev.filter(f => f !== folderName); saveFolderOrder(next); return next })
      toast.success(t('billing.folder_deleted'))
    } catch {
      toast.error(t('common.action_failed'))
    }
  }

  const handleMoveToFolder = async (serverId: number, folder: string | null) => {
    try {
      await billingApi.moveToFolder([serverId], folder)
      setServers(prev => prev.map(s => s.id === serverId ? { ...s, folder } : s))
      toast.success(t('billing.items_moved'))
    } catch {
      toast.error(t('common.action_failed'))
    }
  }

  const markSyncing = (id: number, active: boolean) => {
    setSyncingIds(prev => {
      const next = new Set(prev)
      if (active) next.add(id)
      else next.delete(id)
      return next
    })
  }

  const syncServer = async (id: number): Promise<boolean> => {
    markSyncing(id, true)
    try {
      const res = await billingApi.syncServer(id)
      setServers(prev => prev.map(s => s.id === id ? res.data : s))
      return true
    } catch (e: unknown) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      toast.error(detail || t('common.action_failed'))
      return false
    } finally {
      markSyncing(id, false)
    }
  }

  const handleSync = async (server: BillingServerData) => {
    if (await syncServer(server.id)) toast.success(t('billing.cloud_synced'))
  }

  // Последовательно, а не пачкой: у провайдеров лимиты на запросы, а обновление —
  // операция не срочная, зато каждая ошибка остаётся привязанной к своему аккаунту
  const handleSyncAll = async () => {
    let failed = 0
    for (const srv of cloudServers) {
      if (!await syncServer(srv.id)) failed++
    }
    if (failed === 0) toast.success(t('billing.cloud_synced'))
    else toast.error(t('billing.sync_all_failed', { count: failed }))
  }

  const handleSaveSettings = async (patch: Partial<BillingSettingsData>) => {
    try {
      const res = await billingApi.updateSettings(patch)
      setSettings(res.data)
      toast.success(t('common.saved'))
    } catch {
      toast.error(t('common.action_failed'))
    }
  }

  if (loading) {
    return (
      <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} className="space-y-6">
        <div className="space-y-2">
          <div className="h-7 w-48 bg-dark-700/50 rounded-lg animate-pulse" />
          <div className="h-4 w-64 bg-dark-700/30 rounded-lg animate-pulse" />
        </div>
        {Array.from({ length: 3 }).map((_, i) => (
          <div key={i} className="card p-5 space-y-3">
            <div className="flex items-center gap-3">
              <div className="w-10 h-10 bg-dark-700/50 rounded-xl animate-pulse" />
              <div className="space-y-2 flex-1">
                <div className="h-4 w-40 bg-dark-700/50 rounded animate-pulse" />
                <div className="h-3 w-56 bg-dark-700/30 rounded animate-pulse" />
              </div>
            </div>
          </div>
        ))}
      </motion.div>
    )
  }

  const renderServerCards = (list: BillingServerData[], sortable = false) => {
    const cards = list.map((srv, idx) => (
      <ProjectCard
        key={srv.id}
        server={srv}
        index={idx}
        t={t}
        formatDateTime={formatBillingDateTime}
        sortable={sortable}
        syncing={syncingIds.has(srv.id)}
        onExtend={() => setModal({ kind: 'extend', server: srv })}
        onTopup={() => setModal({ kind: 'topup', server: srv })}
        onEdit={() => setModal({ kind: 'edit', server: srv })}
        onDelete={() => handleDelete(srv.id)}
        onMoveToFolder={() => setModal({ kind: 'move-to-folder', server: srv })}
        onSync={() => handleSync(srv)}
        onPlan={() => setModal({ kind: 'plan', server: srv })}
      />
    ))

    if (sortable) {
      return (
        <SortableContext items={list.map(s => s.id)} strategy={rectSortingStrategy}>
          <div className="grid gap-3">{cards}</div>
        </SortableContext>
      )
    }

    return <div className="grid gap-3">{cards}</div>
  }

  const unfolderedServers = grouped.get(null) || []
  const syncingAll = cloudServers.some(s => syncingIds.has(s.id))

  const activeServer = dragType === 'server' && typeof activeId === 'number'
    ? servers.find(s => s.id === activeId) : null
  const activeFolderName = dragType === 'folder' && typeof activeId === 'string'
    ? activeId.replace('sortable-folder:', '') : null

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-emerald-500/20 to-teal-500/20 flex items-center justify-center">
            <CreditCard className="w-5 h-5 text-emerald-400" />
          </div>
          <div>
            <h1 className="text-xl font-semibold text-white flex items-center gap-2">
              {t('billing.title')}
              <FAQIcon screen="PAGE_BILLING" />
            </h1>
            <p className="text-sm text-dark-400">{t('billing.subtitle')}</p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          {cloudServers.length > 0 && (
            <button
              onClick={handleSyncAll}
              disabled={syncingAll}
              className="flex items-center gap-2 px-3 py-2 bg-dark-800 hover:bg-dark-700
                         text-dark-300 hover:text-white rounded-xl text-sm font-medium transition
                         border border-dark-700/50 disabled:opacity-60 disabled:cursor-not-allowed"
            >
              {syncingAll
                ? <Loader2 className="w-4 h-4 animate-spin" />
                : <RefreshCw className="w-4 h-4" />
              }
              {t('billing.sync_all')}
            </button>
          )}
          <button
            onClick={() => setModal({ kind: 'create-folder' })}
            className="flex items-center gap-2 px-3 py-2 bg-dark-800 hover:bg-dark-700
                       text-dark-300 hover:text-white rounded-xl text-sm font-medium transition border border-dark-700/50"
          >
            <FolderPlus className="w-4 h-4" />
            {t('billing.create_folder')}
          </button>
          <button
            onClick={() => setModal({ kind: 'add' })}
            className="flex items-center gap-2 px-4 py-2 bg-accent-500 hover:bg-accent-600
                       text-white rounded-xl text-sm font-medium transition"
          >
            <Plus className="w-4 h-4" />
            {t('billing.add')}
          </button>
        </div>
      </div>

      {servers.length > 0 && <BillingSummary servers={servers} t={t} />}

      {servers.length === 0 ? (
        <motion.div
          initial={{ opacity: 0, y: 12 }}
          animate={{ opacity: 1, y: 0 }}
          className="bg-dark-900/50 rounded-xl border border-dark-800/50 p-12 text-center"
        >
          <Box className="w-10 h-10 text-dark-600 mx-auto mb-3" />
          <p className="text-dark-400 text-sm">{t('billing.no_items')}</p>
        </motion.div>
      ) : (
        <DndContext
          sensors={sensors}
          collisionDetection={collisionDetection}
          onDragStart={handleDragStart}
          onDragOver={handleDragOver}
          onDragEnd={handleDragEnd}
        >
          <div className="space-y-4">
            <SortableContext items={folderSortableIds} strategy={verticalListSortingStrategy}>
              {folders.map(folderName => {
                const isCollapsed = collapsed.has(folderName)
                const folderServers = grouped.get(folderName) || []
                const daysArr = folderServers.map(s => s.days_left ?? 9999)
                const worstDays = daysArr.length > 0 ? Math.min(...daysArr) : 9999

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
                                {isCollapsed
                                  ? <Folder className="w-4 h-4 text-blue-400" />
                                  : <FolderOpen className="w-4 h-4 text-blue-400" />
                                }
                              </div>
                              <span className="text-sm font-semibold text-white truncate group-hover:text-blue-300 transition">
                                {folderName}
                              </span>
                              <span className="text-xs text-dark-500 flex-shrink-0">
                                {folderServers.length}
                              </span>
                              {isCollapsed && (
                                <span className={`text-xs font-medium flex-shrink-0 ${statusColor(worstDays)}`}>
                                  {formatDays(worstDays === 9999 ? null : worstDays, t)}
                                </span>
                              )}
                              {isCollapsed
                                ? <ChevronRight className="w-3.5 h-3.5 text-dark-600 flex-shrink-0" />
                                : <ChevronDown className="w-3.5 h-3.5 text-dark-600 flex-shrink-0" />
                              }
                            </button>
                          </div>
                          <div className="flex items-center gap-1 flex-shrink-0 ml-2">
                            <Tooltip label={t('common.edit')}>
                              <button
                                onClick={() => setModal({ kind: 'rename-folder', folderName })}
                                className="p-1.5 text-dark-500 hover:text-dark-300 transition rounded-lg hover:bg-dark-800/50"
                              >
                                <Pencil className="w-3.5 h-3.5" />
                              </button>
                            </Tooltip>
                            <Tooltip label={t('common.delete')}>
                              <button
                                onClick={() => handleDeleteFolder(folderName)}
                                className="p-1.5 text-dark-500 hover:text-red-400 transition rounded-lg hover:bg-dark-800/50"
                              >
                                <Trash2 className="w-3.5 h-3.5" />
                              </button>
                            </Tooltip>
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
                                {folderServers.length > 0
                                  ? renderServerCards(folderServers, true)
                                  : (
                                    <div className="py-6 text-center text-dark-500 text-xs">
                                      {t('billing.no_items')}
                                    </div>
                                  )
                                }
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

            <UnfolderDropZone
              isOver={overFolderId === '__unfolder__' && dragType === 'server'}
              hasServers={unfolderedServers.length > 0}
              hasFolders={folders.length > 0}
            >
              {renderServerCards(unfolderedServers, true)}
            </UnfolderDropZone>
          </div>

          <DragOverlay>
            {activeServer && (
              <div className="opacity-90">
                <ProjectCard
                  server={activeServer}
                  index={0}
                  t={t}
                  formatDateTime={formatBillingDateTime}
                  sortable={false}
                  onExtend={() => {}}
                  onTopup={() => {}}
                  onEdit={() => {}}
                  onDelete={() => {}}
                  onMoveToFolder={() => {}}
                  onSync={() => {}}
                  onPlan={() => {}}
                />
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

      {/* Notification settings */}
      <motion.div
        initial={{ opacity: 0, y: 12 }}
        animate={{ opacity: 1, y: 0 }}
        className="bg-dark-900/50 rounded-xl border border-dark-800/50 overflow-hidden"
      >
        <button
          onClick={() => setSettingsOpen(v => !v)}
          className="w-full flex items-center justify-between p-5 hover:bg-dark-800/30 transition"
        >
          <div className="flex items-center gap-3">
            <div className="w-8 h-8 rounded-lg bg-amber-500/20 flex items-center justify-center">
              <Bell className="w-4 h-4 text-amber-400" />
            </div>
            <div className="text-left">
              <span className="text-sm font-medium text-dark-200">{t('billing.notification_settings')}</span>
              <p className="text-xs text-dark-500">{t('billing.notification_hint')}</p>
            </div>
          </div>
          <ChevronDown className={`w-4 h-4 text-dark-500 transition-transform ${settingsOpen ? 'rotate-180' : ''}`} />
        </button>
        <AnimatePresence>
          {settingsOpen && settings && (
            <motion.div
              initial={{ height: 0, opacity: 0 }}
              animate={{ height: 'auto', opacity: 1 }}
              exit={{ height: 0, opacity: 0 }}
              transition={{ duration: 0.2 }}
              className="overflow-hidden"
            >
              <div className="px-5 pb-5 space-y-4">
                <ToggleRow
                  label={t('billing.enable_notifications')}
                  checked={settings.enabled}
                  onChange={v => handleSaveSettings({ enabled: v })}
                />
                <div className="space-y-2">
                  <span className="text-sm text-dark-300">{t('billing.notify_before_days')}</span>
                  <div className="flex flex-wrap gap-2">
                    {NOTIFY_DAY_OPTIONS.map(d => {
                      const active = settings.notify_days.includes(d)
                      return (
                        <button
                          key={d}
                          onClick={() => {
                            const next = active
                              ? settings.notify_days.filter(x => x !== d)
                              : [...settings.notify_days, d].sort((a, b) => a - b)
                            handleSaveSettings({ notify_days: next })
                          }}
                          className={`px-3 py-1.5 rounded-lg text-xs font-medium transition ${
                            active
                              ? 'bg-accent-500/20 text-accent-400 border border-accent-500/30'
                              : 'bg-dark-800 text-dark-400 border border-dark-700/50 hover:border-dark-600'
                          }`}
                        >
                          {d} {t('common.days')}
                        </button>
                      )
                    })}
                  </div>
                </div>
                <div className="space-y-1">
                  <span className="text-sm text-dark-300">{t('billing.check_interval')}</span>
                  <div className="flex flex-wrap gap-2">
                    {CHECK_INTERVAL_OPTIONS.map(m => {
                      const active = settings.check_interval_minutes === m
                      const label = m < 60 ? `${m}m` : `${m / 60}h`
                      return (
                        <button
                          key={m}
                          onClick={() => handleSaveSettings({ check_interval_minutes: m })}
                          className={`px-3 py-1.5 rounded-lg text-xs font-medium transition ${
                            active
                              ? 'bg-accent-500/20 text-accent-400 border border-accent-500/30'
                              : 'bg-dark-800 text-dark-400 border border-dark-700/50 hover:border-dark-600'
                          }`}
                        >
                          {label}
                        </button>
                      )
                    })}
                  </div>
                </div>
                <p className="text-xs text-dark-500">{t('billing.telegram_from_alerts')}</p>
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </motion.div>

      {/* Modals */}
      {modal.kind === 'add' && (
        <AddModal
          t={t}
          folders={folders}
          onClose={() => setModal({ kind: 'none' })}
          onCreated={srv => {
            setServers(prev => [...prev, srv].sort(sortServers))
            setModal({ kind: 'none' })
          }}
        />
      )}
      {modal.kind === 'edit' && (
        <EditModal
          t={t}
          server={modal.server}
          folders={folders}
          onClose={() => setModal({ kind: 'none' })}
          onSaved={srv => {
            setServers(prev => prev.map(s => s.id === srv.id ? srv : s).sort(sortServers))
            setModal({ kind: 'none' })
          }}
        />
      )}
      {modal.kind === 'extend' && (
        <ExtendModal
          t={t}
          server={modal.server}
          onClose={() => setModal({ kind: 'none' })}
          onDone={srv => {
            setServers(prev => prev.map(s => s.id === srv.id ? srv : s).sort(sortServers))
            setModal({ kind: 'none' })
          }}
        />
      )}
      {modal.kind === 'topup' && (
        <TopupModal
          t={t}
          server={modal.server}
          onClose={() => setModal({ kind: 'none' })}
          onDone={srv => {
            setServers(prev => prev.map(s => s.id === srv.id ? srv : s).sort(sortServers))
            setModal({ kind: 'none' })
          }}
        />
      )}
      {modal.kind === 'plan' && (
        <CloudPlanModal t={t} server={modal.server} onClose={() => setModal({ kind: 'none' })} />
      )}
      {modal.kind === 'create-folder' && (
        <CreateFolderModal
          t={t}
          existingFolders={folders}
          onClose={() => setModal({ kind: 'none' })}
          onCreated={(name) => {
            setEmptyFolders(prev => prev.includes(name) ? prev : [...prev, name])
            setModal({ kind: 'none' })
          }}
        />
      )}
      {modal.kind === 'rename-folder' && (
        <RenameFolderModal
          t={t}
          folderName={modal.folderName}
          onClose={() => setModal({ kind: 'none' })}
          onRenamed={(oldName, newName) => {
            setServers(prev => prev.map(s => s.folder === oldName ? { ...s, folder: newName } : s))
            setEmptyFolders(prev => prev.map(f => f === oldName ? newName : f))
            setFolderOrder(prev => { const next = prev.map(f => f === oldName ? newName : f); saveFolderOrder(next); return next })
            setModal({ kind: 'none' })
          }}
        />
      )}
      {modal.kind === 'move-to-folder' && (
        <MoveToFolderModal
          t={t}
          server={modal.server}
          folders={folders}
          onClose={() => setModal({ kind: 'none' })}
          onMoved={(serverId, folder) => {
            handleMoveToFolder(serverId, folder)
            setModal({ kind: 'none' })
          }}
        />
      )}
    </div>
  )
}
