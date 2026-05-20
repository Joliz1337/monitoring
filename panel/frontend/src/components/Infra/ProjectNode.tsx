import { useState, useMemo } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { ChevronRight, ChevronDown, FolderOpen, Plus, Edit2, Trash2, Check, X, Activity } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import InfraServerRow from './InfraServerRow'
import ServerSearchDropdown from './ServerSearchDropdown'
import { formatBitsPerSec } from '../../utils/format'
import type { InfraProject, ServerMetrics } from '../../api/client'

interface ServerData {
  id: number
  name: string
  url: string
  status: 'online' | 'offline' | 'loading' | 'error'
  metrics?: ServerMetrics | null
}

interface ProjectNodeProps {
  project: InfraProject
  servers: Map<number, ServerData>
  allServers: ServerData[]
  allAssignedIds: Set<number>
  collapsed: boolean
  onToggle: () => void
  onRename: (name: string) => Promise<void>
  onDelete: () => Promise<void>
  onAddServer: (serverId: number) => Promise<void>
  onRemoveServer: (serverId: number) => Promise<void>
}

export default function ProjectNode({
  project, servers, allServers, allAssignedIds,
  collapsed, onToggle, onRename, onDelete, onAddServer, onRemoveServer,
}: ProjectNodeProps) {
  const { t } = useTranslation()
  const [editing, setEditing] = useState(false)
  const [editName, setEditName] = useState(project.name)
  const [showSearch, setShowSearch] = useState(false)
  const [deleteConfirm, setDeleteConfirm] = useState(false)

  const totalSpeed = useMemo(() => {
    let rx = 0, tx = 0
    for (const sid of project.server_ids) {
      const srv = servers.get(sid)
      if (srv?.status !== 'online' || !srv.metrics?.network?.total) continue
      rx += srv.metrics.network.total.rx_bytes_per_sec ?? 0
      tx += srv.metrics.network.total.tx_bytes_per_sec ?? 0
    }
    return { rx, tx, hasTraffic: rx > 0 || tx > 0 }
  }, [project.server_ids, servers])

  const handleRename = async () => {
    const trimmed = editName.trim()
    if (!trimmed || trimmed === project.name) { setEditing(false); return }
    try {
      await onRename(trimmed)
      setEditing(false)
    } catch { toast.error(t('common.error')) }
  }

  const handleDelete = async () => {
    try {
      await onDelete()
      toast.success(t('infra.project_deleted'))
    } catch { toast.error(t('common.error')) }
    setDeleteConfirm(false)
  }

  return (
    <div className="ml-4">
      {/* Project header */}
      <div className="flex items-center gap-2 py-1.5 group">
        <button onClick={onToggle} className="p-0.5 rounded hover:bg-dark-700 text-dark-400 transition-colors">
          {collapsed ? <ChevronRight className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}
        </button>
        <FolderOpen className="w-4 h-4 text-primary/60 shrink-0" />

        {editing ? (
          <div className="flex items-center gap-1 flex-1">
            <input
              autoFocus
              value={editName}
              onChange={e => setEditName(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter') handleRename(); if (e.key === 'Escape') setEditing(false) }}
              className="bg-dark-800 border border-dark-600 rounded px-2 py-0.5 text-sm text-dark-100 outline-none focus:border-primary/50 w-40"
            />
            <button onClick={handleRename} className="p-1 rounded hover:bg-dark-700 text-success"><Check className="w-3.5 h-3.5" /></button>
            <button onClick={() => setEditing(false)} className="p-1 rounded hover:bg-dark-700 text-dark-400"><X className="w-3.5 h-3.5" /></button>
          </div>
        ) : (
          <>
            <span className="text-sm font-medium text-dark-200">{project.name}</span>
            <span className="text-xs text-dark-500">{project.server_ids.length}</span>
            {totalSpeed.hasTraffic && (
              <div className="flex items-center gap-1 text-xs font-mono font-medium text-dark-200 ml-1">
                <Activity className="w-3.5 h-3.5 text-accent-400" />
                <span>↓{formatBitsPerSec(totalSpeed.rx, 0)} ↑{formatBitsPerSec(totalSpeed.tx, 0)}</span>
              </div>
            )}
          </>
        )}

        {!editing && (
          <div className="flex items-center gap-0.5 ml-auto opacity-0 group-hover:opacity-100 transition-opacity">
            <button onClick={() => setShowSearch(!showSearch)} className="p-1 rounded hover:bg-dark-700 text-dark-400 hover:text-primary" title={t('infra.add_server')}>
              <Plus className="w-3.5 h-3.5" />
            </button>
            <button onClick={() => { setEditName(project.name); setEditing(true) }} className="p-1 rounded hover:bg-dark-700 text-dark-400 hover:text-dark-200" title={t('common.edit')}>
              <Edit2 className="w-3.5 h-3.5" />
            </button>
            {deleteConfirm ? (
              <div className="flex items-center gap-0.5">
                <button onClick={handleDelete} className="p-1 rounded hover:bg-dark-700 text-danger"><Check className="w-3.5 h-3.5" /></button>
                <button onClick={() => setDeleteConfirm(false)} className="p-1 rounded hover:bg-dark-700 text-dark-400"><X className="w-3.5 h-3.5" /></button>
              </div>
            ) : (
              <button onClick={() => setDeleteConfirm(true)} className="p-1 rounded hover:bg-dark-700 text-dark-400 hover:text-danger" title={t('common.delete')}>
                <Trash2 className="w-3.5 h-3.5" />
              </button>
            )}
          </div>
        )}
      </div>

      {/* Search dropdown */}
      <AnimatePresence>
        {showSearch && (
          <motion.div initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: 'auto' }} exit={{ opacity: 0, height: 0 }} className="ml-6 mb-2">
            <ServerSearchDropdown
              servers={allServers}
              excludeIds={allAssignedIds}
              onSelect={async (id) => { try { await onAddServer(id); toast.success(t('infra.server_added')) } catch { toast.error(t('common.error')) } }}
              onClose={() => setShowSearch(false)}
            />
          </motion.div>
        )}
      </AnimatePresence>

      {/* Servers list */}
      <AnimatePresence>
        {!collapsed && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: 'auto' }}
            exit={{ opacity: 0, height: 0 }}
            className="ml-2"
          >
            {project.server_ids.map(sid => {
              const srv = servers.get(sid)
              if (!srv) return null
              return (
                <InfraServerRow
                  key={sid}
                  server={srv}
                  onRemove={() => onRemoveServer(sid)}
                />
              )
            })}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}
