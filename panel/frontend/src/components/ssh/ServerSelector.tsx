import { useMemo, useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { useTranslation } from 'react-i18next'
import { Server, Search, ChevronDown, Folder, FolderOpen, Eye } from 'lucide-react'
import { Server as ServerType } from '../../api/client'
import { Checkbox } from '../ui/Checkbox'
import { Tooltip } from '../ui/Tooltip'

const EXPANDED_FOLDERS_KEY = 'ssh_expanded_folders'
const NO_FOLDER = '__no_folder__'

interface ServerSelectorProps {
  servers: ServerType[]
  selectedIds: number[]
  onChange: (ids: number[]) => void
  activeId?: number | null
  onOpenServer?: (id: number) => void
}

export function ServerSelector({ servers, selectedIds, onChange, activeId, onOpenServer }: ServerSelectorProps) {
  const { t } = useTranslation()
  const [search, setSearch] = useState('')
  const [expandedFolders, setExpandedFolders] = useState<Set<string>>(() => {
    try {
      const raw = localStorage.getItem(EXPANDED_FOLDERS_KEY)
      return raw ? new Set(JSON.parse(raw)) : new Set()
    } catch {
      return new Set()
    }
  })

  const grouped = useMemo(() => {
    const folders = new Map<string, ServerType[]>()
    const noFolder: ServerType[] = []
    for (const s of servers) {
      if (s.folder) {
        if (!folders.has(s.folder)) folders.set(s.folder, [])
        folders.get(s.folder)!.push(s)
      } else {
        noFolder.push(s)
      }
    }
    return { folders, noFolder }
  }, [servers])

  const sortedFolderNames = useMemo(() => {
    const names = [...grouped.folders.keys()]
    try {
      const saved: string[] = JSON.parse(localStorage.getItem('dashboard_folder_order') || '[]')
      const ordered = saved.filter(f => names.includes(f))
      const rest = names.filter(f => !saved.includes(f)).sort()
      return [...ordered, ...rest]
    } catch {
      return names.sort()
    }
  }, [grouped.folders])

  const filtered = useMemo(() => {
    const q = search.toLowerCase().trim()
    if (!q) return grouped
    const matches = (s: ServerType) =>
      s.name.toLowerCase().includes(q) || s.url.toLowerCase().includes(q)
    const folders = new Map<string, ServerType[]>()
    for (const [name, list] of grouped.folders) {
      const m = list.filter(matches)
      if (m.length) folders.set(name, m)
    }
    return { folders, noFolder: grouped.noFolder.filter(matches) }
  }, [search, grouped])

  const visibleIds = useMemo(
    () => [...Array.from(filtered.folders.values()).flat(), ...filtered.noFolder].map(s => s.id),
    [filtered],
  )

  const toggleServer = (id: number) => {
    onChange(selectedIds.includes(id) ? selectedIds.filter(x => x !== id) : [...selectedIds, id])
  }

  const toggleFolder = (folderServers: ServerType[]) => {
    const ids = folderServers.map(s => s.id)
    const allSelected = ids.every(id => selectedIds.includes(id))
    onChange(allSelected
      ? selectedIds.filter(id => !ids.includes(id))
      : [...new Set([...selectedIds, ...ids])])
  }

  const folderCheckState = (folderServers: ServerType[]): 'none' | 'some' | 'all' => {
    const ids = folderServers.map(s => s.id)
    const count = ids.filter(id => selectedIds.includes(id)).length
    if (count === 0) return 'none'
    return count === ids.length ? 'all' : 'some'
  }

  const toggleCollapsed = (folder: string) => {
    setExpandedFolders(prev => {
      const next = new Set(prev)
      if (next.has(folder)) next.delete(folder)
      else next.add(folder)
      localStorage.setItem(EXPANDED_FOLDERS_KEY, JSON.stringify([...next]))
      return next
    })
  }

  const selectAllVisible = () => onChange([...new Set([...selectedIds, ...visibleIds])])
  const deselectAllVisible = () => {
    const visible = new Set(visibleIds)
    onChange(selectedIds.filter(id => !visible.has(id)))
  }

  const renderServerRow = (server: ServerType) => (
    <motion.label
      key={server.id}
      className={`flex items-center gap-3 p-2 rounded-xl cursor-pointer transition-all
        ${selectedIds.includes(server.id)
          ? 'bg-accent-500/10 border border-accent-500/30'
          : 'bg-dark-800/50 border border-transparent hover:bg-dark-800'}
        ${server.id === activeId ? 'ring-1 ring-accent-500/40' : ''}`}
      whileHover={{ scale: 1.01 }}
      whileTap={{ scale: 0.99 }}
    >
      <Checkbox
        checked={selectedIds.includes(server.id)}
        onChange={() => toggleServer(server.id)}
      />
      <div className="flex-1 min-w-0">
        <p className="font-medium text-sm text-dark-100 truncate">{server.name}</p>
        <p className="text-xs text-dark-500 truncate">{server.url}</p>
      </div>
      <div className={`w-2 h-2 rounded-full shrink-0 ${server.is_active ? 'bg-success' : 'bg-dark-600'}`} />
      {onOpenServer && (
        <Tooltip label={t('ssh_security.selector_open')}>
          <button
            type="button"
            onClick={e => { e.preventDefault(); onOpenServer(server.id) }}
            className="p-1 text-dark-500 hover:text-accent-400 transition-colors shrink-0"
          >
            <Eye className="w-4 h-4" />
          </button>
        </Tooltip>
      )}
    </motion.label>
  )

  const renderGroup = (key: string, label: string, allServers: ServerType[], visibleServers: ServerType[], isNoFolder: boolean) => {
    const checkState = folderCheckState(allServers)
    const isCollapsed = !expandedFolders.has(key)
    const selectedInGroup = allServers.filter(s => selectedIds.includes(s.id)).length

    return (
      <div key={key} className="mb-1">
        <div className="flex items-center gap-2 p-2 rounded-lg hover:bg-dark-800/50 transition-colors">
          <Checkbox
            checked={checkState === 'all'}
            indeterminate={checkState === 'some'}
            onChange={() => toggleFolder(allServers)}
          />
          <div
            className="flex items-center gap-2 flex-1 min-w-0 cursor-pointer"
            onClick={() => toggleCollapsed(key)}
          >
            {isNoFolder
              ? <Server className="w-4 h-4 text-dark-400 shrink-0" />
              : isCollapsed
                ? <Folder className="w-4 h-4 text-accent-400 shrink-0" />
                : <FolderOpen className="w-4 h-4 text-accent-400 shrink-0" />}
            <span className={`font-medium text-sm truncate ${isNoFolder ? 'text-dark-400' : 'text-dark-200'}`}>{label}</span>
            <span className="text-xs text-dark-500 ml-auto shrink-0">{selectedInGroup}/{allServers.length}</span>
            <motion.div animate={{ rotate: isCollapsed ? -90 : 0 }} transition={{ duration: 0.15 }}>
              <ChevronDown className="w-3.5 h-3.5 text-dark-500" />
            </motion.div>
          </div>
        </div>
        <AnimatePresence initial={false}>
          {!isCollapsed && (
            <motion.div
              initial={{ height: 0, opacity: 0 }}
              animate={{ height: 'auto', opacity: 1 }}
              exit={{ height: 0, opacity: 0 }}
              transition={{ duration: 0.15 }}
              className="overflow-hidden"
            >
              <div className="pl-6 space-y-1 pt-1">
                {visibleServers.map(renderServerRow)}
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    )
  }

  const hasFolders = grouped.folders.size > 0
  const nothingVisible = filtered.folders.size === 0 && filtered.noFolder.length === 0

  return (
    <div className="card">
      <div className="flex items-center justify-between mb-3">
        <h2 className="font-semibold text-dark-100 flex items-center gap-2">
          <Server className="w-4 h-4 text-accent-500" />
          {t('ssh_security.selector_title')}
        </h2>
        <span className="text-xs text-dark-400 bg-dark-800 px-2 py-1 rounded-lg">
          {t('ssh_security.selector_selected', { count: selectedIds.length })}
        </span>
      </div>

      {servers.length === 0 ? (
        <div className="text-center py-8">
          <Server className="w-12 h-12 text-dark-600 mx-auto mb-3" />
          <p className="text-dark-400 text-sm">{t('ssh_security.no_servers')}</p>
        </div>
      ) : (
        <>
          <div className="flex items-center gap-2 bg-dark-800 border border-dark-600 rounded-lg px-3 py-1.5 mb-3">
            <Search className="w-4 h-4 text-dark-400 shrink-0" />
            <input
              type="text"
              value={search}
              onChange={e => setSearch(e.target.value)}
              placeholder={t('ssh_security.selector_search')}
              className="bg-transparent text-sm text-dark-100 placeholder-dark-500 outline-none w-full"
            />
          </div>
          <div className="flex gap-2 mb-3">
            <button onClick={selectAllVisible} className="btn btn-secondary text-xs py-1.5 px-3">
              {t('ssh_security.selector_select_all')}
            </button>
            <button onClick={deselectAllVisible} className="btn btn-secondary text-xs py-1.5 px-3">
              {t('ssh_security.selector_deselect_all')}
            </button>
          </div>

          <div className="space-y-1 max-h-[420px] overflow-y-auto pr-1">
            {hasFolders ? (
              <>
                {sortedFolderNames
                  .filter(name => filtered.folders.has(name))
                  .map(name => renderGroup(
                    name, name,
                    grouped.folders.get(name)!,
                    filtered.folders.get(name)!,
                    false,
                  ))}
                {filtered.noFolder.length > 0 && renderGroup(
                  NO_FOLDER, t('ssh_security.selector_no_folder'),
                  grouped.noFolder, filtered.noFolder, true,
                )}
              </>
            ) : (
              filtered.noFolder.map(renderServerRow)
            )}
            {nothingVisible && (
              <div className="text-center py-6">
                <Search className="w-8 h-8 text-dark-600 mx-auto mb-2" />
                <p className="text-dark-400 text-sm">{t('ssh_security.selector_no_results')}</p>
              </div>
            )}
          </div>
        </>
      )}
    </div>
  )
}
