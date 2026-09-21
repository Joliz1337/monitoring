import { useCallback, useState } from 'react'
import { useDroppable } from '@dnd-kit/core'
import { useSortable } from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'
import { Box, Folder, Loader2, X } from 'lucide-react'
import { toast } from 'sonner'
import { billingApi, BillingServerData } from '../../api/client'
import { Field, INPUT_CLASS, Overlay, Translate } from './shared'

export function CreateFolderModal({ t, existingFolders, onClose, onCreated }: {
  t: Translate
  existingFolders: string[]
  onClose: () => void
  onCreated: (name: string) => void
}) {
  const [name, setName] = useState('')
  const trimmed = name.trim()
  const duplicate = existingFolders.includes(trimmed)

  const handleCreate = () => {
    if (!trimmed || duplicate) return
    onCreated(trimmed)
    toast.success(t('billing.folder_created'))
  }

  return (
    <Overlay onClose={onClose}>
      <div className="p-6">
        <div className="flex items-center justify-between mb-5">
          <h2 className="text-lg font-semibold text-white">{t('billing.create_folder')}</h2>
          <button onClick={onClose} className="text-dark-500 hover:text-dark-300 transition">
            <X className="w-5 h-5" />
          </button>
        </div>
        <Field label={t('billing.folder_name')}>
          <input
            value={name}
            onChange={e => setName(e.target.value)}
            placeholder={t('billing.folder_name_placeholder')}
            className={INPUT_CLASS}
            autoFocus
            onKeyDown={e => { if (e.key === 'Enter') handleCreate() }}
          />
        </Field>
        {duplicate && (
          <p className="text-xs text-red-400 mt-2">{t('billing.folder_exists')}</p>
        )}
        <div className="flex gap-3 mt-6">
          <button onClick={onClose} className="flex-1 py-2.5 bg-dark-800 text-dark-300 rounded-xl text-sm font-medium hover:bg-dark-700 transition">
            {t('common.cancel')}
          </button>
          <button
            onClick={handleCreate}
            disabled={!trimmed || duplicate}
            className="flex-1 py-2.5 bg-accent-500 text-white rounded-xl text-sm font-medium hover:bg-accent-600 transition
                       disabled:opacity-40 disabled:cursor-not-allowed"
          >
            {t('common.create')}
          </button>
        </div>
      </div>
    </Overlay>
  )
}

export function RenameFolderModal({ t, folderName, onClose, onRenamed }: {
  t: Translate
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
    try {
      await billingApi.renameFolder(folderName, trimmed)
      onRenamed(folderName, trimmed)
      toast.success(t('billing.folder_renamed'))
    } catch {
      toast.error(t('common.action_failed'))
    } finally {
      setSaving(false)
    }
  }

  return (
    <Overlay onClose={onClose}>
      <div className="p-6">
        <div className="flex items-center justify-between mb-5">
          <h2 className="text-lg font-semibold text-white">{t('billing.rename_folder')}</h2>
          <button onClick={onClose} className="text-dark-500 hover:text-dark-300 transition">
            <X className="w-5 h-5" />
          </button>
        </div>
        <Field label={t('billing.folder_name')}>
          <input
            value={name}
            onChange={e => setName(e.target.value)}
            className={INPUT_CLASS}
            autoFocus
            onKeyDown={e => { if (e.key === 'Enter') submit() }}
          />
        </Field>
        <div className="flex gap-3 mt-6">
          <button onClick={onClose} className="flex-1 py-2.5 bg-dark-800 text-dark-300 rounded-xl text-sm font-medium hover:bg-dark-700 transition">
            {t('common.cancel')}
          </button>
          <button
            onClick={submit}
            disabled={!name.trim() || name.trim() === folderName || saving}
            className="flex-1 py-2.5 bg-accent-500 text-white rounded-xl text-sm font-medium hover:bg-accent-600 transition
                       disabled:opacity-40 flex items-center justify-center gap-2"
          >
            {saving && <Loader2 className="w-4 h-4 animate-spin" />}
            {t('common.save')}
          </button>
        </div>
      </div>
    </Overlay>
  )
}

export function MoveToFolderModal({ t, server, folders, onClose, onMoved }: {
  t: Translate
  server: BillingServerData
  folders: string[]
  onClose: () => void
  onMoved: (serverId: number, folder: string | null) => void
}) {
  const [selected, setSelected] = useState(server.folder || '')

  return (
    <Overlay onClose={onClose}>
      <div className="p-6">
        <div className="flex items-center justify-between mb-5">
          <h2 className="text-lg font-semibold text-white">{t('billing.move_to_folder')}</h2>
          <button onClick={onClose} className="text-dark-500 hover:text-dark-300 transition">
            <X className="w-5 h-5" />
          </button>
        </div>
        <p className="text-sm text-dark-400 mb-4">{server.name}</p>
        <div className="space-y-1.5">
          <button
            onClick={() => setSelected('')}
            className={`w-full flex items-center gap-2.5 px-3 py-2.5 rounded-lg text-sm transition ${
              selected === ''
                ? 'bg-accent-500/15 text-accent-400 border border-accent-500/30'
                : 'bg-dark-800/50 text-dark-300 border border-dark-700/50 hover:border-dark-600'
            }`}
          >
            <Box className="w-4 h-4" />
            {t('billing.no_folder')}
          </button>
          {folders.map(f => (
            <button
              key={f}
              onClick={() => setSelected(f)}
              className={`w-full flex items-center gap-2.5 px-3 py-2.5 rounded-lg text-sm transition ${
                selected === f
                  ? 'bg-accent-500/15 text-accent-400 border border-accent-500/30'
                  : 'bg-dark-800/50 text-dark-300 border border-dark-700/50 hover:border-dark-600'
              }`}
            >
              <Folder className="w-4 h-4" />
              {f}
            </button>
          ))}
        </div>
        <div className="flex gap-3 mt-6">
          <button onClick={onClose} className="flex-1 py-2.5 bg-dark-800 text-dark-300 rounded-xl text-sm font-medium hover:bg-dark-700 transition">
            {t('common.cancel')}
          </button>
          <button
            onClick={() => onMoved(server.id, selected || null)}
            className="flex-1 py-2.5 bg-accent-500 text-white rounded-xl text-sm font-medium hover:bg-accent-600 transition"
          >
            {t('common.save')}
          </button>
        </div>
      </div>
    </Overlay>
  )
}

export function SortableFolderItem({ folderId, isDropOver, children }: {
  folderId: string
  isDropOver: boolean
  children: (handleProps: {
    ref: (node: HTMLElement | null) => void
    listeners: ReturnType<typeof useSortable>['listeners']
    attributes: ReturnType<typeof useSortable>['attributes']
  }) => React.ReactNode
}) {
  const {
    setNodeRef: setSortableRef, setActivatorNodeRef, attributes, listeners, transform, transition, isDragging,
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

export function UnfolderDropZone({ isOver, hasServers, hasFolders, children }: {
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
        isOver ? 'bg-accent-500/5 ring-2 ring-accent-500/30 p-3' : ''
      }`}
    >
      {children}
    </div>
  )
}
