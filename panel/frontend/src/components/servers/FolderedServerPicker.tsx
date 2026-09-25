import { useMemo, useState, type ReactNode } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { ChevronDown, Folder, FolderOpen, Server as ServerIcon } from 'lucide-react'
import { orderFolders } from '../../utils/folders'

const NO_FOLDER = '__no_folder__'

export interface FolderedServer {
  id: number
  name: string
  url: string
  folder?: string | null
}

interface Labels {
  searchPlaceholder: string
  empty: string
  noFolder: string
}

interface Props<T extends FolderedServer> {
  servers: T[]
  /** Строка сервера — у каждого раздела своя: домен, метка «уже в профиле» и т.п. */
  renderServer: (server: T) => ReactNode
  labels: Labels
  /** Ключ localStorage для раскрытых папок */
  storageKey: string
}

interface Groups<T> {
  folders: Map<string, T[]>
  noFolder: T[]
}

function readExpanded(storageKey: string): Set<string> {
  try {
    const raw = localStorage.getItem(storageKey)
    return raw ? new Set(JSON.parse(raw)) : new Set()
  } catch {
    return new Set()
  }
}

function groupByFolder<T extends FolderedServer>(servers: T[]): Groups<T> {
  const folders = new Map<string, T[]>()
  const noFolder: T[] = []
  for (const server of servers) {
    if (!server.folder) {
      noFolder.push(server)
      continue
    }
    if (!folders.has(server.folder)) folders.set(server.folder, [])
    folders.get(server.folder)!.push(server)
  }
  return { folders, noFolder }
}

/** Выбор сервера из списка, разложенного по папкам дашборда, с поиском по имени и адресу */
export default function FolderedServerPicker<T extends FolderedServer>({
  servers, renderServer, labels, storageKey,
}: Props<T>) {
  const [search, setSearch] = useState('')
  const [expanded, setExpanded] = useState<Set<string>>(() => readExpanded(storageKey))

  const query = search.toLowerCase().trim()
  const groups = useMemo(() => {
    const matched = query
      ? servers.filter(s => s.name.toLowerCase().includes(query) || s.url.toLowerCase().includes(query))
      : servers
    return groupByFolder(matched)
  }, [servers, query])
  const folderNames = useMemo(() => orderFolders([...groups.folders.keys()]), [groups.folders])

  const toggle = (key: string) => {
    setExpanded(prev => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      try { localStorage.setItem(storageKey, JSON.stringify([...next])) } catch { /* приватный режим */ }
      return next
    })
  }

  const renderGroup = (key: string, label: string, members: T[], isFolder: boolean) => {
    // При поиске папки раскрыты: иначе найденный сервер пришлось бы ещё искать глазами
    const collapsed = !query && !expanded.has(key)
    const FolderIcon = collapsed ? Folder : FolderOpen
    return (
      <div key={key} className="mb-1">
        <div
          className="flex items-center gap-2 p-2 rounded-lg hover:bg-dark-800/50 transition-colors cursor-pointer"
          onClick={() => toggle(key)}
        >
          {isFolder
            ? <FolderIcon className="w-4 h-4 text-accent-400 shrink-0" />
            : <ServerIcon className="w-4 h-4 text-dark-400 shrink-0" />}
          <span className={`font-medium text-sm truncate ${isFolder ? 'text-dark-200' : 'text-dark-400'}`}>{label}</span>
          <span className="text-xs text-dark-500 ml-auto shrink-0">{members.length}</span>
          <motion.div animate={{ rotate: collapsed ? -90 : 0 }} transition={{ duration: 0.15 }}>
            <ChevronDown className="w-3.5 h-3.5 text-dark-500" />
          </motion.div>
        </div>
        <AnimatePresence initial={false}>
          {!collapsed && (
            <motion.div
              initial={{ height: 0, opacity: 0 }}
              animate={{ height: 'auto', opacity: 1 }}
              exit={{ height: 0, opacity: 0 }}
              transition={{ duration: 0.15 }}
              className="overflow-hidden"
            >
              <div className="pl-4 space-y-1 pt-1">
                {members.map(renderServer)}
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    )
  }

  const isEmpty = groups.folders.size === 0 && groups.noFolder.length === 0

  return (
    <>
      <input type="text" value={search} onChange={e => setSearch(e.target.value)}
        placeholder={labels.searchPlaceholder}
        className="w-full px-3 py-1.5 mb-2 rounded-lg bg-dark-800 border border-dark-700 text-dark-100 text-sm focus:outline-none focus:border-accent-500/50 transition-colors" autoFocus />
      {isEmpty ? (
        <div className="text-xs text-dark-500">{labels.empty}</div>
      ) : groups.folders.size > 0 ? (
        <div className="space-y-1 max-h-[32rem] overflow-y-auto">
          {folderNames.map(name => renderGroup(name, name, groups.folders.get(name)!, true))}
          {groups.noFolder.length > 0 && renderGroup(NO_FOLDER, labels.noFolder, groups.noFolder, false)}
        </div>
      ) : (
        <div className="space-y-1 max-h-48 overflow-y-auto">
          {groups.noFolder.map(renderServer)}
        </div>
      )}
    </>
  )
}
