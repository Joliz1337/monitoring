import { useRef, useState, type ReactNode } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { Loader2, X } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import type { FolderBoard } from '../../hooks/useFolderBoard'

/** Окна создания и переименования папки; existingFolders — папки, показанные на странице */
export function FolderDialogs({ board, existingFolders }: { board: FolderBoard; existingFolders: string[] }) {
  const { dialog } = board
  if (dialog.kind === 'create') {
    return <CreateFolderModal existingFolders={existingFolders} onClose={board.closeDialog} onSubmit={board.createFolder} />
  }
  if (dialog.kind === 'rename') {
    return <RenameFolderModal folderName={dialog.folderName} onClose={board.closeDialog} onRenamed={board.renameFolder} />
  }
  return null
}

function ModalOverlay({ children, onClose }: { children: ReactNode; onClose: () => void }) {
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

function CreateFolderModal({ existingFolders, onClose, onSubmit }: {
  existingFolders: string[]
  onClose: () => void
  onSubmit: (name: string) => void
}) {
  const { t } = useTranslation()
  const [name, setName] = useState('')
  const trimmed = name.trim()
  const duplicate = existingFolders.includes(trimmed)

  const handleSubmit = () => {
    if (!trimmed || duplicate) return
    onSubmit(trimmed)
  }

  return (
    <ModalOverlay onClose={onClose}>
      <div className="p-6">
        <div className="flex items-center justify-between mb-5">
          <h2 className="text-lg font-semibold text-white">{t('dashboard.create_folder')}</h2>
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

function RenameFolderModal({ folderName, onClose, onRenamed }: {
  folderName: string
  onClose: () => void
  onRenamed: (oldName: string, newName: string) => Promise<void>
}) {
  const { t } = useTranslation()
  const [name, setName] = useState(folderName)
  const [saving, setSaving] = useState(false)

  const submit = async () => {
    const trimmed = name.trim()
    if (!trimmed || trimmed === folderName) return
    setSaving(true)
    try { await onRenamed(folderName, trimmed) } finally { setSaving(false) }
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
