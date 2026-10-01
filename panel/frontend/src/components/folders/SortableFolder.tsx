import { useCallback, type ReactNode } from 'react'
import { useDroppable } from '@dnd-kit/core'
import { useSortable } from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'
import { AnimatePresence, motion } from 'framer-motion'
import { ChevronDown, ChevronRight, Folder, FolderOpen, GripVertical, Pencil, Trash2 } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Tooltip } from '../ui/Tooltip'
import { FOLDER_DROP_PREFIX, FOLDER_SORTABLE_PREFIX, UNFOLDER_DROP_ID } from '../../hooks/useFolderBoard'

interface SortableFolderProps {
  name: string
  collapsed: boolean
  isDropTarget: boolean
  onToggle: () => void
  onRename: () => void
  onDelete: () => void
  /** Бейджи сразу за именем, внутри кнопки сворачивания — видны и у свёрнутой папки */
  badges?: ReactNode
  /** Отдельная строка под заголовком — для того, что на узком экране не влезает рядом с именем */
  footer?: ReactNode
  children: ReactNode
}

/** Папка, которую можно перетаскивать за ручку и в которую можно бросить сервер */
export function SortableFolder({
  name, collapsed, isDropTarget, onToggle, onRename, onDelete, badges, footer, children,
}: SortableFolderProps) {
  const { t } = useTranslation()
  const {
    setNodeRef: setSortableRef,
    setActivatorNodeRef,
    attributes,
    listeners,
    transform,
    transition,
    isDragging,
  } = useSortable({ id: `${FOLDER_SORTABLE_PREFIX}${name}` })
  const { setNodeRef: setDropRef } = useDroppable({ id: `${FOLDER_DROP_PREFIX}${name}` })

  const combinedRef = useCallback((node: HTMLDivElement | null) => {
    setSortableRef(node)
    setDropRef(node)
  }, [setSortableRef, setDropRef])

  const style = {
    transform: CSS.Transform.toString(transform),
    transition,
    opacity: isDragging ? 0.3 : 1,
  }

  return (
    <div
      ref={combinedRef}
      style={style}
      className={`rounded-xl border overflow-hidden transition-colors duration-150 ${
        isDropTarget && !isDragging
          ? 'bg-blue-500/10 border-blue-500/40 ring-2 ring-blue-500/30'
          : 'bg-dark-900/50 border-dark-800/50'
      }`}
    >
      <div className="flex flex-wrap items-center gap-y-2 px-4 py-3">
        <div className="flex items-center gap-1 flex-1 min-w-0">
          <div
            ref={setActivatorNodeRef}
            {...listeners}
            {...attributes}
            className="p-1 text-dark-600 hover:text-dark-400 cursor-grab active:cursor-grabbing transition rounded flex-shrink-0"
          >
            <GripVertical className="w-4 h-4" />
          </div>
          <button onClick={onToggle} className="flex items-center gap-2.5 flex-1 min-w-0 group">
            <div className="w-8 h-8 rounded-lg bg-blue-500/15 flex items-center justify-center flex-shrink-0">
              {collapsed ? <Folder className="w-4 h-4 text-blue-400" /> : <FolderOpen className="w-4 h-4 text-blue-400" />}
            </div>
            <span className="text-sm font-semibold text-white truncate group-hover:text-blue-300 transition">{name}</span>
            {badges}
            {collapsed ? <ChevronRight className="w-3.5 h-3.5 text-dark-600 flex-shrink-0" /> : <ChevronDown className="w-3.5 h-3.5 text-dark-600 flex-shrink-0" />}
          </button>
        </div>
        <div className="flex items-center gap-1 flex-shrink-0 ml-2">
          <Tooltip label={t('common.edit')}>
            <button onClick={onRename} className="p-1.5 text-dark-500 hover:text-dark-300 transition rounded-lg hover:bg-dark-800/50">
              <Pencil className="w-3.5 h-3.5" />
            </button>
          </Tooltip>
          <Tooltip label={t('common.delete')}>
            <button onClick={onDelete} className="p-1.5 text-dark-500 hover:text-red-400 transition rounded-lg hover:bg-dark-800/50">
              <Trash2 className="w-3.5 h-3.5" />
            </button>
          </Tooltip>
        </div>
        {footer}
      </div>
      <AnimatePresence initial={false}>
        {!collapsed && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.2 }}
            className="overflow-hidden"
          >
            <div className="px-3 pb-3">{children}</div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}

/** Зона серверов без папки: сюда бросают сервер, чтобы вынуть его из папки */
export function UnfolderDropZone({ isOver, hasServers, hasFolders, children }: {
  isOver: boolean
  hasServers: boolean
  hasFolders: boolean
  children: ReactNode
}) {
  const { setNodeRef } = useDroppable({ id: UNFOLDER_DROP_ID })

  if (!hasServers && !hasFolders) return <>{children}</>

  return (
    <div
      ref={setNodeRef}
      className={`rounded-xl transition-colors duration-150 min-h-[40px] ${
        isOver
          ? 'bg-accent-500/5 ring-2 ring-accent-500/30'
          : ''
      }`}
    >
      {children}
    </div>
  )
}

/** Заголовок папки, который едет за курсором в DragOverlay */
export function FolderDragPreview({ name, count }: { name: string; count: number }) {
  return (
    <div className="opacity-90 bg-dark-900 border border-blue-500/40 rounded-xl px-4 py-3 flex items-center gap-2.5 shadow-2xl">
      <GripVertical className="w-4 h-4 text-dark-500" />
      <div className="w-8 h-8 rounded-lg bg-blue-500/15 flex items-center justify-center">
        <Folder className="w-4 h-4 text-blue-400" />
      </div>
      <span className="text-sm font-semibold text-white">{name}</span>
      <span className="text-xs text-dark-500">{count}</span>
    </div>
  )
}
