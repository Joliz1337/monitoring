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
  FlaskConical,
  Package,
  Settings2,
  Siren,
  Settings,
  type LucideIcon,
} from 'lucide-react'

export interface PanelModule {
  /** Сегмент маршрута; у дашборда пустой — это индексный роут */
  id: string
  path: string
  icon: LucideIcon
  labelKey: string
}

/** Порядок записей = порядок вкладок в боковом меню */
export const PANEL_MODULES: PanelModule[] = [
  { id: 'dashboard', path: '', icon: LayoutDashboard, labelKey: 'common.dashboard' },
  { id: 'servers', path: 'servers', icon: Server, labelKey: 'common.servers' },
  { id: 'bulk-actions', path: 'bulk-actions', icon: Layers, labelKey: 'bulk_actions.title' },
  { id: 'haproxy-configs', path: 'haproxy-configs', icon: FileCode2, labelKey: 'haproxy_configs.title' },
  { id: 'firewall-profiles', path: 'firewall-profiles', icon: Flame, labelKey: 'firewall_profiles.title' },
  { id: 'dnat-profiles', path: 'dnat-profiles', icon: Route, labelKey: 'dnat_profiles.title' },
  { id: 'alerts', path: 'alerts', icon: Bell, labelKey: 'common.alerts' },
  { id: 'billing', path: 'billing', icon: CreditCard, labelKey: 'common.billing' },
  { id: 'blocklist', path: 'blocklist', icon: Shield, labelKey: 'common.blocklist' },
  { id: 'torrent-blocker', path: 'torrent-blocker', icon: ShieldBan, labelKey: 'torrent_blocker.title' },
  { id: 'ssh-security', path: 'ssh-security', icon: KeyRound, labelKey: 'ssh_security.title' },
  { id: 'remnawave', path: 'remnawave', icon: Radio, labelKey: 'common.remnawave' },
  { id: 'remnawave-nginx', path: 'remnawave-nginx', icon: Waypoints, labelKey: 'remnawave_nginx.title' },
  { id: 'xray-test', path: 'xray-test', icon: FlaskConical, labelKey: 'xray_test.title' },
  { id: 'wildcard-ssl', path: 'wildcard-ssl', icon: ShieldCheck, labelKey: 'wildcard_ssl.title' },
  { id: 'updates', path: 'updates', icon: Package, labelKey: 'common.updates' },
  { id: 'system-optimizations', path: 'system-optimizations', icon: Settings2, labelKey: 'sys_opt.title' },
  { id: 'anti-ddos', path: 'anti-ddos', icon: Siren, labelKey: 'anti_ddos.title' },
  { id: 'settings', path: 'settings', icon: Settings, labelKey: 'common.settings' },
]

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
