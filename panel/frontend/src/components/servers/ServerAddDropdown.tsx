import { useEffect, useMemo, useRef, useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { ChevronDown, Folder, FolderOpen, Lock, Plus, Search, Server as ServerIcon, X } from 'lucide-react'
import type { Server } from '../../api/client'
import { orderFolders } from '../../utils/folders'
import { Tooltip } from '../ui/Tooltip'

const NO_FOLDER = '__no_folder__'

interface Labels {
  placeholder: string
  noFolder: string
  addFolder: string
  allAdded: string
  noResults: string
  clearSearch: string
}

interface Props {
  servers: Server[]
  /** Уже выбранные — в списке не показываются */
  excludeIds: number[]
  onAdd: (ids: number[]) => void
  labels: Labels
  /** Ключ localStorage для раскрытых папок */
  storageKey: string
  /** Помечает замком ноды, которые действие всё равно не примут */
  isRestricted?: (server: Server) => boolean
}

interface Groups {
  folders: Map<string, Server[]>
  noFolder: Server[]
}

function readExpanded(storageKey: string): Set<string> {
  try {
    const raw = localStorage.getItem(storageKey)
    return raw ? new Set(JSON.parse(raw)) : new Set()
  } catch {
    return new Set()
  }
}

/**
 * Поиск с выпадающим списком серверов по папкам — для добавления в список
 * (исключения, привязки). Уже добавленные скрыты, папку можно добавить целиком.
 */
export function ServerAddDropdown({ servers, excludeIds, onAdd, labels, storageKey, isRestricted }: Props) {
  const [search, setSearch] = useState('')
  const [open, setOpen] = useState(false)
  const [expanded, setExpanded] = useState<Set<string>>(() => readExpanded(storageKey))
  const containerRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const handleClick = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', handleClick)
    return () => document.removeEventListener('mousedown', handleClick)
  }, [])

  const candidates = useMemo(() => {
    const excluded = new Set(excludeIds)
    return servers.filter(s => !excluded.has(s.id))
  }, [servers, excludeIds])

  const grouped = useMemo<Groups>(() => {
    const folders = new Map<string, Server[]>()
    const noFolder: Server[] = []
    for (const s of candidates) {
      if (!s.folder) {
        noFolder.push(s)
        continue
      }
      if (!folders.has(s.folder)) folders.set(s.folder, [])
      folders.get(s.folder)!.push(s)
    }
    return { folders, noFolder }
  }, [candidates])

  const filtered = useMemo<Groups>(() => {
    const q = search.toLowerCase().trim()
    if (!q) return grouped
    const matches = (s: Server) => s.name.toLowerCase().includes(q) || s.url.toLowerCase().includes(q)
    const folders = new Map<string, Server[]>()
    for (const [name, list] of grouped.folders) {
      const matched = list.filter(matches)
      if (matched.length > 0) folders.set(name, matched)
    }
    return { folders, noFolder: grouped.noFolder.filter(matches) }
  }, [search, grouped])

  const sortedFolderNames = useMemo(() => orderFolders([...filtered.folders.keys()]), [filtered.folders])

  const searching = search.trim().length > 0
  // Папки есть у кого-то из всех серверов — иначе список плоский,
  // даже если все «беспапочные» уже добавлены
  const hasFolders = servers.some(s => s.folder)
  const nothingVisible = filtered.folders.size === 0 && filtered.noFolder.length === 0
  const emptyMessage = candidates.length === 0 ? labels.allAdded : labels.noResults

  const toggleCollapsed = (key: string) => {
    setExpanded(prev => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      try {
        localStorage.setItem(storageKey, JSON.stringify([...next]))
      } catch {
        // приватный режим — просто не запомним раскрытые папки
      }
      return next
    })
  }

  const renderRow = (server: Server, nested: boolean) => (
    <button
      key={server.id}
      type="button"
      onClick={() => onAdd([server.id])}
      className={`w-full flex items-center gap-2.5 py-2 pr-3 text-left text-sm text-dark-300 hover:bg-dark-800/80 transition-colors ${
        nested ? 'pl-8' : 'pl-3'
      }`}
    >
      <Plus className="w-3.5 h-3.5 text-dark-500 shrink-0" />
      <ServerIcon className="w-3.5 h-3.5 text-dark-500 shrink-0" />
      <span className="flex-1 min-w-0">
        <span className="block truncate">{server.name}</span>
        <span className="block text-xs text-dark-500 truncate">{server.url.replace(/^https?:\/\//, '')}</span>
      </span>
      {isRestricted?.(server) && <Lock className="w-3 h-3 text-purple shrink-0" />}
    </button>
  )

  const renderGroup = (key: string, label: string, list: Server[], isFolder: boolean) => {
    const isCollapsed = !searching && !expanded.has(key)
    const icon = isFolder
      ? (isCollapsed
          ? <Folder className="w-4 h-4 text-accent-400 shrink-0" />
          : <FolderOpen className="w-4 h-4 text-accent-400 shrink-0" />)
      : <ServerIcon className="w-4 h-4 text-dark-400 shrink-0" />

    return (
      <div key={key}>
        <div className="flex items-center gap-2 px-3 py-2 bg-dark-900/40 hover:bg-dark-800/50 transition-colors">
          <div
            className="flex items-center gap-2 flex-1 min-w-0 cursor-pointer select-none"
            onClick={() => toggleCollapsed(key)}
          >
            {icon}
            <span className={`text-sm font-medium truncate ${isFolder ? 'text-dark-200' : 'text-dark-400'}`}>{label}</span>
            <span className="text-xs text-dark-500 shrink-0">({list.length})</span>
            <motion.div animate={{ rotate: isCollapsed ? -90 : 0 }} transition={{ duration: 0.15 }}>
              <ChevronDown className="w-3.5 h-3.5 text-dark-500" />
            </motion.div>
          </div>
          <Tooltip label={labels.addFolder}>
            <button
              type="button"
              onClick={() => onAdd(list.map(s => s.id))}
              className="p-1 rounded-md text-dark-400 hover:text-accent-400 hover:bg-accent-500/10 transition-colors shrink-0"
            >
              <Plus className="w-3.5 h-3.5" />
            </button>
          </Tooltip>
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
              {list.map(s => renderRow(s, true))}
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    )
  }

  return (
    <div className="relative" ref={containerRef}>
      <div
        onClick={() => setOpen(v => !v)}
        className="flex items-center gap-2 px-3 py-2 rounded-lg bg-dark-800 border border-dark-700 cursor-pointer hover:border-dark-600"
      >
        <Search className="w-4 h-4 text-dark-400 shrink-0" />
        <input
          type="text"
          value={search}
          onChange={e => { setSearch(e.target.value); setOpen(true) }}
          onFocus={() => setOpen(true)}
          onClick={e => e.stopPropagation()}
          placeholder={labels.placeholder}
          className="bg-transparent text-sm text-dark-100 placeholder-dark-500 outline-none w-full"
        />
        {search && (
          <Tooltip label={labels.clearSearch}>
            <button
              type="button"
              onClick={e => { e.stopPropagation(); setSearch('') }}
              className="text-dark-500 hover:text-dark-300"
            >
              <X className="w-3.5 h-3.5" />
            </button>
          </Tooltip>
        )}
      </div>

      {open && (
        <div className="absolute z-20 mt-1 w-full bg-dark-900 border border-dark-700 rounded-lg shadow-xl max-h-[32rem] overflow-y-auto">
          {nothingVisible ? (
            <p className="text-xs text-dark-500 py-3 text-center">{emptyMessage}</p>
          ) : hasFolders ? (
            <>
              {sortedFolderNames.map(name => renderGroup(name, name, filtered.folders.get(name)!, true))}
              {filtered.noFolder.length > 0 && renderGroup(NO_FOLDER, labels.noFolder, filtered.noFolder, false)}
            </>
          ) : (
            filtered.noFolder.map(s => renderRow(s, false))
          )}
        </div>
      )}
    </div>
  )
}
