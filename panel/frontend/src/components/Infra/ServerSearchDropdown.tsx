import { useState, useRef, useEffect } from 'react'
import { Search, Plus } from 'lucide-react'
import { useTranslation } from 'react-i18next'

interface ServerOption {
  id: number
  name: string
  url: string
}

interface ServerSearchDropdownProps {
  servers: ServerOption[]
  excludeIds: Set<number>
  onSelect: (serverId: number) => void
  onClose: () => void
}

function parseHost(url: string): string {
  const match = url.match(/^https?:\/\/([^:/]+)/)
  return match?.[1] ?? url
}

export default function ServerSearchDropdown({ servers, excludeIds, onSelect, onClose }: ServerSearchDropdownProps) {
  const [query, setQuery] = useState('')
  const inputRef = useRef<HTMLInputElement>(null)
  const containerRef = useRef<HTMLDivElement>(null)
  const { t } = useTranslation()

  useEffect(() => { inputRef.current?.focus() }, [])

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        onClose()
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [onClose])

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
          {filtered.map(s => (
            <button
              key={s.id}
              className="flex items-center gap-2 w-full px-3 py-2 text-left hover:bg-dark-700 transition-colors"
              onClick={() => { onSelect(s.id); onClose() }}
            >
              <Plus className="w-3.5 h-3.5 text-dark-400 shrink-0" />
              <span className="text-sm text-dark-100 truncate">{s.name}</span>
              <span className="text-xs text-dark-500 ml-auto shrink-0">{parseHost(s.url)}</span>
            </button>
          ))}
        </div>
      ) : (
        <div className="absolute z-50 mt-1 w-full bg-dark-800 border border-dark-600 rounded-lg shadow-xl px-3 py-3 text-sm text-dark-400">
          {available.length === 0 ? t('infra.all_servers_assigned') : t('infra.no_results')}
        </div>
      )}
    </div>
  )
}
