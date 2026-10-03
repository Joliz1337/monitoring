import { useCallback, useRef, useState } from 'react'
import {
  closestCenter,
  pointerWithin,
  KeyboardSensor,
  PointerSensor,
  TouchSensor,
  useSensor,
  useSensors,
  type CollisionDetection,
  type DragEndEvent,
  type DragOverEvent,
  type DragStartEvent,
  type SensorDescriptor,
} from '@dnd-kit/core'
import { arrayMove, sortableKeyboardCoordinates } from '@dnd-kit/sortable'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import { useServersStore, type ServerWithMetrics } from '../stores/serversStore'
import { readFolderOrder, saveFolderOrder } from '../utils/folders'

export const FOLDER_SORTABLE_PREFIX = 'sortable-folder:'
export const FOLDER_DROP_PREFIX = 'folder:'
export const UNFOLDER_DROP_ID = 'drop:unfolder'
const UNFOLDER_OVER_ID = '__unfolder__'

// Без сенсоров drag не стартует: при поиске или фильтре список отфильтрован,
// и сохранение порядка отправило бы на бэк неполный набор id
const NO_SENSORS: SensorDescriptor<object>[] = []

type FolderDialog =
  | { kind: 'none' }
  | { kind: 'create' }
  | { kind: 'rename'; folderName: string }

/**
 * Раскладка серверов по папкам перетаскиванием (dnd-kit, multi-container) и управление
 * папками. Общая для дашборда и страницы «Серверы»: страница рисует карточки и папки,
 * а порядок, смену папки и сохранение берёт отсюда.
 */
export function useFolderBoard(servers: ServerWithMetrics[]) {
  const { applyServerArrangement, renameFolder: renameStoreFolder, deleteFolder: deleteStoreFolder } = useServersStore()
  const { t } = useTranslation()

  const [dragType, setDragType] = useState<'server' | 'folder' | null>(null)
  const [activeId, setActiveId] = useState<string | number | null>(null)
  const [overFolderId, setOverFolderId] = useState<string | null>(null)
  // Локальная копия списка на время drag: onDragOver двигает сервер между папками в ней,
  // чтобы dnd-kit раздвигал карточки и показывал слот вставки; стор не трогаем до drop
  const [dragServers, setDragServers] = useState<ServerWithMetrics[] | null>(null)
  const dragServersRef = useRef<ServerWithMetrics[] | null>(null)
  const isDraggingRef = useRef(false)
  const [emptyFolders, setEmptyFolders] = useState<string[]>([])
  const [folderOrder, setFolderOrder] = useState<string[]>(readFolderOrder)
  const [dialog, setDialog] = useState<FolderDialog>({ kind: 'none' })

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 8 } }),
    useSensor(TouchSensor, { activationConstraint: { delay: 200, tolerance: 8 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates })
  )

  const collisionDetection: CollisionDetection = useCallback((args) => {
    if (dragType === 'folder') {
      return closestCenter({
        ...args,
        droppableContainers: args.droppableContainers.filter(c =>
          String(c.id).startsWith(FOLDER_SORTABLE_PREFIX)
        ),
      })
    }

    const containers = args.droppableContainers.filter(c =>
      !String(c.id).startsWith(FOLDER_SORTABLE_PREFIX)
    )
    const hits = pointerWithin({ ...args, droppableContainers: containers })

    // Карточка под курсором точнее зоны папки: даёт конкретный слот вставки
    const cardHits = hits.filter(h => typeof h.id === 'number' && h.id !== args.active.id)
    if (cardHits.length > 0) return cardHits

    const zoneHits = hits.filter(h => {
      const idStr = String(h.id)
      return idStr.startsWith(FOLDER_DROP_PREFIX) || idStr === UNFOLDER_DROP_ID
    })
    if (zoneHits.length > 0) {
      // Курсор в зоне, но между карточками (grid gap): целимся в ближайшую карточку
      // этой зоны, иначе overIndex сбрасывается и превью раздвижки дёргается на каждом зазоре
      const zoneId = String(zoneHits[0].id)
      const zoneFolder = zoneId === UNFOLDER_DROP_ID ? null : zoneId.replace(FOLDER_DROP_PREFIX, '')
      const list = dragServersRef.current
      if (list) {
        const folderOf = new Map(list.map(s => [s.id, s.folder || null]))
        const zoneCards = containers.filter(c =>
          typeof c.id === 'number' && c.id !== args.active.id && folderOf.get(c.id) === zoneFolder
        )
        if (zoneCards.length > 0) {
          const closest = closestCenter({ ...args, droppableContainers: zoneCards })
          if (closest.length > 0) return closest
        }
      }
      return zoneHits
    }

    return closestCenter({
      ...args,
      droppableContainers: containers.filter(c => c.id !== args.active.id),
    })
  }, [dragType])

  const handleDragStart = (event: DragStartEvent, folders: string[]) => {
    isDraggingRef.current = true
    const id = String(event.active.id)
    if (id.startsWith(FOLDER_SORTABLE_PREFIX)) {
      setDragType('folder')
      setActiveId(id)
      return
    }
    setDragType('server')
    setActiveId(event.active.id as number)
    dragServersRef.current = servers
    setDragServers(servers)
    // Фиксируем все текущие папки как «существующие»: если из папки утащат
    // последний сервер, она не должна исчезнуть из-под курсора посреди drag
    setEmptyFolders(prev => Array.from(new Set([...prev, ...folders])))
  }

  const handleDragOver = (event: DragOverEvent) => {
    if (dragType !== 'server') return
    const { active, over } = event
    if (!over) {
      setOverFolderId(null)
      return
    }

    const list = dragServersRef.current
    const overStr = String(over.id)
    let targetFolder: string | null
    let overServerId: number | null = null

    if (typeof over.id === 'number') {
      overServerId = over.id
      targetFolder = (list ?? servers).find(s => s.id === over.id)?.folder || null
      setOverFolderId(null)
    } else if (overStr.startsWith(FOLDER_DROP_PREFIX)) {
      targetFolder = overStr.replace(FOLDER_DROP_PREFIX, '')
      setOverFolderId(targetFolder)
    } else if (overStr === UNFOLDER_DROP_ID) {
      targetFolder = null
      setOverFolderId(UNFOLDER_OVER_ID)
    } else {
      setOverFolderId(null)
      return
    }

    if (!list) return
    const draggedId = active.id as number
    const dragged = list.find(s => s.id === draggedId)
    if (!dragged || (dragged.folder || null) === targetFolder) return

    // Смена контейнера прямо во время drag: локально переносим сервер в целевую
    // папку, чтобы её SortableContext включил его и показал слот вставки
    const without = list.filter(s => s.id !== draggedId)
    let insertIdx: number
    if (overServerId != null) {
      const overIdx = without.findIndex(s => s.id === overServerId)
      const activeRect = active.rect.current.translated
      const isBelow = activeRect !== null && activeRect.top > over.rect.top + over.rect.height
      insertIdx = overIdx === -1 ? without.length : overIdx + (isBelow ? 1 : 0)
    } else {
      // Наведение на зону папки (не на карточку) — в конец её блока
      let lastIdx = -1
      for (let i = 0; i < without.length; i++) {
        if ((without[i].folder || null) === targetFolder) lastIdx = i
      }
      insertIdx = lastIdx === -1 ? without.length : lastIdx + 1
    }

    const next = [
      ...without.slice(0, insertIdx),
      { ...dragged, folder: targetFolder },
      ...without.slice(insertIdx),
    ]
    dragServersRef.current = next
    setDragServers(next)
  }

  const clearDragVisuals = () => {
    dragServersRef.current = null
    setDragType(null)
    setActiveId(null)
    setOverFolderId(null)
    setDragServers(null)
  }

  const handleDragCancel = () => {
    clearDragVisuals()
    isDraggingRef.current = false
  }

  const handleDragEnd = async (event: DragEndEvent, folders: string[]) => {
    const { active, over } = event
    const prevDragType = dragType
    const localList = dragServersRef.current
    clearDragVisuals()

    // isDraggingRef держим до конца сохранения: тик поллинга в окне между дропом
    // и коммитом на бэке принёс бы старый порядок и откатил карточки скачком
    try {
      if (!over) return

      const activeStr = String(active.id)
      const overStr = String(over.id)

      if (prevDragType === 'folder' && activeStr.startsWith(FOLDER_SORTABLE_PREFIX) && overStr.startsWith(FOLDER_SORTABLE_PREFIX)) {
        const activeFolder = activeStr.replace(FOLDER_SORTABLE_PREFIX, '')
        const overFolder = overStr.replace(FOLDER_SORTABLE_PREFIX, '')
        if (activeFolder !== overFolder) {
          const oldIdx = folders.indexOf(activeFolder)
          const newIdx = folders.indexOf(overFolder)
          if (oldIdx !== -1 && newIdx !== -1) {
            const newOrder = arrayMove([...folders], oldIdx, newIdx)
            setFolderOrder(newOrder)
            saveFolderOrder(newOrder)
          }
        }
        return
      }

      if (prevDragType !== 'server' || !localList) return

      // Финальная позиция: смена папки уже применена в localList на dragOver,
      // остаётся зафиксировать перестановку внутри контейнера
      const draggedId = active.id as number
      let list = localList
      if (typeof over.id === 'number' && over.id !== draggedId) {
        const oldIndex = list.findIndex(s => s.id === draggedId)
        const newIndex = list.findIndex(s => s.id === over.id)
        if (oldIndex !== -1 && newIndex !== -1) list = arrayMove(list, oldIndex, newIndex)
      }

      const dragged = list.find(s => s.id === draggedId)
      if (!dragged) return
      const original = servers.find(s => s.id === draggedId)
      const folderChanged = (original?.folder || null) !== (dragged.folder || null)
      const orderChanged = list.length !== servers.length || list.some((s, i) => s.id !== servers[i].id)
      if (!folderChanged && !orderChanged) return

      try {
        await applyServerArrangement(list.map(s => s.id), draggedId, dragged.folder || null)
        if (folderChanged) toast.success(t('dashboard.server_moved'))
      } catch {
        toast.error(t('common.action_failed'))
      }
    } finally {
      isDraggingRef.current = false
    }
  }

  /** Пропсы для DndContext; folders — папки в том порядке, в каком они показаны */
  const dndContextProps = (folders: string[], dragEnabled: boolean) => ({
    sensors: dragEnabled ? sensors : NO_SENSORS,
    collisionDetection,
    onDragStart: (event: DragStartEvent) => handleDragStart(event, folders),
    onDragOver: handleDragOver,
    onDragEnd: (event: DragEndEvent) => handleDragEnd(event, folders),
    onDragCancel: handleDragCancel,
  })

  const isDropTarget = (folder: string | null) =>
    dragType === 'server' && overFolderId === (folder ?? UNFOLDER_OVER_ID)

  const closeDialog = () => setDialog({ kind: 'none' })

  const createFolder = (name: string) => {
    setEmptyFolders(prev => (prev.includes(name) ? prev : [...prev, name]))
    closeDialog()
    toast.success(t('dashboard.folder_created'))
  }

  const renameFolder = async (oldName: string, newName: string) => {
    try {
      await renameStoreFolder(oldName, newName)
    } catch {
      toast.error(t('common.action_failed'))
      return
    }
    setEmptyFolders(prev => prev.map(f => (f === oldName ? newName : f)))
    setFolderOrder(prev => {
      const next = prev.map(f => (f === oldName ? newName : f))
      saveFolderOrder(next)
      return next
    })
    closeDialog()
    toast.success(t('dashboard.folder_renamed'))
  }

  const deleteFolder = async (folderName: string) => {
    if (!confirm(t('dashboard.confirm_delete_folder'))) return
    try {
      await deleteStoreFolder(folderName)
      setEmptyFolders(prev => prev.filter(f => f !== folderName))
      setFolderOrder(prev => {
        const next = prev.filter(f => f !== folderName)
        saveFolderOrder(next)
        return next
      })
      toast.success(t('dashboard.folder_deleted'))
    } catch {
      toast.error(t('common.action_failed'))
    }
  }

  return {
    /** Список для отрисовки: во время drag — локальная копия с уже перенесённым сервером */
    displayedServers: dragServers ?? servers,
    emptyFolders,
    folderOrder,
    isDraggingRef,
    activeServerId: dragType === 'server' && typeof activeId === 'number' ? activeId : null,
    activeFolderName: dragType === 'folder' && typeof activeId === 'string'
      ? activeId.replace(FOLDER_SORTABLE_PREFIX, '')
      : null,
    dndContextProps,
    isDropTarget,
    dialog,
    openCreateFolder: () => setDialog({ kind: 'create' }),
    openRenameFolder: (folderName: string) => setDialog({ kind: 'rename', folderName }),
    closeDialog,
    createFolder,
    renameFolder,
    deleteFolder,
  }
}

export type FolderBoard = ReturnType<typeof useFolderBoard>
