import { useState, useEffect, useMemo, useCallback, useRef } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { Network, Plus, ChevronDown, ChevronRight, Check, X, Server as ServerIcon } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import { useInfraStore } from '../../stores/infraStore'
import { useServersStore } from '../../stores/serversStore'
import AccountNode from './AccountNode'
import InfraServerRow from './InfraServerRow'
import { readStorage, writeStorage } from '../../utils/storage'
import type { InfraTree as InfraTreeData } from '../../api/client'

const COLLAPSED_KEY = 'infra_collapsed'
const UNASSIGNED_OPEN_KEY = 'infra_unassigned_open'

function loadCollapsed(): Set<string> {
  try {
    const raw = localStorage.getItem(COLLAPSED_KEY)
    return raw ? new Set(JSON.parse(raw)) : new Set()
  } catch { return new Set() }
}

function saveCollapsed(set: Set<string>) {
  writeStorage(COLLAPSED_KEY, JSON.stringify([...set]))
}

// Ключи аккаунтов и проектов, которые надо раскрыть, чтобы строка сервера стала видна
function findServerNodeKeys(tree: InfraTreeData, serverId: number): string[] {
  const keys: string[] = []
  for (const acc of tree.accounts) {
    const projectKeys = acc.projects
      .filter(proj => proj.server_ids.includes(serverId))
      .map(proj => `p-${proj.id}`)
    if (projectKeys.length === 0 && !acc.server_ids.includes(serverId)) continue
    keys.push(`a-${acc.id}`, ...projectKeys)
  }
  return keys
}

interface InfraTreeProps {
  highlightedServerId?: number | null
}

export default function InfraTree({ highlightedServerId = null }: InfraTreeProps) {
  const { t } = useTranslation()
  const {
    tree, isLoading, fetchTree,
    createAccount, updateAccount, deleteAccount,
    createProject, updateProject, deleteProject,
    addServerToProject, removeServerFromProject,
    addServerToAccount, removeServerFromAccount,
  } = useInfraStore()
  const servers = useServersStore(s => s.servers)

  const [collapsed, setCollapsed] = useState(loadCollapsed)
  const [showAddAccount, setShowAddAccount] = useState(false)
  const [newAccountName, setNewAccountName] = useState('')
  const [treeVisible, setTreeVisible] = useState(() => readStorage('infra_visible') !== 'false')
  const [unassignedOpen, setUnassignedOpen] = useState(() => readStorage(UNASSIGNED_OPEN_KEY) === 'true')

  useEffect(() => { fetchTree() }, [fetchTree])

  useEffect(() => { writeStorage('infra_visible', String(treeVisible)) }, [treeVisible])

  useEffect(() => { writeStorage(UNASSIGNED_OPEN_KEY, String(unassignedOpen)) }, [unassignedOpen])

  const toggle = useCallback((key: string) => {
    setCollapsed(prev => {
      const next = new Set(prev)
      next.has(key) ? next.delete(key) : next.add(key)
      saveCollapsed(next)
      return next
    })
  }, [])

  // Путь к выделенному серверу раскрывается один раз на выделение: если потом свернуть
  // узел вручную, обновление дерева не должно раскрывать его обратно
  const revealedServerIdRef = useRef<number | null>(null)

  useEffect(() => {
    if (highlightedServerId === null) {
      revealedServerIdRef.current = null
      return
    }
    if (!tree || revealedServerIdRef.current === highlightedServerId) return
    revealedServerIdRef.current = highlightedServerId

    const keysToOpen = findServerNodeKeys(tree, highlightedServerId)
    const isUnassigned = tree.unassigned_server_ids.includes(highlightedServerId)
    if (keysToOpen.length === 0 && !isUnassigned) return

    setTreeVisible(true)
    if (isUnassigned) setUnassignedOpen(true)
    setCollapsed(prev => {
      if (!keysToOpen.some(key => prev.has(key))) return prev
      const next = new Set(prev)
      for (const key of keysToOpen) next.delete(key)
      saveCollapsed(next)
      return next
    })
  }, [highlightedServerId, tree])

  const serverMap = useMemo(() => {
    const map = new Map<number, (typeof servers)[0]>()
    for (const s of servers) map.set(s.id, s)
    return map
  }, [servers])

  const allAssignedIds = useMemo(() => {
    if (!tree) return new Set<number>()
    const set = new Set<number>()
    for (const acc of tree.accounts) {
      for (const sid of acc.server_ids) set.add(sid)
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
          className="flex flex-1 items-center gap-2 self-stretch text-dark-300 hover:text-dark-100 transition-colors"
        >
          {treeVisible ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}
          <Network className="w-4 h-4" />
          <span className="text-sm font-medium">{t('infra.title')}</span>
        </button>

        {treeVisible && (
          <button
            onClick={() => { setNewAccountName(''); setShowAddAccount(true) }}
            className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-lg bg-dark-800 border border-dark-600 hover:border-primary/40 text-dark-300 hover:text-primary transition-all"
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
                highlightedServerId={highlightedServerId}
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
                onAddAccountServer={(sid) => addServerToAccount(acc.id, sid)}
                onRemoveAccountServer={(sid) => removeServerFromAccount(acc.id, sid)}
              />
            ))}

            {/* Unassigned servers */}
            {tree && tree.unassigned_server_ids.length > 0 && (
              <div className="mt-2 pt-2 border-t border-dark-700/50">
                <button
                  onClick={() => setUnassignedOpen(!unassignedOpen)}
                  className="flex items-center gap-2 w-full px-2 py-2 rounded-lg text-dark-300 hover:text-dark-100 hover:bg-dark-800/50 transition-colors"
                >
                  <span className="p-1 text-dark-400">
                    {unassignedOpen ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}
                  </span>
                  <ServerIcon className="w-4 h-4 text-dark-400 shrink-0" />
                  <span className="text-sm font-semibold">{t('infra.unassigned')}</span>
                  <span className="px-1.5 py-0.5 rounded-md bg-dark-800 border border-dark-700 text-xs font-medium text-dark-300">
                    {tree.unassigned_server_ids.length}
                  </span>
                </button>
                <AnimatePresence>
                  {unassignedOpen && (
                    <motion.div initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: 'auto' }} exit={{ opacity: 0, height: 0 }}>
                      {tree.unassigned_server_ids.map(sid => {
                        const srv = serverMap.get(sid)
                        if (!srv) return null
                        return <InfraServerRow key={sid} server={srv} highlighted={sid === highlightedServerId} />
                      })}
                    </motion.div>
                  )}
                </AnimatePresence>
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
