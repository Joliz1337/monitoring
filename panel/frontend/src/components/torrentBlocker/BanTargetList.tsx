import { useEffect, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { AnimatePresence, motion } from 'framer-motion'
import {
  AlertTriangle, ChevronDown, Folder, FolderOpen, Server as ServerIcon, SlidersHorizontal, Trash2, X,
} from 'lucide-react'
import type {
  Server, TorrentBlockerFolderRule, TorrentBlockerServerMode, TorrentBlockerServerRule,
} from '../../api/client'
import { groupByFolder, orderFolders } from '../../utils/folders'
import { nodeAllows } from '../../utils/nodeCapabilities'
import { writeStorage } from '../../utils/storage'
import { ServerAddDropdown } from '../servers/ServerAddDropdown'
import { Tooltip } from '../ui/Tooltip'

const MODES: TorrentBlockerServerMode[] = ['exclude', 'include']
// Зеркало MAX_PORT_BAN_PORTS панели и ноды: ядро раскладывает диапазон на отдельные порты
const MAX_PORTS = 64
const PORT_SPEC_RE = /^(\d{1,5})(?:-(\d{1,5}))?$/
const EXPANDED_KEY = 'torrent_ban_list_expanded'
const NO_FOLDER = '__no_folder__'
const NAMES_IN_WARNING = 5

export interface BanList {
  servers: TorrentBlockerServerRule[]
  folders: TorrentBlockerFolderRule[]
}

interface Entry {
  id: number
  name: string
  address: string
  folder: string | null
  rule: TorrentBlockerServerRule
}

type Choice = 'folder' | 'default' | 'ports'

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

const hasOwnRule = (rule: TorrentBlockerServerRule) => rule.override || rule.ports.length > 0
const samePorts = (a: string[], b: string[]) => a.join(',') === b.join(',')

function readExpanded(): Set<string> {
  try {
    const raw = localStorage.getItem(EXPANDED_KEY)
    return raw ? new Set(JSON.parse(raw)) : new Set()
  } catch {
    return new Set()
  }
}

interface RulePopoverProps {
  title: string
  note?: string
  choices: { value: Choice; label: string }[]
  initialChoice: Choice
  initialPorts: string[]
  portBanSupported: boolean
  minNodeVersion: string
  align: 'left' | 'right'
  onApply: (choice: Choice, ports: string[]) => void
  onClose: () => void
}

function RulePopover({
  title, note, choices, initialChoice, initialPorts, portBanSupported, minNodeVersion, align, onApply, onClose,
}: RulePopoverProps) {
  const { t } = useTranslation()
  const [choice, setChoice] = useState<Choice>(initialChoice)
  const [text, setText] = useState(initialPorts.join(', '))
  const parsed = useMemo(() => parsePorts(text), [text])
  const showError = parsed.error !== null && text.trim() !== ''

  const canApply = choice !== 'ports' || parsed.ports !== null
  const apply = () => {
    if (!canApply) return
    onApply(choice, choice === 'ports' && parsed.ports ? parsed.ports : [])
    onClose()
  }

  const renderChoice = (value: Choice, label: string) => {
    const active = choice === value
    const disabled = value === 'ports' && !portBanSupported && initialChoice !== 'ports'
    const button = (
      <button
        type="button"
        disabled={disabled}
        onClick={() => setChoice(value)}
        className="w-full flex items-center gap-2 px-2 py-1.5 rounded-md text-left text-sm text-dark-200 hover:bg-dark-800 transition-colors disabled:opacity-50 disabled:hover:bg-transparent"
      >
        <span className={`w-3.5 h-3.5 rounded-full border-2 shrink-0 flex items-center justify-center ${active ? 'border-accent-500' : 'border-dark-600'}`}>
          {active && <span className="w-1.5 h-1.5 rounded-full bg-accent-500" />}
        </span>
        <span className="truncate">{label}</span>
      </button>
    )
    if (value !== 'ports') return <div key={value}>{button}</div>
    return (
      <Tooltip key={value} label={t('torrent_blocker.ports_need_node', { version: minNodeVersion })} disabled={portBanSupported}>
        <span className="block">{button}</span>
      </Tooltip>
    )
  }

  return (
    <div
      className={`absolute top-full z-30 mt-1 w-72 max-w-[calc(100vw-2rem)] bg-dark-900 border border-dark-700 rounded-lg shadow-xl p-3 space-y-2 ${align === 'right' ? 'right-0' : 'left-0'}`}
    >
      <div>
        <div className="text-sm font-medium text-dark-100 truncate">{title}</div>
        {note && <p className="text-xs text-dark-500 mt-0.5">{note}</p>}
      </div>
      {choices.map(item => renderChoice(item.value, item.label))}
      {choice === 'ports' && (
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

function PortsBadge({ ports, inherited }: { ports: string[]; inherited?: boolean }) {
  return (
    <span
      className={`px-1.5 rounded font-mono text-xs max-w-[10rem] truncate shrink-0 ${
        inherited ? 'bg-dark-700/60 text-dark-400' : 'bg-accent-500/15 text-accent-400'
      }`}
    >
      {ports.join(', ')}
    </span>
  )
}

interface Props {
  mode: TorrentBlockerServerMode
  list: BanList
  servers: Server[]
  portBanUnsupportedIds: number[]
  minNodeVersion: string
  onModeChange: (mode: TorrentBlockerServerMode) => void
  onListChange: (list: BanList) => void
}

/**
 * Где банить: переключатель «везде, кроме / только на выбранных» и серверы текущего
 * режима по папкам. Порты задаются серверу (клик по нему) или всей папке (кнопка
 * в заголовке); своя настройка сервера главнее папки.
 */
export function BanTargetList({
  mode, list, servers, portBanUnsupportedIds, minNodeVersion, onModeChange, onListChange,
}: Props) {
  const { t } = useTranslation()
  const [openKey, setOpenKey] = useState<string | null>(null)
  const [expanded, setExpanded] = useState<Set<string>>(readExpanded)
  // Кнопка и её поповер — одно целое: повторный клик по кнопке закрывает поповер, а не открывает заново
  const openRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (openKey === null) return
    const handleClick = (e: MouseEvent) => {
      if (openRef.current && !openRef.current.contains(e.target as Node)) setOpenKey(null)
    }
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpenKey(null)
    }
    document.addEventListener('mousedown', handleClick)
    document.addEventListener('keydown', handleKey)
    return () => {
      document.removeEventListener('mousedown', handleClick)
      document.removeEventListener('keydown', handleKey)
    }
  }, [openKey])

  const serverById = useMemo(() => new Map(servers.map(s => [s.id, s])), [servers])
  const unsupported = useMemo(() => new Set(portBanUnsupportedIds), [portBanUnsupportedIds])
  const folderPorts = useMemo(() => new Map(list.folders.map(f => [f.name, f.ports])), [list.folders])

  // Порядок как в поиске — по списку серверов; удалённые из панели — в конце, без папки
  const entries = useMemo<Entry[]>(() => {
    const ruleById = new Map(list.servers.map(rule => [rule.id, rule]))
    const known = servers
      .filter(s => ruleById.has(s.id))
      .map(s => ({
        id: s.id,
        name: s.name,
        address: s.url.replace(/^https?:\/\//, ''),
        folder: s.folder || null,
        rule: ruleById.get(s.id)!,
      }))
    const missing = list.servers
      .filter(rule => !serverById.has(rule.id))
      .map(rule => ({ id: rule.id, name: `#${rule.id}`, address: '', folder: null, rule }))
    return [...known, ...missing]
  }, [list.servers, servers, serverById])

  const grouped = useMemo(() => groupByFolder(entries), [entries])
  const folderNames = useMemo(
    () => orderFolders([...grouped.keys()].filter((name): name is string => name !== null)),
    [grouped],
  )
  const hasFolders = servers.some(s => s.folder)

  const effectivePorts = (entry: Entry): string[] => {
    if (hasOwnRule(entry.rule)) return entry.rule.ports
    return (entry.folder && folderPorts.get(entry.folder)) || []
  }

  const commit = (rules: TorrentBlockerServerRule[], folders: TorrentBlockerFolderRule[] = list.folders) => {
    // Порты папки без её серверов в списке ничего не значат — не храним
    const listedFolders = new Set(rules.map(rule => serverById.get(rule.id)?.folder).filter(Boolean))
    onListChange({ servers: rules, folders: folders.filter(folder => listedFolders.has(folder.name)) })
  }

  const addServers = (ids: number[]) => {
    const present = new Set(list.servers.map(rule => rule.id))
    const added = ids.filter(id => !present.has(id)).map(id => ({ id, ports: [], override: false }))
    commit([...list.servers, ...added])
  }
  const removeServers = (ids: number[]) => {
    const removed = new Set(ids)
    setOpenKey(null)
    commit(list.servers.filter(rule => !removed.has(rule.id)))
  }
  const setServerRule = (id: number, ports: string[], override: boolean) =>
    commit(list.servers.map(rule => (rule.id === id ? { id, ports, override } : rule)))
  const setFolderPorts = (name: string, ports: string[]) => {
    const others = list.folders.filter(folder => folder.name !== name)
    commit(list.servers, ports.length > 0 ? [...others, { name, ports }] : others)
  }
  const clearList = () => {
    setOpenKey(null)
    onListChange({ servers: [], folders: [] })
  }

  const switchMode = (next: TorrentBlockerServerMode) => {
    setOpenKey(null)
    if (next !== mode) onModeChange(next)
  }
  const toggleOpen = (key: string) => setOpenKey(prev => (prev === key ? null : key))
  const toggleCollapsed = (key: string) => {
    setExpanded(prev => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      writeStorage(EXPANDED_KEY, JSON.stringify([...next]))
      return next
    })
  }

  const namesList = (names: string[]) => {
    const shown = names.slice(0, NAMES_IN_WARNING).join(', ')
    const rest = names.length - NAMES_IN_WARNING
    return rest > 0 ? `${shown} ${t('torrent_blocker.more_servers', { count: rest })}` : shown
  }
  const defaultLabel = t(`torrent_blocker.rule_default_${mode}`)

  const renderRuleBadge = (entry: Entry, folderRule: string[] | undefined) => {
    if (entry.rule.ports.length > 0) return <PortsBadge ports={entry.rule.ports} />
    if (!folderRule) return null
    if (entry.rule.override) {
      return <span className="px-1.5 rounded bg-accent-500/15 text-accent-400 text-xs shrink-0">{defaultLabel}</span>
    }
    return (
      <Tooltip label={t('torrent_blocker.inherited_from_folder')}>
        <span className="shrink-0 flex"><PortsBadge ports={folderRule} inherited /></span>
      </Tooltip>
    )
  }

  const renderServer = (entry: Entry, nested: boolean) => {
    const key = `server:${entry.id}`
    const isOpen = openKey === key
    const folderRule = entry.folder ? folderPorts.get(entry.folder) : undefined
    const supported = !unsupported.has(entry.id)
    const outdated = !supported && effectivePorts(entry).length > 0

    const choices: { value: Choice; label: string }[] = [
      ...(folderRule ? [{ value: 'folder' as Choice, label: t('torrent_blocker.rule_folder', { ports: folderRule.join(', ') }) }] : []),
      { value: 'default', label: defaultLabel },
      { value: 'ports', label: t('torrent_blocker.rule_ports') },
    ]
    let initialChoice: Choice = 'default'
    if (entry.rule.ports.length > 0) initialChoice = 'ports'
    else if (folderRule && !entry.rule.override) initialChoice = 'folder'

    const apply = (choice: Choice, ports: string[]) => {
      if (choice === 'ports') setServerRule(entry.id, ports, true)
      else setServerRule(entry.id, [], choice === 'default' && Boolean(folderRule))
    }

    return (
      <div
        key={key}
        ref={isOpen ? openRef : undefined}
        className={`relative flex items-center gap-2 py-1.5 pr-2 transition-colors ${nested ? 'pl-8' : 'pl-3'} ${
          isOpen ? 'bg-dark-800/60' : 'hover:bg-dark-800/40'
        }`}
      >
        <button
          type="button"
          onClick={() => toggleOpen(key)}
          className="flex items-center gap-2.5 flex-1 min-w-0 text-left rounded-md focus:outline-none focus-visible:ring-2 focus-visible:ring-accent-500/40"
        >
          <ServerIcon className="w-3.5 h-3.5 text-dark-500 shrink-0" />
          <span className="flex-1 min-w-0">
            <span className="block text-sm text-dark-200 truncate">{entry.name}</span>
            {entry.address && <span className="block text-xs text-dark-500 truncate">{entry.address}</span>}
          </span>
          {renderRuleBadge(entry, folderRule)}
        </button>
        {outdated && (
          <Tooltip label={t('torrent_blocker.ports_need_node', { version: minNodeVersion })}>
            <AlertTriangle className="w-3.5 h-3.5 text-amber-400 shrink-0" />
          </Tooltip>
        )}
        <Tooltip label={t('common.remove_from_list')}>
          <button
            type="button"
            aria-label={t('common.remove_from_list')}
            onClick={() => removeServers([entry.id])}
            className="p-1 rounded-md text-dark-500 hover:text-dark-300 shrink-0"
          >
            <X className="w-4 h-4" />
          </button>
        </Tooltip>
        {isOpen && (
          <RulePopover
            title={entry.name}
            choices={choices}
            initialChoice={initialChoice}
            initialPorts={effectivePorts(entry)}
            portBanSupported={supported}
            minNodeVersion={minNodeVersion}
            align="left"
            onApply={apply}
            onClose={() => setOpenKey(null)}
          />
        )}
      </div>
    )
  }

  const renderFolderWarning = (items: Entry[], folderRule: string[] | undefined) => {
    const overrides = folderRule
      ? items.filter(e => hasOwnRule(e.rule) && !samePorts(e.rule.ports, folderRule)).map(e => e.name)
      : []
    const outdated = items.filter(e => unsupported.has(e.id) && effectivePorts(e).length > 0).map(e => e.name)
    if (overrides.length === 0 && outdated.length === 0) return null
    return (
      <Tooltip
        maxWidth={320}
        label={
          <div className="space-y-1">
            {overrides.length > 0 && <div>{t('torrent_blocker.folder_overrides', { names: namesList(overrides) })}</div>}
            {outdated.length > 0 && (
              <div>{t('torrent_blocker.folder_outdated', { version: minNodeVersion, names: namesList(outdated) })}</div>
            )}
          </div>
        }
      >
        <AlertTriangle className="w-4 h-4 text-amber-400 shrink-0" />
      </Tooltip>
    )
  }

  const renderFolderRuleButton = (folder: string, label: string, folderRule: string[] | undefined) => {
    const key = `folder:${folder}`
    const isOpen = openKey === key
    return (
      // Поповер встаёт по правому краю всей строки папки (relative у заголовка), а не кнопки —
      // иначе на узком экране уезжал бы за левый край
      <div ref={isOpen ? openRef : undefined} className="shrink-0">
        <Tooltip label={t('torrent_blocker.folder_rule')} disabled={isOpen}>
          <button
            type="button"
            aria-label={t('torrent_blocker.folder_rule')}
            onClick={() => toggleOpen(key)}
            className={`flex items-center gap-1.5 p-1.5 rounded-md transition-colors ${
              isOpen ? 'text-accent-400 bg-accent-500/10' : 'text-dark-400 hover:text-accent-400 hover:bg-accent-500/10'
            }`}
          >
            {folderRule && <PortsBadge ports={folderRule} />}
            <SlidersHorizontal className="w-4 h-4" />
          </button>
        </Tooltip>
        {isOpen && (
          <RulePopover
            title={label}
            note={t('torrent_blocker.folder_rule_note')}
            choices={[
              { value: 'default', label: defaultLabel },
              { value: 'ports', label: t('torrent_blocker.rule_ports') },
            ]}
            initialChoice={folderRule ? 'ports' : 'default'}
            initialPorts={folderRule ?? []}
            portBanSupported
            minNodeVersion={minNodeVersion}
            align="right"
            onApply={(choice, ports) => setFolderPorts(folder, choice === 'ports' ? ports : [])}
            onClose={() => setOpenKey(null)}
          />
        )}
      </div>
    )
  }

  const renderGroup = (key: string, label: string, items: Entry[], folder: string | null) => {
    const isCollapsed = !expanded.has(key)
    const folderRule = folder ? folderPorts.get(folder) : undefined
    const removeLabel = folder ? t('torrent_blocker.remove_folder') : t('common.remove_from_list')
    const icon = folder
      ? (isCollapsed
          ? <Folder className="w-4 h-4 text-accent-400 shrink-0" />
          : <FolderOpen className="w-4 h-4 text-accent-400 shrink-0" />)
      : <ServerIcon className="w-4 h-4 text-dark-400 shrink-0" />

    return (
      <div key={key}>
        <div className="relative flex items-center gap-1.5 pl-3 pr-2 py-1.5 bg-dark-900/40">
          <div
            className="flex items-center gap-2 flex-1 min-w-0 py-0.5 cursor-pointer select-none"
            onClick={() => toggleCollapsed(key)}
          >
            {icon}
            <span className={`text-sm font-medium truncate ${folder ? 'text-dark-200' : 'text-dark-400'}`}>{label}</span>
            <span className="text-xs text-dark-500 shrink-0">({items.length})</span>
            <motion.div animate={{ rotate: isCollapsed ? -90 : 0 }} transition={{ duration: 0.15 }}>
              <ChevronDown className="w-3.5 h-3.5 text-dark-500" />
            </motion.div>
          </div>
          {renderFolderWarning(items, folderRule)}
          {folder && renderFolderRuleButton(folder, label, folderRule)}
          <Tooltip label={removeLabel}>
            <button
              type="button"
              aria-label={removeLabel}
              onClick={() => removeServers(items.map(e => e.id))}
              className="p-1.5 rounded-md text-dark-500 hover:text-dark-300 shrink-0"
            >
              <X className="w-4 h-4" />
            </button>
          </Tooltip>
        </div>
        <AnimatePresence initial={false}>
          {!isCollapsed && (
            // overflow виден после раскрытия, иначе обрезался бы поповер сервера
            <motion.div
              initial={{ height: 0, opacity: 0, overflow: 'hidden' }}
              animate={{ height: 'auto', opacity: 1, transitionEnd: { overflow: 'visible' } }}
              exit={{ height: 0, opacity: 0, overflow: 'hidden' }}
              transition={{ duration: 0.15 }}
            >
              {items.map(e => renderServer(e, true))}
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    )
  }

  const withoutFolder = grouped.get(null) ?? []

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between gap-2">
        <label className="text-sm text-dark-300">{t('torrent_blocker.ban_targets')}</label>
        {entries.length > 0 && (
          <button
            type="button"
            onClick={clearList}
            className="flex items-center gap-1 text-xs text-dark-500 hover:text-red-400 transition-colors"
          >
            <Trash2 className="w-3.5 h-3.5" />
            {t('torrent_blocker.clear_list')}
          </button>
        )}
      </div>
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

      {entries.length > 0 && (
        <div className="border border-dark-700 rounded-lg divide-y divide-dark-800">
          {hasFolders ? (
            <>
              {folderNames.map(name => renderGroup(name, name, grouped.get(name)!, name))}
              {withoutFolder.length > 0 && renderGroup(NO_FOLDER, t('torrent_blocker.no_folder'), withoutFolder, null)}
            </>
          ) : (
            entries.map(e => renderServer(e, false))
          )}
        </div>
      )}

      <ServerAddDropdown
        servers={servers}
        excludeIds={list.servers.map(rule => rule.id)}
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
