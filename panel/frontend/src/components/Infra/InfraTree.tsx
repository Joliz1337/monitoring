import { useState, useEffect, useMemo, useCallback } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { Network, Plus, ChevronDown, ChevronRight, Check, X, Server as ServerIcon } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import { useInfraStore } from '../../stores/infraStore'
import { useServersStore } from '../../stores/serversStore'
import AccountNode from './AccountNode'
import InfraServerRow from './InfraServerRow'

const COLLAPSED_KEY = 'infra_collapsed'

function loadCollapsed(): Set<string> {
  try {
    const raw = localStorage.getItem(COLLAPSED_KEY)
    return raw ? new Set(JSON.parse(raw)) : new Set()
  } catch { return new Set() }
}

function saveCollapsed(set: Set<string>) {
  localStorage.setItem(COLLAPSED_KEY, JSON.stringify([...set]))
}

export default function InfraTree() {
  const { t } = useTranslation()
  const { tree, isLoading, fetchTree, createAccount, updateAccount, deleteAccount, createProject, updateProject, deleteProject, addServerToProject, removeServerFromProject } = useInfraStore()
  const servers = useServersStore(s => s.servers)

  const [collapsed, setCollapsed] = useState(loadCollapsed)
  const [showAddAccount, setShowAddAccount] = useState(false)
  const [newAccountName, setNewAccountName] = useState('')
  const [treeVisible, setTreeVisible] = useState(() => localStorage.getItem('infra_visible') !== 'false')

  useEffect(() => { fetchTree() }, [fetchTree])

  useEffect(() => { localStorage.setItem('infra_visible', String(treeVisible)) }, [treeVisible])

  const toggle = useCallback((key: string) => {
    setCollapsed(prev => {
      const next = new Set(prev)
      next.has(key) ? next.delete(key) : next.add(key)
      saveCollapsed(next)
      return next
    })
  }, [])

  const serverMap = useMemo(() => {
    const map = new Map<number, (typeof servers)[0]>()
    for (const s of servers) map.set(s.id, s)
    return map
  }, [servers])

  const allAssignedIds = useMemo(() => {
    if (!tree) return new Set<number>()
    const set = new Set<number>()
    for (const acc of tree.accounts) {
      for (const proj of acc.projects) {
        for (const sid of proj.server_ids) set.add(sid)
      }
    }
    return set
  }, [tree])

  const handleCreateAccount = async () => {
    const trimmed = newAccountName.trim()
    if (!trimmed) return
    try {
      await createAccount(trimmed)
      setNewAccountName('')
      setShowAddAccount(false)
      toast.success(t('infra.account_created'))
    } catch { toast.error(t('common.error')) }
  }

  const hasContent = tree && (tree.accounts.length > 0 || tree.unassigned_server_ids.length > 0)

  return (
    <div className="mb-6">
      {/* Header */}
      <div className="flex items-center gap-3 mb-3">
        <button
          onClick={() => setTreeVisible(!treeVisible)}
          className="flex items-center gap-2 text-dark-300 hover:text-dark-100 transition-colors"
        >
          {treeVisible ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}
          <Network className="w-4 h-4" />
          <span className="text-sm font-medium">{t('infra.title')}</span>
        </button>

        {treeVisible && (
          <button
            onClick={() => { setNewAccountName(''); setShowAddAccount(true) }}
            className="ml-auto flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-lg bg-dark-800 border border-dark-600 hover:border-primary/40 text-dark-300 hover:text-primary transition-all"
          >
            <Plus className="w-3.5 h-3.5" />
            {t('infra.add_account')}
          </button>
        )}
      </div>

      <AnimatePresence>
        {treeVisible && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: 'auto' }}
            exit={{ opacity: 0, height: 0 }}
            className="bg-dark-900/50 border border-dark-700/50 rounded-xl p-3"
          >
            {isLoading && !tree && (
              <div className="text-sm text-dark-400 py-4 text-center">{t('common.loading')}...</div>
            )}

            {/* Add account inline form */}
            <AnimatePresence>
              {showAddAccount && (
                <motion.div initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: 'auto' }} exit={{ opacity: 0, height: 0 }} className="mb-3">
                  <div className="flex items-center gap-2">
                    <input
                      autoFocus
                      value={newAccountName}
                      onChange={e => setNewAccountName(e.target.value)}
                      onKeyDown={e => { if (e.key === 'Enter') handleCreateAccount(); if (e.key === 'Escape') setShowAddAccount(false) }}
                      placeholder={t('infra.account_name')}
                      className="bg-dark-800 border border-dark-600 rounded-lg px-3 py-1.5 text-sm text-dark-100 placeholder:text-dark-500 outline-none focus:border-primary/50 w-64"
                    />
                    <button onClick={handleCreateAccount} className="p-1.5 rounded-lg hover:bg-dark-700 text-success"><Check className="w-4 h-4" /></button>
                    <button onClick={() => setShowAddAccount(false)} className="p-1.5 rounded-lg hover:bg-dark-700 text-dark-400"><X className="w-4 h-4" /></button>
                  </div>
                </motion.div>
              )}
            </AnimatePresence>

            {/* Accounts */}
            {tree?.accounts.map(acc => (
              <AccountNode
                key={acc.id}
                account={acc}
                servers={serverMap}
                allServers={servers}
                allAssignedIds={allAssignedIds}
                collapsedProjects={collapsed}
                onToggleProject={toggle}
                collapsed={collapsed.has(`a-${acc.id}`)}
                onToggle={() => toggle(`a-${acc.id}`)}
                onRename={(name) => updateAccount(acc.id, name)}
                onDelete={() => deleteAccount(acc.id)}
                onCreateProject={(name) => createProject(acc.id, name)}
                onRenameProject={(pid, name) => updateProject(pid, { name })}
                onDeleteProject={(pid) => deleteProject(pid)}
                onAddServer={(pid, sid) => addServerToProject(pid, sid)}
                onRemoveServer={(pid, sid) => removeServerFromProject(pid, sid)}
              />
            ))}

            {/* Unassigned servers */}
            {tree && tree.unassigned_server_ids.length > 0 && (
              <div className="mt-2 pt-2 border-t border-dark-700/50">
                <div className="flex items-center gap-2 px-2 py-1.5 text-dark-400">
                  <ServerIcon className="w-4 h-4" />
                  <span className="text-xs font-medium">{t('infra.unassigned')}</span>
                  <span className="text-xs text-dark-500">{tree.unassigned_server_ids.length}</span>
                </div>
                {tree.unassigned_server_ids.map(sid => {
                  const srv = serverMap.get(sid)
                  if (!srv) return null
                  return <InfraServerRow key={sid} server={srv} />
                })}
              </div>
            )}

            {/* Empty state */}
            {tree && !hasContent && (
              <div className="text-sm text-dark-500 py-4 text-center">{t('infra.empty')}</div>
            )}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}
