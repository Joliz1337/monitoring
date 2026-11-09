import { useEffect, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { AlertTriangle, Server as ServerIcon, X } from 'lucide-react'
import type { Server, TorrentBlockerServerMode, TorrentBlockerServerRule } from '../../api/client'
import { nodeAllows } from '../../utils/nodeCapabilities'
import { ServerAddDropdown } from '../servers/ServerAddDropdown'
import { Tooltip } from '../ui/Tooltip'

const MODES: TorrentBlockerServerMode[] = ['exclude', 'include']
// Зеркало MAX_PORT_BAN_PORTS панели и ноды: ядро раскладывает диапазон на отдельные порты
const MAX_PORTS = 64
const PORT_SPEC_RE = /^(\d{1,5})(?:-(\d{1,5}))?$/

type PortsParse =
  | { ports: string[]; error: null }
  | { ports: null; error: 'empty' | 'invalid' | 'too_many'; value?: string }

function parsePorts(text: string): PortsParse {
  const tokens = text.split(/[\s,;]+/).filter(Boolean)
  if (tokens.length === 0) return { ports: null, error: 'empty' }
  const ranges = new Map<string, [number, number]>()
  for (const token of tokens) {
    const match = PORT_SPEC_RE.exec(token)
    const start = match ? Number(match[1]) : NaN
    const end = match ? Number(match[2] ?? match[1]) : NaN
    if (!match || start < 1 || end > 65535 || start > end) return { ports: null, error: 'invalid', value: token }
    ranges.set(`${start}-${end}`, [start, end])
  }
  const sorted = [...ranges.values()].sort((a, b) => a[0] - b[0] || a[1] - b[1])
  const total = sorted.reduce((sum, [start, end]) => sum + end - start + 1, 0)
  if (total > MAX_PORTS) return { ports: null, error: 'too_many' }
  return { ports: sorted.map(([start, end]) => (start === end ? String(start) : `${start}-${end}`)), error: null }
}

interface RulePopoverProps {
  mode: TorrentBlockerServerMode
  serverName: string
  rule: TorrentBlockerServerRule
  portBanSupported: boolean
  minNodeVersion: string
  onApply: (ports: string[]) => void
  onClose: () => void
}

function RulePopover({ mode, serverName, rule, portBanSupported, minNodeVersion, onApply, onClose }: RulePopoverProps) {
  const { t } = useTranslation()
  const [portsOnly, setPortsOnly] = useState(rule.ports.length > 0)
  const [text, setText] = useState(rule.ports.join(', '))
  const parsed = useMemo(() => parsePorts(text), [text])
  const showError = parsed.error !== null && text.trim() !== ''

  const canApply = !portsOnly || parsed.ports !== null
  const apply = () => {
    if (!canApply) return
    onApply(portsOnly && parsed.ports ? parsed.ports : [])
    onClose()
  }

  const option = (active: boolean, disabled: boolean, label: string, onSelect: () => void) => (
    <button
      type="button"
      disabled={disabled}
      onClick={onSelect}
      className="w-full flex items-center gap-2 px-2 py-1.5 rounded-md text-left text-sm text-dark-200 hover:bg-dark-800 transition-colors disabled:opacity-50 disabled:hover:bg-transparent"
    >
      <span className={`w-3.5 h-3.5 rounded-full border-2 shrink-0 flex items-center justify-center ${active ? 'border-accent-500' : 'border-dark-600'}`}>
        {active && <span className="w-1.5 h-1.5 rounded-full bg-accent-500" />}
      </span>
      {label}
    </button>
  )

  return (
    <div
      className="absolute left-0 top-full z-30 mt-1 w-72 max-w-[calc(100vw-2rem)] bg-dark-900 border border-dark-700 rounded-lg shadow-xl p-3 space-y-2"
    >
      <div className="text-sm font-medium text-dark-100 truncate">{serverName}</div>
      {option(!portsOnly, false, t(`torrent_blocker.rule_default_${mode}`), () => setPortsOnly(false))}
      <Tooltip label={t('torrent_blocker.ports_need_node', { version: minNodeVersion })} disabled={portBanSupported}>
        <span className="block">
          {option(portsOnly, !portBanSupported && !portsOnly, t('torrent_blocker.rule_ports'), () => setPortsOnly(true))}
        </span>
      </Tooltip>
      {portsOnly && (
        <div className="pl-7 space-y-1">
          <input
            type="text"
            autoFocus
            value={text}
            onChange={e => setText(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') apply() }}
            placeholder="8443, 2053"
            className="w-full bg-dark-800 border border-dark-700 rounded-md px-2 py-1 text-sm font-mono text-dark-200 focus:border-accent-500 focus:outline-none"
          />
          <p className={`text-xs ${showError ? 'text-red-400' : 'text-dark-500'}`}>
            {showError
              ? t(`torrent_blocker.ports_error_${parsed.error}`, { value: parsed.value, max: MAX_PORTS })
              : t('torrent_blocker.ports_hint', { max: MAX_PORTS })}
          </p>
        </div>
      )}
      <button
        type="button"
        onClick={apply}
        disabled={!canApply}
        className="w-full bg-accent-500 hover:bg-accent-600 text-white rounded-md px-3 py-1.5 text-xs font-medium transition-colors disabled:opacity-50"
      >
        {t('torrent_blocker.rule_apply')}
      </button>
    </div>
  )
}

interface Props {
  mode: TorrentBlockerServerMode
  excluded: TorrentBlockerServerRule[]
  included: TorrentBlockerServerRule[]
  servers: Server[]
  portBanUnsupportedIds: number[]
  minNodeVersion: string
  onModeChange: (mode: TorrentBlockerServerMode) => void
  onRulesChange: (mode: TorrentBlockerServerMode, rules: TorrentBlockerServerRule[]) => void
}

/**
 * Где банить: переключатель «везде, кроме / только на выбранных» и список серверов
 * текущего режима. Клик по серверу — способ бана на нём: режим по умолчанию или только порты.
 */
export function BanTargetList({
  mode, excluded, included, servers, portBanUnsupportedIds, minNodeVersion, onModeChange, onRulesChange,
}: Props) {
  const { t } = useTranslation()
  const [openId, setOpenId] = useState<number | null>(null)
  // Чип и его поповер — одно целое: клик по открытому чипу закрывает поповер, а не открывает заново
  const openRef = useRef<HTMLSpanElement>(null)
  const rules = mode === 'exclude' ? excluded : included

  useEffect(() => {
    if (openId === null) return
    const handleClick = (e: MouseEvent) => {
      if (openRef.current && !openRef.current.contains(e.target as Node)) setOpenId(null)
    }
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpenId(null)
    }
    document.addEventListener('mousedown', handleClick)
    document.addEventListener('keydown', handleKey)
    return () => {
      document.removeEventListener('mousedown', handleClick)
      document.removeEventListener('keydown', handleKey)
    }
  }, [openId])
  const names = useMemo(() => new Map(servers.map(s => [s.id, s.name])), [servers])
  const unsupported = useMemo(() => new Set(portBanUnsupportedIds), [portBanUnsupportedIds])

  const addServers = (ids: number[]) => {
    const present = new Set(rules.map(r => r.id))
    const added = ids.filter(id => !present.has(id)).map(id => ({ id, ports: [] }))
    onRulesChange(mode, [...rules, ...added])
  }
  const removeServer = (id: number) => onRulesChange(mode, rules.filter(r => r.id !== id))
  const setPorts = (id: number, ports: string[]) =>
    onRulesChange(mode, rules.map(r => (r.id === id ? { ...r, ports } : r)))

  const switchMode = (next: TorrentBlockerServerMode) => {
    setOpenId(null)
    if (next !== mode) onModeChange(next)
  }

  return (
    <div className="space-y-2">
      <label className="text-sm text-dark-300">{t('torrent_blocker.ban_targets')}</label>
      <div className="flex items-center gap-1 bg-dark-800/60 border border-dark-700 rounded-lg p-0.5 w-fit">
        {MODES.map(item => (
          <button
            key={item}
            type="button"
            onClick={() => switchMode(item)}
            className={`px-3 py-1 rounded-md text-xs font-medium transition-colors ${mode === item ? 'bg-accent-500 text-white' : 'text-dark-400 hover:text-dark-200'}`}
          >
            {t(`torrent_blocker.mode_${item}`)}
          </button>
        ))}
      </div>
      <p className="text-xs text-dark-500">{t(`torrent_blocker.mode_${mode}_hint`)}</p>

      {rules.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {rules.map(rule => {
            const name = names.get(rule.id) || `#${rule.id}`
            const supported = !unsupported.has(rule.id)
            return (
              <span key={rule.id} ref={openId === rule.id ? openRef : undefined} className="relative">
                <span
                  className={`inline-flex items-center gap-1 px-2 py-0.5 bg-dark-800 border rounded-md text-xs text-dark-300 ${openId === rule.id ? 'border-accent-500' : 'border-dark-700'}`}
                >
                  <button
                    type="button"
                    onClick={() => setOpenId(openId === rule.id ? null : rule.id)}
                    className="inline-flex items-center gap-1 hover:text-dark-100"
                  >
                    <ServerIcon className="w-3.5 h-3.5 text-dark-500" />
                    {name}
                    {rule.ports.length > 0 && (
                      <span className="px-1.5 rounded bg-accent-500/15 text-accent-400 font-mono max-w-[10rem] truncate">
                        {rule.ports.join(', ')}
                      </span>
                    )}
                  </button>
                  {rule.ports.length > 0 && !supported && (
                    <Tooltip label={t('torrent_blocker.ports_need_node', { version: minNodeVersion })}>
                      <AlertTriangle className="w-3.5 h-3.5 text-amber-400" />
                    </Tooltip>
                  )}
                  <Tooltip label={t('common.remove_from_list')}>
                    <button
                      type="button"
                      onClick={() => { setOpenId(null); removeServer(rule.id) }}
                      className="text-dark-500 hover:text-dark-300 ml-0.5"
                    >
                      <X className="w-4 h-4" />
                    </button>
                  </Tooltip>
                </span>
                {openId === rule.id && (
                  <RulePopover
                    mode={mode}
                    serverName={name}
                    rule={rule}
                    portBanSupported={supported}
                    minNodeVersion={minNodeVersion}
                    onApply={ports => setPorts(rule.id, ports)}
                    onClose={() => setOpenId(null)}
                  />
                )}
              </span>
            )
          })}
        </div>
      )}

      <ServerAddDropdown
        servers={servers}
        excludeIds={rules.map(r => r.id)}
        onAdd={addServers}
        storageKey="torrent_excluded_expanded_folders"
        isRestricted={server => !nodeAllows(server, 'ipset', 'write')}
        labels={{
          placeholder: t('torrent_blocker.search_servers'),
          noFolder: t('torrent_blocker.no_folder'),
          addFolder: t(`torrent_blocker.add_folder_${mode}`),
          allAdded: servers.length === 0 ? t('torrent_blocker.no_servers') : t('torrent_blocker.all_added'),
          noResults: t('common.no_results'),
          clearSearch: t('common.clear_search'),
        }}
      />
    </div>
  )
}
