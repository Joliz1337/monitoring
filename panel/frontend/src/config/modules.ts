import {
  LayoutDashboard,
  Server,
  Layers,
  FileCode2,
  Flame,
  Route,
  Bell,
  CreditCard,
  Shield,
  ShieldBan,
  ShieldCheck,
  KeyRound,
  Radio,
  Waypoints,
  DoorOpen,
  FlaskConical,
  Package,
  Settings2,
  Siren,
  Settings,
  Radar,
  FolderCog,
  ShieldHalf,
  Wrench,
  type LucideIcon,
} from 'lucide-react'

export type NavGroupId = 'configs' | 'security' | 'maintenance'

export interface NavGroup {
  id: NavGroupId
  icon: LucideIcon
  labelKey: string
}

export const NAV_GROUPS: Record<NavGroupId, NavGroup> = {
  configs: { id: 'configs', icon: FolderCog, labelKey: 'nav.group_configs' },
  security: { id: 'security', icon: ShieldHalf, labelKey: 'nav.group_security' },
  maintenance: { id: 'maintenance', icon: Wrench, labelKey: 'nav.group_maintenance' },
}

export interface PanelModule {
  /** Сегмент маршрута; у дашборда пустой — это индексный роут */
  id: string
  path: string
  icon: LucideIcon
  labelKey: string
  /** Папка бокового меню; без неё вкладка стоит в меню отдельным пунктом */
  group?: NavGroupId
}

/**
 * Порядок записей = порядок вкладок в боковом меню и в настройке разделов.
 * Папка встаёт на место своей первой вкладки, поэтому вкладки одной папки идут подряд.
 */
export const PANEL_MODULES: PanelModule[] = [
  { id: 'dashboard', path: '', icon: LayoutDashboard, labelKey: 'common.dashboard' },
  { id: 'servers', path: 'servers', icon: Server, labelKey: 'common.servers' },
  { id: 'bulk-actions', path: 'bulk-actions', icon: Layers, labelKey: 'bulk_actions.title' },
  { id: 'alerts', path: 'alerts', icon: Bell, labelKey: 'common.alerts' },
  { id: 'billing', path: 'billing', icon: CreditCard, labelKey: 'common.billing' },
  { id: 'haproxy-configs', path: 'haproxy-configs', icon: FileCode2, labelKey: 'haproxy_configs.title', group: 'configs' },
  { id: 'firewall-profiles', path: 'firewall-profiles', icon: Flame, labelKey: 'firewall_profiles.title', group: 'configs' },
  { id: 'dnat-profiles', path: 'dnat-profiles', icon: Route, labelKey: 'dnat_profiles.title', group: 'configs' },
  { id: 'loss', path: 'loss', icon: Radar, labelKey: 'loss.title', group: 'configs' },
  { id: 'remnawave', path: 'remnawave', icon: Radio, labelKey: 'common.remnawave', group: 'configs' },
  { id: 'remnawave-nginx', path: 'remnawave-nginx', icon: Waypoints, labelKey: 'remnawave_nginx.title', group: 'configs' },
  { id: 'exit-proxy', path: 'exit-proxy', icon: DoorOpen, labelKey: 'exit_proxy.title', group: 'configs' },
  { id: 'xray-test', path: 'xray-test', icon: FlaskConical, labelKey: 'xray_test.title', group: 'configs' },
  { id: 'blocklist', path: 'blocklist', icon: Shield, labelKey: 'common.blocklist', group: 'security' },
  { id: 'torrent-blocker', path: 'torrent-blocker', icon: ShieldBan, labelKey: 'torrent_blocker.title', group: 'security' },
  { id: 'ssh-security', path: 'ssh-security', icon: KeyRound, labelKey: 'ssh_security.title', group: 'security' },
  { id: 'anti-ddos', path: 'anti-ddos', icon: Siren, labelKey: 'anti_ddos.title', group: 'security' },
  { id: 'updates', path: 'updates', icon: Package, labelKey: 'common.updates', group: 'maintenance' },
  { id: 'system-optimizations', path: 'system-optimizations', icon: Settings2, labelKey: 'sys_opt.title', group: 'maintenance' },
  { id: 'wildcard-ssl', path: 'wildcard-ssl', icon: ShieldCheck, labelKey: 'wildcard_ssl.title', group: 'maintenance' },
  { id: 'settings', path: 'settings', icon: Settings, labelKey: 'common.settings' },
]

export type NavEntry =
  | { kind: 'module'; module: PanelModule }
  | { kind: 'group'; group: NavGroup; modules: PanelModule[] }

/** Папка, все вкладки которой выключены в настройках, в меню не попадает */
export function buildNavTree(modules: PanelModule[]): NavEntry[] {
  const entries: NavEntry[] = []
  const groupMembers = new Map<NavGroupId, PanelModule[]>()

  for (const module of modules) {
    if (!module.group) {
      entries.push({ kind: 'module', module })
      continue
    }
    const members = groupMembers.get(module.group)
    if (members) {
      members.push(module)
      continue
    }
    const created = [module]
    groupMembers.set(module.group, created)
    entries.push({ kind: 'group', group: NAV_GROUPS[module.group], modules: created })
  }

  return entries
}

/** Скрыть нельзя: дашборд и серверы — ядро панели, настройки — единственный путь вернуть скрытое */
const ALWAYS_ON = ['dashboard', 'servers', 'settings']

export const TOGGLEABLE_MODULES = PANEL_MODULES.filter(m => !ALWAYS_ON.includes(m.id))

const KNOWN_IDS = new Set(TOGGLEABLE_MODULES.map(m => m.id))

/**
 * Хранится список выключенных разделов, а не включённых: раздел, добавленный
 * следующим релизом, появляется у всех сам, без правки настройки.
 */
export function parseHiddenModules(raw: string | undefined | null): string[] {
  if (!raw) return []
  return raw.split(',').map(id => id.trim()).filter(id => KNOWN_IDS.has(id))
}

export function serializeHiddenModules(ids: string[]): string {
  return ids.filter(id => KNOWN_IDS.has(id)).join(',')
}
