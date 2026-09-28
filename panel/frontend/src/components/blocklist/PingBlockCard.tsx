import { useEffect, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import { AlertCircle, EyeOff } from 'lucide-react'
import { blocklistApi, BlocklistSettings, PingBlockMode, PingBlockScope, Server } from '../../api/client'
import { Checkbox } from '../ui/Checkbox'
import FolderedServerPicker from '../servers/FolderedServerPicker'

const MODES: PingBlockMode[] = ['off', 'all', 'selected']

function toggled<T>(items: T[], item: T): T[] {
  return items.includes(item) ? items.filter(i => i !== item) : [...items, item]
}

/** Где серверы не отвечают на ping: нигде, везде или на выбранных папках и серверах */
export default function PingBlockCard({ servers }: { servers: Server[] }) {
  const { t } = useTranslation()
  const [settings, setSettings] = useState<BlocklistSettings | null>(null)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    blocklistApi.getSettings()
      .then(response => setSettings(response.data))
      .catch(err => console.error('Failed to fetch blocklist settings:', err))
  }, [])

  const scope = settings?.ping_block
  const activeServers = useMemo(() => servers.filter(s => s.is_active), [servers])
  const folders = useMemo(() => new Set(scope?.folders ?? []), [scope])
  const serverIds = useMemo(() => new Set(scope?.server_ids ?? []), [scope])

  const coversServer = (server: Server) =>
    serverIds.has(server.id) || (!!server.folder && folders.has(server.folder))
  const coveredCount = activeServers.filter(coversServer).length

  // Сохраняем сразу; пока запрос идёт, выбор заблокирован — иначе ответы
  // пришли бы не по порядку и старый затёр бы свежий выбор
  const save = async (next: PingBlockScope, announce = false) => {
    setSaving(true)
    try {
      const response = await blocklistApi.updateSettings({ ping_block: next })
      setSettings(response.data)
      if (announce) toast.success(t(`blocklist.ping_mode_${next.mode}_saved`))
    } catch (err) {
      console.error('Failed to update ping block:', err)
      toast.error(t('common.action_failed'))
    } finally {
      setSaving(false)
    }
  }

  const disabled = !scope || saving

  return (
    <div className="card">
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div className="min-w-0 flex-1">
          <h3 className="text-sm font-semibold text-dark-100 flex items-center gap-2">
            <EyeOff className="w-4 h-4 text-accent-400" />
            {t('blocklist.block_ping')}
          </h3>
          <p className="text-xs text-dark-400 mt-1">{t('blocklist.block_ping_hint')}</p>
        </div>
        <div className="flex gap-1.5 shrink-0">
          {MODES.map(mode => (
            <button
              key={mode}
              onClick={() => scope && scope.mode !== mode && save({ ...scope, mode }, true)}
              disabled={disabled}
              className={`px-3 py-1.5 rounded-lg text-sm font-medium transition-all disabled:opacity-60 ${
                scope?.mode === mode
                  ? 'bg-accent-500 text-dark-950'
                  : 'text-dark-400 hover:text-dark-200 bg-dark-800 border border-dark-700'
              }`}
            >
              {t(`blocklist.ping_mode_${mode}`)}
            </button>
          ))}
        </div>
      </div>

      {scope?.mode === 'selected' && (
        <div className="mt-4">
          <p className="text-xs text-dark-400 mb-2">
            {t('blocklist.ping_selected_hint', { count: coveredCount, total: activeServers.length })}
          </p>
          <FolderedServerPicker
            servers={activeServers}
            autoFocus={false}
            storageKey="blocklist_ping_expanded_folders"
            labels={{
              searchPlaceholder: t('bulk_actions.search_servers'),
              empty: t('bulk_actions.no_results'),
              noFolder: t('bulk_actions.no_folder'),
            }}
            renderFolderAction={(folder, members) => (
              <Checkbox
                checked={folders.has(folder)}
                indeterminate={!folders.has(folder) && members.some(m => serverIds.has(m.id))}
                onChange={() => save({ ...scope, folders: toggled(scope.folders, folder) })}
                disabled={disabled}
              />
            )}
            renderServer={server => {
              // Папка выбрана целиком — сервер закрыт через неё, отдельно не снимается
              const viaFolder = !!server.folder && folders.has(server.folder)
              return (
                <label
                  key={server.id}
                  className="flex items-center gap-2 px-2 py-1.5 rounded-lg hover:bg-dark-800/50 cursor-pointer"
                >
                  <Checkbox
                    checked={coversServer(server)}
                    onChange={() => save({ ...scope, server_ids: toggled(scope.server_ids, server.id) })}
                    disabled={disabled || viaFolder}
                  />
                  <span className="text-sm text-dark-200 truncate">{server.name}</span>
                </label>
              )
            }}
          />
        </div>
      )}

      {settings && settings.ping_block.mode !== 'off' && settings.outdated_servers.length > 0 && (
        <p className="text-xs text-amber-400 mt-3 flex items-start gap-1.5">
          <AlertCircle className="w-3.5 h-3.5 shrink-0 mt-0.5" />
          {t('blocklist.block_ping_outdated', {
            version: settings.min_node_version,
            servers: settings.outdated_servers.join(', '),
          })}
        </p>
      )}
    </div>
  )
}
