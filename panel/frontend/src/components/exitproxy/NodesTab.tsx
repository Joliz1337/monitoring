import { useMemo, useState } from 'react'
import { Folder, Search } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import type { ExitProxyNode } from '../../api/client'
import { useExitProxyStore } from '../../stores/exitProxyStore'
import { orderFolders } from '../../utils/folders'
import NodeRow from './NodeRow'
import SnippetBlock from './SnippetBlock'

const NO_FOLDER = '__no_folder__'

export default function NodesTab() {
  const { t } = useTranslation()
  const nodes = useExitProxyStore(s => s.nodes)
  const status = useExitProxyStore(s => s.status)
  const [search, setSearch] = useState('')
  const [onlyEnabled, setOnlyEnabled] = useState(false)
  const [open, setOpen] = useState<Set<number>>(new Set())

  const groups = useMemo(() => {
    const query = search.trim().toLowerCase()
    const visible = nodes.filter(node =>
      (!onlyEnabled || node.enabled)
      && (!query || node.name.toLowerCase().includes(query) || (node.folder ?? '').toLowerCase().includes(query)),
    )
    const byFolder = new Map<string, ExitProxyNode[]>()
    for (const node of visible) {
      const key = node.folder || NO_FOLDER
      byFolder.set(key, [...(byFolder.get(key) ?? []), node])
    }
    const named = orderFolders([...byFolder.keys()].filter(key => key !== NO_FOLDER))
    const ordered = byFolder.has(NO_FOLDER) ? [...named, NO_FOLDER] : named
    return ordered.map(key => ({ key, nodes: byFolder.get(key) ?? [] }))
  }, [nodes, search, onlyEnabled])

  const enabledCount = nodes.filter(node => node.enabled).length
  const activeCount = nodes.filter(node => node.install_status === 'active').length
  const toggleOpen = (id: number) => setOpen(prev => {
    const next = new Set(prev)
    if (next.has(id)) next.delete(id)
    else next.add(id)
    return next
  })

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <div className="relative flex-1 min-w-[220px]">
          <Search className="w-4 h-4 text-dark-500 absolute left-3 top-1/2 -translate-y-1/2" />
          <input
            value={search}
            onChange={e => setSearch(e.target.value)}
            placeholder={t('exit_proxy.search_placeholder')}
            className="input pl-9 w-full"
          />
        </div>
        <button
          onClick={() => setOnlyEnabled(v => !v)}
          className={`px-3 py-1.5 rounded-lg text-xs border transition-colors ${onlyEnabled ? 'bg-accent-500/15 border-accent-500/40 text-accent-400' : 'border-dark-700 text-dark-400 hover:text-dark-200'}`}
        >
          {t('exit_proxy.only_enabled')}
        </button>
        <span className="text-xs text-dark-500">{t('exit_proxy.counts', { enabled: enabledCount, active: activeCount })}</span>
      </div>

      {nodes.length === 0 ? (
        <div className="card p-8 text-center text-sm text-dark-400">{t('exit_proxy.no_nodes')}</div>
      ) : enabledCount === 0 ? (
        <div className="bg-amber-500/10 border border-amber-500/20 rounded-lg p-3 text-xs text-amber-300">{t('exit_proxy.no_enabled_nodes')}</div>
      ) : null}

      {groups.map(group => (
        <div key={group.key} className="card overflow-hidden">
          <div className="flex items-center gap-2 px-4 py-2 bg-dark-800/40 border-b border-dark-800 text-xs text-dark-400">
            <Folder className="w-3.5 h-3.5" />
            <span className="font-medium text-dark-300">{group.key === NO_FOLDER ? t('exit_proxy.no_folder') : group.key}</span>
            <span>· {group.nodes.length}</span>
          </div>
          <div className="divide-y divide-dark-800">
            {group.nodes.map(node => (
              <NodeRow
                key={node.server_id}
                node={node}
                open={open.has(node.server_id)}
                onToggleOpen={() => toggleOpen(node.server_id)}
                minNodeVersion={status?.min_node_version ?? ''}
              />
            ))}
          </div>
        </div>
      ))}

      <SnippetBlock />
    </div>
  )
}
