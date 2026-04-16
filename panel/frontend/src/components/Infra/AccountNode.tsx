import { useState, useMemo } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { ChevronRight, ChevronDown, Mail, Plus, Edit2, Trash2, Check, X, Activity } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import ProjectNode from './ProjectNode'
import { formatBitsPerSec } from '../../utils/format'
import type { InfraAccount, ServerMetrics } from '../../api/client'

interface ServerData {
  id: number
  name: string
  url: string
  status: 'online' | 'offline' | 'loading' | 'error'
  metrics?: ServerMetrics | null
}

interface AccountNodeProps {
  account: InfraAccount
  servers: Map<number, ServerData>
  allServers: ServerData[]
  allAssignedIds: Set<number>
  collapsedProjects: Set<string>
  onToggleProject: (key: string) => void
  collapsed: boolean
  onToggle: () => void
  onRename: (name: string) => Promise<void>
  onDelete: () => Promise<void>
  onCreateProject: (name: string) => Promise<void>
  onRenameProject: (projectId: number, name: string) => Promise<void>
  onDeleteProject: (projectId: number) => Promise<void>
  onAddServer: (projectId: number, serverId: number) => Promise<void>
  onRemoveServer: (projectId: number, serverId: number) => Promise<void>
}

export default function AccountNode({
  account, servers, allServers, allAssignedIds,
  collapsedProjects, onToggleProject,
  collapsed, onToggle,
  onRename, onDelete, onCreateProject,
  onRenameProject, onDeleteProject, onAddServer, onRemoveServer,
}: AccountNodeProps) {
  const { t } = useTranslation()
  const [editing, setEditing] = useState(false)
  const [editName, setEditName] = useState(account.name)
  const [addingProject, setAddingProject] = useState(false)
  const [newProjectName, setNewProjectName] = useState('')
  const [deleteConfirm, setDeleteConfirm] = useState(false)

  const serverCount = account.projects.reduce((sum, p) => sum + p.server_ids.length, 0)

  const totalSpeed = useMemo(() => {
    let rx = 0, tx = 0
    for (const proj of account.projects) {
      for (const sid of proj.server_ids) {
        const srv = servers.get(sid)
        if (srv?.status !== 'online' || !srv.metrics?.network?.total) continue
        rx += srv.metrics.network.total.rx_bytes_per_sec ?? 0
        tx += srv.metrics.network.total.tx_bytes_per_sec ?? 0
      }
    }
    return { rx, tx, hasTraffic: rx > 0 || tx > 0 }
  }, [account.projects, servers])

  const handleRename = async () => {
    const trimmed = editName.trim()
    if (!trimmed || trimmed === account.name) { setEditing(false); return }
    try {
      await onRename(trimmed)
      setEditing(false)
    } catch { toast.error(t('common.error')) }
  }

  const handleDelete = async () => {
    try {
      await onDelete()
      toast.success(t('infra.account_deleted'))
    } catch { toast.error(t('common.error')) }
    setDeleteConfirm(false)
  }

  const handleCreateProject = async () => {
    const trimmed = newProjectName.trim()
    if (!trimmed) return
    try {
      await onCreateProject(trimmed)
      setNewProjectName('')
      setAddingProject(false)
      toast.success(t('infra.project_created'))
    } catch { toast.error(t('common.error')) }
  }

  return (
    <div className="mb-2">
      {/* Account header */}
      <div className="flex items-center gap-2 py-2 px-2 rounded-lg hover:bg-dark-800/50 group transition-colors">
        <button onClick={onToggle} className="p-0.5 rounded hover:bg-dark-700 text-dark-400 transition-colors">
          {collapsed ? <ChevronRight className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}
        </button>
        <Mail className="w-4 h-4 text-primary/70 shrink-0" />

        {editing ? (
          <div className="flex items-center gap-1 flex-1">
            <input
              autoFocus
              value={editName}
              onChange={e => setEditName(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter') handleRename(); if (e.key === 'Escape') setEditing(false) }}
              className="bg-dark-800 border border-dark-600 rounded px-2 py-1 text-sm text-dark-100 outline-none focus:border-primary/50 w-56"
            />
            <button onClick={handleRename} className="p-1 rounded hover:bg-dark-700 text-success"><Check className="w-4 h-4" /></button>
            <button onClick={() => setEditing(false)} className="p-1 rounded hover:bg-dark-700 text-dark-400"><X className="w-4 h-4" /></button>
          </div>
        ) : (
          <>
            <span className="text-sm font-semibold text-dark-100">{account.name}</span>
            <span className="text-xs text-dark-500">{account.projects.length} {t('infra.projects_short')} / {serverCount} {t('infra.servers_short')}</span>
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
            <button onClick={() => { setNewProjectName(''); setAddingProject(true) }} className="p-1 rounded hover:bg-dark-700 text-dark-400 hover:text-primary" title={t('infra.add_project')}>
              <Plus className="w-4 h-4" />
            </button>
            <button onClick={() => { setEditName(account.name); setEditing(true) }} className="p-1 rounded hover:bg-dark-700 text-dark-400 hover:text-dark-200" title={t('common.edit')}>
              <Edit2 className="w-4 h-4" />
            </button>
            {deleteConfirm ? (
              <div className="flex items-center gap-0.5">
                <button onClick={handleDelete} className="p-1 rounded hover:bg-dark-700 text-danger"><Check className="w-4 h-4" /></button>
                <button onClick={() => setDeleteConfirm(false)} className="p-1 rounded hover:bg-dark-700 text-dark-400"><X className="w-4 h-4" /></button>
              </div>
            ) : (
              <button onClick={() => setDeleteConfirm(true)} className="p-1 rounded hover:bg-dark-700 text-dark-400 hover:text-danger" title={t('common.delete')}>
                <Trash2 className="w-4 h-4" />
              </button>
            )}
          </div>
        )}
      </div>

      {/* Add project inline form */}
      <AnimatePresence>
        {addingProject && (
          <motion.div initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: 'auto' }} exit={{ opacity: 0, height: 0 }} className="ml-8 mb-1">
            <div className="flex items-center gap-1">
              <input
                autoFocus
                value={newProjectName}
                onChange={e => setNewProjectName(e.target.value)}
                onKeyDown={e => { if (e.key === 'Enter') handleCreateProject(); if (e.key === 'Escape') setAddingProject(false) }}
                placeholder={t('infra.project_name')}
                className="bg-dark-800 border border-dark-600 rounded px-2 py-1 text-sm text-dark-100 placeholder:text-dark-500 outline-none focus:border-primary/50 w-44"
              />
              <button onClick={handleCreateProject} className="p-1 rounded hover:bg-dark-700 text-success"><Check className="w-4 h-4" /></button>
              <button onClick={() => setAddingProject(false)} className="p-1 rounded hover:bg-dark-700 text-dark-400"><X className="w-4 h-4" /></button>
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Projects */}
      <AnimatePresence>
        {!collapsed && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: 'auto' }}
            exit={{ opacity: 0, height: 0 }}
          >
            {account.projects.map(proj => (
              <ProjectNode
                key={proj.id}
                project={proj}
                servers={servers}
                allServers={allServers}
                allAssignedIds={allAssignedIds}
                collapsed={collapsedProjects.has(`p-${proj.id}`)}
                onToggle={() => onToggleProject(`p-${proj.id}`)}
                onRename={(name) => onRenameProject(proj.id, name)}
                onDelete={() => onDeleteProject(proj.id)}
                onAddServer={(serverId) => onAddServer(proj.id, serverId)}
                onRemoveServer={(serverId) => onRemoveServer(proj.id, serverId)}
              />
            ))}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}
