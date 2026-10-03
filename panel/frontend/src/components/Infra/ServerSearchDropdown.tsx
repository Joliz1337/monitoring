import { useState, useRef, useEffect, type RefObject } from 'react'
import { Search, Plus, Loader2 } from 'lucide-react'
import { useTranslation } from 'react-i18next'

interface ServerOption {
  id: number
  name: string
  url: string
}

interface ServerSearchDropdownProps {
  servers: ServerOption[]
  excludeIds: Set<number>
  onSelect: (serverId: number) => Promise<void>
  onClose: () => void
  /** Кнопка, открывающая поиск: её нажатие закрывает поиск сама, а не как клик снаружи */
  toggleRef?: RefObject<HTMLElement>
}

function parseHost(url: string): string {
  const match = url.match(/^https?:\/\/([^:/]+)/)
  return match?.[1] ?? url
}

export default function ServerSearchDropdown({ servers, excludeIds, onSelect, onClose, toggleRef }: ServerSearchDropdownProps) {
  const [query, setQuery] = useState('')
  const [addingIds, setAddingIds] = useState<ReadonlySet<number>>(new Set())
  const inputRef = useRef<HTMLInputElement>(null)
  const containerRef = useRef<HTMLDivElement>(null)
  const { t } = useTranslation()

  useEffect(() => { inputRef.current?.focus() }, [])

  useEffect(() => {
    // Без исключения mousedown по кнопке-переключателю закрыл бы поиск, а её click тут же открыл бы снова
    const handler = (e: MouseEvent) => {
      const target = e.target as Node
      if (containerRef.current?.contains(target) || toggleRef?.current?.contains(target)) return
      onClose()
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [onClose, toggleRef])

  // Пока ждём API, строка остаётся на месте неактивной: дерево обновится только после ответа,
  // а спрятанная сразу строка подставила бы под курсор соседнюю — двойной клик добавлял бы и её
  const handleSelect = async (serverId: number) => {
    if (addingIds.has(serverId)) return
    setAddingIds(prev => new Set(prev).add(serverId))
    inputRef.current?.focus()
    try {
      await onSelect(serverId)
    } finally {
      setAddingIds(prev => {
        const next = new Set(prev)
        next.delete(serverId)
        return next
      })
    }
  }

  const available = servers.filter(s => !excludeIds.has(s.id))
  const q = query.toLowerCase()
  const filtered = q
    ? available.filter(s => s.name.toLowerCase().includes(q) || parseHost(s.url).includes(q))
    : available

  return (
    <div ref={containerRef} className="relative w-full max-w-sm">
      <div className="flex items-center gap-2 bg-dark-800 border border-dark-600 rounded-lg px-3 py-2">
        <Search className="w-4 h-4 text-dark-400 shrink-0" />
        <input
          ref={inputRef}
          value={query}
          onChange={e => setQuery(e.target.value)}
          placeholder={t('infra.search_server')}
          className="bg-transparent text-sm text-dark-100 placeholder:text-dark-500 outline-none flex-1"
          onKeyDown={e => e.key === 'Escape' && onClose()}
        />
      </div>

      {filtered.length > 0 ? (
        <div className="absolute z-50 mt-1 w-full max-h-48 overflow-y-auto bg-dark-800 border border-dark-600 rounded-lg shadow-xl">
          {filtered.map(s => {
            const isAdding = addingIds.has(s.id)
            return (
              <button
                key={s.id}
                aria-disabled={isAdding}
                className={`flex items-center gap-2 w-full px-3 py-2 text-left transition-colors ${isAdding ? 'opacity-50 cursor-default' : 'hover:bg-dark-700'}`}
                // Фокус остаётся в поиске: можно сразу дописать запрос и добавить следующий сервер.
                // Поэтому и не disabled — React не отдаёт ему mousedown, и браузер снимал бы фокус
                onMouseDown={e => e.preventDefault()}
                onClick={() => handleSelect(s.id)}
              >
                {isAdding
                  ? <Loader2 className="w-3.5 h-3.5 text-dark-400 shrink-0 animate-spin" />
                  : <Plus className="w-3.5 h-3.5 text-dark-400 shrink-0" />}
                <span className="text-sm text-dark-100 truncate">{s.name}</span>
                <span className="text-xs text-dark-500 ml-auto shrink-0">{parseHost(s.url)}</span>
              </button>
            )
          })}
        </div>
      ) : (
        <div className="absolute z-50 mt-1 w-full bg-dark-800 border border-dark-600 rounded-lg shadow-xl px-3 py-3 text-sm text-dark-400">
          {available.length === 0 ? t('infra.all_servers_assigned') : t('infra.no_results')}
        </div>
      )}
    </div>
  )
}
