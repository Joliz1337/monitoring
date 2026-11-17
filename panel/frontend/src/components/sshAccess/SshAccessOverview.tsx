import { useState } from 'react'
import type { TFunction } from 'i18next'
import { useTranslation } from 'react-i18next'
import { Trash2, X } from 'lucide-react'
import type { SshAccessLevel, SshAccessOverview as Overview, SshAccessPart, SshAccessTarget } from '../../api/client'

export const ALL_ACCESS_PARTS: SshAccessPart[] = ['login', 'port', 'proxy']
const WITHOUT_LOGIN_NAMES_SHOWN = 8

function loginText(level: SshAccessLevel, t: TFunction): string | null {
  if (!level.auth) return level.ssh_user
  const method = t(level.auth === 'key' ? 'sshAccess.part_key' : 'sshAccess.part_password')
  return `${level.ssh_user || 'root'} · ${method}`
}

function proxyText(level: SshAccessLevel, t: TFunction): string | null {
  if (level.proxy_mode === 'off') return t('sshAccess.part_proxy_off')
  if (!level.proxy) return null
  const mode = t(level.proxy_mode === 'always' ? 'sshAccess.mode_always_short' : 'sshAccess.mode_fallback_short')
  return `${t('sshAccess.part_proxy', { proxy: level.proxy })} (${mode})`
}

interface LevelRowProps {
  label: string
  note?: string
  level: SshAccessLevel
  target: SshAccessTarget
  disabled: boolean
  onClear: (target: SshAccessTarget, parts: SshAccessPart[]) => void
}

function LevelRow({ label, note, level, target, disabled, onClear }: LevelRowProps) {
  const { t } = useTranslation()
  const port = level.ssh_port ? t('sshAccess.part_port', { port: level.ssh_port }) : null
  const parts: { part: SshAccessPart; text: string | null }[] = [
    { part: 'login', text: loginText(level, t) },
    { part: 'port', text: port },
    { part: 'proxy', text: proxyText(level, t) },
  ]

  return (
    <li className="flex items-center gap-2 flex-wrap px-3 py-2 rounded-lg bg-dark-800/40 border border-dark-700/40">
      <span className="text-sm font-medium text-dark-100">{label}</span>
      {parts.map(({ part, text }) => text && (
        <span key={part} className="inline-flex items-center gap-1 text-xs text-dark-300 bg-dark-700/50 rounded-md pl-2 pr-1 py-0.5">
          <span>{text}</span>
          <button
            type="button"
            aria-label={t(`sshAccess.remove_${part}`)}
            title={t(`sshAccess.remove_${part}`)}
            onClick={() => onClear(target, [part])}
            disabled={disabled}
            className="text-dark-500 hover:text-danger disabled:opacity-50"
          >
            <X className="w-3 h-3" />
          </button>
        </span>
      ))}
      {note && <span className="text-xs text-warning">{note}</span>}
      <button
        type="button"
        aria-label={t('sshAccess.remove_all')}
        title={t('sshAccess.remove_all')}
        onClick={() => onClear(target, ALL_ACCESS_PARTS)}
        disabled={disabled}
        className="ml-auto text-dark-500 hover:text-danger disabled:opacity-50"
      >
        <Trash2 className="w-4 h-4" />
      </button>
    </li>
  )
}

interface Props {
  overview: Overview
  disabled: boolean
  onClear: (target: SshAccessTarget, parts: SshAccessPart[]) => void
}

/** Блок «Сейчас»: что задано на каждом уровне и откуда серверы берут вход */
export default function SshAccessOverview({ overview, disabled, onClear }: Props) {
  const { t } = useTranslation()
  const [serversShown, setServersShown] = useState(false)

  const nothingSet = !overview.global && overview.folders.length === 0 && overview.servers.length === 0
  const namedWithoutLogin = overview.without_login.slice(0, WITHOUT_LOGIN_NAMES_SHOWN).map(s => s.name).join(', ')
  const moreWithoutLogin = overview.without_login_total - Math.min(overview.without_login.length, WITHOUT_LOGIN_NAMES_SHOWN)

  return (
    <div className="space-y-2">
      {nothingSet && <p className="text-sm text-dark-400">{t('sshAccess.nothing_set')}</p>}

      <ul className="space-y-1.5">
        {overview.global && (
          <LevelRow
            label={t('sshAccess.level_global')}
            level={overview.global}
            target={{ scope: 'global' }}
            disabled={disabled}
            onClear={onClear}
          />
        )}
        {overview.folders.map(level => (
          <LevelRow
            key={level.folder}
            label={t('sshAccess.level_folder', { folder: level.folder })}
            note={level.servers === 0 ? t('sshAccess.folder_empty') : undefined}
            level={level}
            target={{ scope: 'folders', folders: [level.folder] }}
            disabled={disabled}
            onClear={onClear}
          />
        ))}
      </ul>

      {overview.servers.length > 0 && (
        <div>
          <button
            type="button"
            onClick={() => setServersShown(shown => !shown)}
            className="text-sm text-dark-300 hover:text-dark-100"
          >
            {t('sshAccess.level_servers', { count: overview.servers.length })}
            <span className="text-accent-400 ml-2">{t(serversShown ? 'sshAccess.hide' : 'sshAccess.show')}</span>
          </button>
          {serversShown && (
            <ul className="space-y-1.5 mt-1.5 max-h-48 overflow-y-auto pr-1">
              {overview.servers.map(level => (
                <LevelRow
                  key={level.server_id}
                  label={level.name}
                  level={level}
                  target={{ scope: 'servers', server_ids: [level.server_id] }}
                  disabled={disabled}
                  onClear={onClear}
                />
              ))}
            </ul>
          )}
        </div>
      )}

      <p className="text-xs text-dark-400">{t('sshAccess.summary', overview.summary)}</p>
      {overview.without_login_total > 0 && (
        <p className="text-xs text-warning break-words">
          {t('sshAccess.without_login', { names: namedWithoutLogin })}
          {moreWithoutLogin > 0 && ` ${t('sshAccess.and_more', { count: moreWithoutLogin })}`}
        </p>
      )}
    </div>
  )
}
