import { useMemo, useState, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { AlertTriangle, Server } from 'lucide-react'
import type { HAProxyAvailableServer } from '../../api/client'
import { Checkbox } from '../ui/Checkbox'
import FolderedServerPicker from '../servers/FolderedServerPicker'

interface Props {
  /** Все серверы панели: привязанные к этому профилю компонент отсеивает сам */
  servers: HAProxyAvailableServer[]
  profileId: number
  profileNames: ReadonlyMap<number, string>
  renderBadge?: (server: HAProxyAvailableServer) => ReactNode
  onLink: (serverId: number) => void
}

/**
 * Выбор сервера для привязки к профилю. Серверы других профилей скрыты, пока их не
 * попросят показать: привязка забирает сервер у прежнего профиля и сразу меняет ему конфиг
 */
export default function LinkServerPicker({ servers, profileId, profileNames, renderBadge, onLink }: Props) {
  const { t } = useTranslation()
  const [linkedRequested, setLinkedRequested] = useState(false)

  const candidates = useMemo(() => servers.filter(server => server.active_profile_id !== profileId), [servers, profileId])
  const free = useMemo(() => candidates.filter(server => server.active_profile_id === null), [candidates])
  const linkedCount = candidates.length - free.length
  const showLinked = linkedRequested && linkedCount > 0

  const profileName = (id: number) => profileNames.get(id) ?? `#${id}`

  const pick = (server: HAProxyAvailableServer) => {
    if (server.active_profile_id !== null) {
      const question = t('haproxy_configs.relink_confirm', { server: server.name, profile: profileName(server.active_profile_id) })
      if (!confirm(question)) return
    }
    onLink(server.id)
  }

  const renderServer = (server: HAProxyAvailableServer) => (
    <button key={server.id} type="button" onClick={() => pick(server)}
      className="w-full flex items-center justify-between gap-3 px-3 py-2 rounded-lg text-sm text-dark-200 hover:bg-dark-700/50 transition-colors">
      <span className="flex items-center gap-2 min-w-0">
        <Server className="w-3.5 h-3.5 text-dark-400 shrink-0" />
        <span className="truncate">{server.name}</span>
        {renderBadge?.(server)}
      </span>
      {server.active_profile_id !== null && (
        <span className="text-xs text-amber-400/80 truncate max-w-[50%]">
          {t('haproxy_configs.in_profile', { name: profileName(server.active_profile_id) })}
        </span>
      )}
    </button>
  )

  return (
    <div className="rounded-lg border border-dark-700/50 bg-dark-900/50 p-3">
      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1.5 mb-2">
        <span className="text-xs text-dark-400">{t('haproxy_configs.select_server')}</span>
        <label className={`flex items-center gap-2 text-xs select-none ${linkedCount > 0 ? 'text-dark-300 cursor-pointer' : 'text-dark-500'}`}>
          <Checkbox checked={showLinked} disabled={linkedCount === 0} tone="warning"
            onChange={event => setLinkedRequested(event.target.checked)} />
          {t('haproxy_configs.show_linked')}
          <span className="text-dark-500 tabular-nums">{linkedCount}</span>
        </label>
      </div>
      {showLinked && (
        <div className="flex items-start gap-2 p-2.5 mb-2 rounded-lg bg-amber-500/10 border border-amber-500/30 text-amber-200 text-xs">
          <AlertTriangle className="w-4 h-4 shrink-0 text-amber-400" />
          <span>{t('haproxy_configs.relink_warning')}</span>
        </div>
      )}
      <FolderedServerPicker
        servers={showLinked ? candidates : free}
        renderServer={renderServer}
        storageKey="haproxy_add_expanded_folders"
        labels={{
          searchPlaceholder: t('haproxy_configs.search_server'),
          empty: linkedCount > 0 && !showLinked ? t('haproxy_configs.no_free_servers') : t('haproxy_configs.no_available_servers'),
          noFolder: t('bulk_actions.no_folder'),
        }}
      />
    </div>
  )
}
