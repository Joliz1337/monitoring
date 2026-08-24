import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Monitor, Search, Server, ChevronDown, Folder, FolderOpen } from 'lucide-react'
import type { ServerWithMetrics } from '../../api/client'
import { Checkbox } from '../ui/Checkbox'
import { nodeAllows } from '../../utils/nodeCapabilities'

const NO_FOLDER = '__no_folder__'
const EXPANDED_KEY = 'xray_test_expanded_folders'

interface Props {
  servers: ServerWithMetrics[]
  value: string[]
  onChange: (locations: string[]) => void
}

/**
 * Выбор мест запуска: сама панель и любое число нод.
 * Один ключ из разных точек ведёт себя по-разному, поэтому выбор множественный —
 * каждая точка даёт собственную строку результата.
 */
export function LocationPicker({ servers, value, onChange }: Props) {
  const { t } = useTranslation()
  const [search, setSearch] = useState('')
  const [expanded, setExpanded] = useState<Set<string>>(() => {
    try {
      const raw = localStorage.getItem(EXPANDED_KEY)
      return raw ? new Set(JSON.parse(raw)) : new Set()
    } catch {
      return new Set()
    }
  })

  // Нода без права на выполнение команд прогон не примет — показываем её
  // отдельной строкой, а не молча прячем
  const { usable, restricted } = useMemo(() => {
    const usable: ServerWithMetrics[] = []
    const restricted: ServerWithMetrics[] = []
    servers.forEach(server => {
      (nodeAllows(server, 'exec', 'write') ? usable : restricted).push(server)
    })
    return { usable, restricted }
  }, [servers])

  const grouped = useMemo(() => {
    const query = search.trim().toLowerCase()
    const matches = (server: ServerWithMetrics) =>
      !query
      || server.name.toLowerCase().includes(query)
      || server.url.toLowerCase().includes(query)

    const folders = new Map<string, ServerWithMetrics[]>()
    usable.filter(matches).forEach(server => {
      const key = server.folder || NO_FOLDER
      if (!folders.has(key)) folders.set(key, [])
      folders.get(key)!.push(server)
    })
    return folders
  }, [usable, search])

  const panelSelected = value.includes('panel')
  const nodeIds = usable.map(server => `node:${server.id}`)
  const selectedNodes = value.filter(item => item !== 'panel')

  const toggle = (location: string) => {
    onChange(
      value.includes(location)
        ? value.filter(item => item !== location)
        : [...value, location],
    )
  }

  const toggleFolder = (name: string) => {
    setExpanded(prev => {
      const next = new Set(prev)
      if (next.has(name)) next.delete(name)
      else next.add(name)
      try {
        localStorage.setItem(EXPANDED_KEY, JSON.stringify([...next]))
      } catch {
        // приватный режим — просто не запомним раскрытые папки
      }
      return next
    })
  }

  const toggleAllNodes = () => {
    onChange(
      selectedNodes.length === nodeIds.length
        ? value.filter(item => item === 'panel')
        : [...value.filter(item => item === 'panel'), ...nodeIds],
    )
  }

  // Во время поиска папки раскрыты: иначе найденное не видно
  const searching = search.trim().length > 0

  return (
    <div className="rounded-lg border border-dark-800/60 overflow-hidden">
      <label
        className={`flex items-center gap-2.5 px-3 py-2.5 cursor-pointer select-none border-b border-dark-800/60 transition-colors ${
          panelSelected ? 'bg-accent-500/[0.08]' : 'hover:bg-dark-800/30'
        }`}
      >
        <Checkbox checked={panelSelected} onChange={() => toggle('panel')} />
        <Monitor className="w-4 h-4 text-dark-400" />
        <span className="text-sm text-dark-200">{t('xray_test.where_panel')}</span>
      </label>

      {usable.length > 0 && (
        <>
          <div className="flex items-center gap-2 px-3 py-2 border-b border-dark-800/60">
            <Search className="w-3.5 h-3.5 text-dark-500 shrink-0" />
            <input
              className="flex-1 bg-transparent text-xs text-dark-200 placeholder-dark-600 outline-none"
              placeholder={t('xray_test.search_servers')}
              value={search}
              onChange={event => setSearch(event.target.value)}
            />
            <button
              className="text-[11px] text-dark-400 hover:text-accent-400 shrink-0"
              onClick={toggleAllNodes}
            >
              {selectedNodes.length === nodeIds.length
                ? t('xray_test.unselect_all')
                : t('xray_test.select_all')}
            </button>
          </div>

          <div className="max-h-56 overflow-auto">
            {[...grouped.entries()].map(([folder, list]) => {
              const isOpen = searching || folder === NO_FOLDER || expanded.has(folder)
              const inFolder = list.map(server => `node:${server.id}`)
              const checkedCount = inFolder.filter(id => value.includes(id)).length

              return (
                <div key={folder}>
                  {folder !== NO_FOLDER && (
                    <div className="flex items-center gap-2 px-3 py-1.5 bg-dark-900/40">
                      <Checkbox
                        checked={checkedCount === inFolder.length && checkedCount > 0}
                        indeterminate={checkedCount > 0 && checkedCount < inFolder.length}
                        onChange={() => onChange(
                          checkedCount === inFolder.length
                            ? value.filter(item => !inFolder.includes(item))
                            : [...new Set([...value, ...inFolder])],
                        )}
                      />
                      <button
                        className="flex items-center gap-1.5 text-xs text-dark-300 hover:text-dark-100"
                        onClick={() => toggleFolder(folder)}
                      >
                        {isOpen
                          ? <FolderOpen className="w-3.5 h-3.5" />
                          : <Folder className="w-3.5 h-3.5" />}
                        {folder}
                        <span className="text-dark-600">({list.length})</span>
                        <ChevronDown
                          className={`w-3 h-3 transition-transform ${isOpen ? '' : '-rotate-90'}`}
                        />
                      </button>
                    </div>
                  )}

                  {isOpen && list.map(server => {
                    const id = `node:${server.id}`
                    const checked = value.includes(id)
                    return (
                      <label
                        key={server.id}
                        className={`flex items-center gap-2.5 px-3 py-2 cursor-pointer select-none transition-colors ${
                          folder !== NO_FOLDER ? 'pl-8' : ''
                        } ${checked ? 'bg-accent-500/[0.06]' : 'hover:bg-dark-800/30'}`}
                      >
                        <Checkbox checked={checked} onChange={() => toggle(id)} />
                        <Server className="w-3.5 h-3.5 text-dark-500 shrink-0" />
                        <span className="flex-1 min-w-0">
                          <span className="block text-xs text-dark-200 truncate">{server.name}</span>
                          <span className="block text-[10px] text-dark-500 font-mono truncate">
                            {server.url.replace(/^https?:\/\//, '')}
                          </span>
                        </span>
                      </label>
                    )
                  })}
                </div>
              )
            })}

            {grouped.size === 0 && (
              <p className="px-3 py-4 text-xs text-dark-500 text-center">
                {t('xray_test.no_servers_found')}
              </p>
            )}
          </div>
        </>
      )}

      {restricted.length > 0 && (
        <p className="px-3 py-2 text-[11px] text-dark-500 border-t border-dark-800/60">
          {t('xray_test.servers_restricted', { count: restricted.length })}
        </p>
      )}
    </div>
  )
}
