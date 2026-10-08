import { Outlet, NavLink, useParams, useLocation, Link } from 'react-router-dom'
import { motion, AnimatePresence } from 'framer-motion'
import {
  LayoutDashboard,
  Server,
  Settings,
  Menu,
  Activity,
  X,
  Sparkles,
  Package,
  Layers,
  Search,
  Shield,
  Radio,
  Gauge,
  StickyNote,
  ChevronDown,
  type LucideIcon
} from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import type { TFunction } from 'i18next'
import type { UpdateSummary } from '../../api/client'
import { useExtStore } from '../../stores/_extStore'
import { useNotesStore } from '../../stores/notesStore'
import { useSettingsStore } from '../../stores/settingsStore'
import { useUpdateSummaryStore } from '../../stores/updateSummaryStore'
import { useAutoRefresh } from '../../hooks/useAutoRefresh'
import { useScrollRestoration } from '../../hooks/useScrollRestoration'
import { useExpandedNavGroups } from '../../hooks/useExpandedNavGroups'
import { PANEL_MODULES, buildNavTree, type NavGroup, type PanelModule } from '../../config/modules'
import { useTranslation } from 'react-i18next'
import { Tooltip } from '../ui/Tooltip'
import NotesDrawer from '../Notes/NotesDrawer'
import { FAQDrawer } from '../FAQ'

const iconMap: Record<string, LucideIcon> = {
  Search,
  LayoutDashboard,
  Server,
  Settings,
  Package,
  Layers,
  Shield,
  Radio,
  Gauge
}

const overlayVariants = {
  hidden: { opacity: 0 },
  visible: { opacity: 1 },
  exit: { opacity: 0 }
}

const navItemVariants = {
  hidden: { opacity: 0, x: -20 },
  visible: (i: number) => ({
    opacity: 1,
    x: 0,
    transition: { delay: i * 0.1, duration: 0.3 }
  })
}

/** Отдельные пункты из стора встают сразу после «Массовых операций» */
const EXTRA_NAV_ITEM_INDEX = 3

// Версии на GitHub панель кэширует на 5 минут — чаще спрашивать сводку незачем
const UPDATE_SUMMARY_POLL_MS = 5 * 60_000
const MAX_BADGE_COUNT = 99

interface NavBadge {
  count: number
  hints: string[]
}

interface NavLinkItem {
  to: string
  icon: LucideIcon
  label: string
  end: boolean
  active: boolean
  badge?: NavBadge
}

type SidebarEntry =
  | { kind: 'link'; item: NavLinkItem }
  | { kind: 'group'; group: NavGroup; items: NavLinkItem[] }

/** Число на значке — сколько всего обновить: панель считается за одну, плюс каждая отставшая нода */
function updatesBadge(summary: UpdateSummary | null, t: TFunction): NavBadge | undefined {
  if (!summary) return undefined
  const panelUpdate = summary.panel.update_available
  const outdatedNodes = summary.nodes.outdated
  if (!panelUpdate && outdatedNodes === 0) return undefined

  const hints: string[] = []
  if (panelUpdate) {
    hints.push(t('nav.updates_badge_panel', { current: summary.panel.version, latest: summary.panel.latest_version }))
  }
  if (outdatedNodes > 0) {
    hints.push(t('nav.updates_badge_nodes', { count: outdatedNodes, total: summary.nodes.total, latest: summary.nodes.latest_version }))
  }
  return { count: (panelUpdate ? 1 : 0) + outdatedNodes, hints }
}

function NavBadgePill({ badge }: { badge: NavBadge }) {
  return (
    <Tooltip label={<div className="space-y-0.5">{badge.hints.map(hint => <div key={hint}>{hint}</div>)}</div>} position="right" maxWidth={300}>
      <span className="relative z-10 ml-auto min-w-[1.25rem] h-5 px-1.5 rounded-full bg-accent-500/20 text-accent-300
                       text-2xs font-semibold leading-none flex items-center justify-center">
        {badge.count > MAX_BADGE_COUNT ? `${MAX_BADGE_COUNT}+` : badge.count}
      </span>
    </Tooltip>
  )
}

interface SidebarLinkProps {
  item: NavLinkItem
  nested?: boolean
  onNavigate: () => void
}

function SidebarLink({ item, nested = false, onNavigate }: SidebarLinkProps) {
  return (
    <NavLink to={item.to} end={item.end} onClick={onNavigate} className="block">
      <motion.div
        className={`
          relative flex items-center gap-3 rounded-xl transition-all duration-200
          ${nested ? 'px-3 py-2.5 text-sm' : 'px-4 py-3'}
          ${item.active
            ? 'bg-accent-500/10 text-accent-400'
            : 'text-dark-400 hover:text-dark-200 hover:bg-dark-800/50'
          }
        `}
        whileHover={{ x: 4 }}
        whileTap={{ scale: 0.98 }}
      >
        <motion.div
          animate={item.active ? { rotate: [0, -10, 10, 0] } : {}}
          transition={{ duration: 0.5 }}
        >
          <item.icon className={nested ? 'w-4 h-4' : 'w-5 h-5'} />
        </motion.div>
        <span className="font-medium">{item.label}</span>
        {item.badge && <NavBadgePill badge={item.badge} />}

        {/* Glow effect for active item */}
        {item.active && (
          <motion.div
            className="absolute inset-0 rounded-xl bg-accent-500/5"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
          />
        )}
      </motion.div>
    </NavLink>
  )
}

interface SidebarGroupProps {
  group: NavGroup
  items: NavLinkItem[]
  expanded: boolean
  onToggle: () => void
  onNavigate: () => void
}

function SidebarGroup({ group, items, expanded, onToggle, onNavigate }: SidebarGroupProps) {
  const { t } = useTranslation()
  // У свёрнутой папки активная вкладка и значки её вкладок не видны — показываем их на самой папке
  const highlighted = !expanded && items.some(item => item.active)
  const collapsedBadge = expanded ? undefined : items.find(item => item.badge)?.badge

  return (
    <div>
      <motion.button
        type="button"
        onClick={onToggle}
        aria-expanded={expanded}
        className={`
          w-full flex items-center gap-3 px-4 py-3 rounded-xl transition-all duration-200
          ${highlighted
            ? 'bg-accent-500/10 text-accent-400'
            : 'text-dark-400 hover:text-dark-200 hover:bg-dark-800/50'
          }
        `}
        whileHover={{ x: 4 }}
        whileTap={{ scale: 0.98 }}
      >
        <group.icon className="w-5 h-5" />
        <span className="font-medium">{t(group.labelKey)}</span>
        {collapsedBadge && <NavBadgePill badge={collapsedBadge} />}
        <ChevronDown
          className={`w-4 h-4 transition-transform duration-200 ${collapsedBadge ? '' : 'ml-auto'} ${expanded ? '' : '-rotate-90'}`}
        />
      </motion.button>

      <AnimatePresence initial={false}>
        {expanded && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.2 }}
            className="overflow-hidden"
          >
            <div className="ml-6 mt-1 pl-2 border-l border-dark-800 space-y-1">
              {items.map(item => (
                <SidebarLink key={item.to} item={item} nested onNavigate={onNavigate} />
              ))}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}

export default function Layout() {
  const { uid } = useParams()
  const location = useLocation()
  const extNavItems = useExtStore(s => s.navItems)
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const toggleNotes = useNotesStore(s => s.toggle)
  const notesOpen = useNotesStore(s => s.isOpen)
  const hiddenModules = useSettingsStore(s => s.hiddenModules)
  const fetchSettings = useSettingsStore(s => s.fetchSettings)
  const [expandedGroups, setGroupOpen] = useExpandedNavGroups()
  const { t } = useTranslation()

  useEffect(() => { fetchSettings() }, [fetchSettings])

  const updatesVisible = !hiddenModules.includes('updates')
  const updateSummary = useUpdateSummaryStore(s => s.summary)
  const refreshUpdateSummary = useUpdateSummaryStore(s => s.refresh)
  useAutoRefresh(refreshUpdateSummary, { enabled: updatesVisible, customInterval: UPDATE_SUMMARY_POLL_MS })
  const moduleBadges: Record<string, NavBadge | undefined> = {
    updates: updatesVisible ? updatesBadge(updateSummary, t) : undefined,
  }
  const hasNavBadge = Object.values(moduleBadges).some(Boolean)

  const pageContentRef = useRef<HTMLDivElement>(null)
  useScrollRestoration(pageContentRef)

  // Каскадное появление пунктов — только при первой отрисовке меню: раздел,
  // включённый в настройках позже, иначе висел бы прозрачным index × 0.1 с
  const introDone = useRef(false)
  useEffect(() => { introDone.current = true }, [])

  // Сравнение по границе сегмента: иначе /remnawave подсвечивался бы и на /remnawave-nginx
  const isPathActive = (to: string, end: boolean) =>
    location.pathname === to || (!end && location.pathname.startsWith(`${to}/`))

  const toNavLink = (to: string, icon: LucideIcon, label: string, end: boolean): NavLinkItem =>
    ({ to, icon, label, end, active: isPathActive(to, end) })

  const moduleLink = (module: PanelModule): NavLinkItem => ({
    ...toNavLink(
      module.path ? `/${uid}/${module.path}` : `/${uid}`,
      module.icon,
      t(module.labelKey),
      module.path === '',
    ),
    badge: moduleBadges[module.id],
  })

  const visibleModules = PANEL_MODULES.filter(module => !hiddenModules.includes(module.id))
  const sidebarEntries: SidebarEntry[] = buildNavTree(visibleModules).map(entry =>
    entry.kind === 'module'
      ? { kind: 'link', item: moduleLink(entry.module) }
      : { kind: 'group', group: entry.group, items: entry.modules.map(moduleLink) }
  )
  sidebarEntries.splice(EXTRA_NAV_ITEM_INDEX, 0, ...extNavItems.map((navItem): SidebarEntry => ({
    kind: 'link',
    item: toNavLink(`/${uid}/${navItem.path}`, iconMap[navItem.icon] || Search, navItem.label, false),
  })))

  const activeGroupId = sidebarEntries.find(
    (entry): entry is Extract<SidebarEntry, { kind: 'group' }> =>
      entry.kind === 'group' && entry.items.some(item => item.active)
  )?.group.id

  // Переход на вкладку из свёрнутой папки (в том числе по ссылке со страницы) раскрывает её
  useEffect(() => {
    if (activeGroupId) setGroupOpen(activeGroupId, true)
  }, [activeGroupId, setGroupOpen])

  const closeSidebar = () => setSidebarOpen(false)

  return (
    <div className="min-h-screen bg-dark-950 flex overflow-hidden">
      {/* Animated background */}
      <div className="fixed inset-0 z-0 pointer-events-none">
        <div className="absolute inset-0 bg-gradient-to-br from-accent-500/5 via-transparent to-purple/5" />
        <div className="absolute top-0 left-0 w-[500px] h-[500px] bg-accent-500/10 rounded-full blur-[100px] bg-blob-drift-a" />
        <div className="absolute bottom-0 right-0 w-[400px] h-[400px] bg-purple/10 rounded-full blur-[100px] bg-blob-drift-b" />
      </div>
      
      {/* Mobile overlay */}
      <AnimatePresence>
        {sidebarOpen && (
          <motion.div 
            variants={overlayVariants}
            initial="hidden"
            animate="visible"
            exit="exit"
            className="fixed inset-0 bg-black/60 backdrop-blur-sm z-40 lg:hidden"
            onClick={closeSidebar}
          />
        )}
      </AnimatePresence>
      
      {/* Sidebar: на десктопе закреплено и прокручивается само, если не влезает по высоте.
          z-0 ниже контента (z-10) — модалки страниц должны перекрывать меню */}
      <motion.aside
        className={`
          fixed inset-y-0 left-0 z-50 lg:z-0
          w-72 bg-dark-900/80 backdrop-blur-xl border-r border-dark-800/50
          flex flex-col
          ${sidebarOpen ? 'translate-x-0' : '-translate-x-full lg:translate-x-0'}
          transition-transform duration-300 ease-out lg:transition-none
        `}
      >
        <div className="flex flex-col h-full">
          {/* Logo */}
          <div className="p-6 border-b border-dark-800/50">
            <Link to={`/${uid}`}>
              <motion.div 
                className="flex items-center gap-3 cursor-pointer"
                initial={{ opacity: 0, y: -10 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 0.5 }}
                whileHover={{ scale: 1.02 }}
                whileTap={{ scale: 0.98 }}
              >
                <motion.div 
                  className="w-11 h-11 rounded-xl bg-gradient-to-br from-accent-500/20 to-accent-600/20 
                             flex items-center justify-center border border-accent-500/20
                             shadow-lg shadow-accent-500/10"
                  whileHover={{ scale: 1.05, rotate: 5 }}
                  transition={{ type: 'spring', stiffness: 400 }}
                >
                  <Activity className="w-5 h-5 text-accent-400" />
                </motion.div>
                <div>
                  <h1 className="font-bold text-dark-100 flex items-center gap-2">
                    {t('common.monitoring')}
                    <Sparkles className="w-3.5 h-3.5 text-accent-400" />
                  </h1>
                </div>
              </motion.div>
            </Link>
          </div>
          
          {/* Close button for mobile */}
          <motion.button
            className="absolute top-4 right-4 p-2 rounded-lg hover:bg-dark-800 text-dark-400 lg:hidden"
            onClick={closeSidebar}
            whileHover={{ scale: 1.1 }}
            whileTap={{ scale: 0.9 }}
          >
            <X className="w-5 h-5" />
          </motion.button>
          
          {/* Navigation */}
          <nav className="flex-1 min-h-0 overflow-y-auto overflow-x-hidden overscroll-contain p-4 space-y-1">
            {sidebarEntries.map((entry, index) => (
              <motion.div
                key={entry.kind === 'link' ? entry.item.to : entry.group.id}
                custom={index}
                variants={navItemVariants}
                initial={introDone.current ? false : 'hidden'}
                animate="visible"
              >
                {entry.kind === 'link' ? (
                  <SidebarLink item={entry.item} onNavigate={closeSidebar} />
                ) : (
                  <SidebarGroup
                    group={entry.group}
                    items={entry.items}
                    expanded={expandedGroups.has(entry.group.id)}
                    onToggle={() => setGroupOpen(entry.group.id, !expandedGroups.has(entry.group.id))}
                    onNavigate={closeSidebar}
                  />
                )}
              </motion.div>
            ))}
          </nav>
          
        </div>
      </motion.aside>
      
      {/* Main content: отступ margin, а не padding — прозрачный padding поверх меню перехватывал бы клики */}
      <div className="flex-1 flex flex-col min-w-0 relative z-10 lg:ml-72">
        {/* Mobile header */}
        <motion.header 
          className="h-16 bg-dark-900/60 backdrop-blur-xl border-b border-dark-800/50 
                     flex items-center px-4 lg:hidden sticky top-0 z-30"
          initial={{ y: -20, opacity: 0 }}
          animate={{ y: 0, opacity: 1 }}
          transition={{ duration: 0.3 }}
        >
          <motion.button
            onClick={() => setSidebarOpen(true)}
            className="relative p-2 rounded-xl hover:bg-dark-800 text-dark-400"
            whileHover={{ scale: 1.05 }}
            whileTap={{ scale: 0.95 }}
          >
            <Menu className="w-6 h-6" />
            {hasNavBadge && <span className="absolute top-1.5 right-1.5 w-2 h-2 rounded-full bg-accent-400" />}
          </motion.button>
          
          <div className="ml-4 flex items-center gap-2">
            <Activity className="w-5 h-5 text-accent-500" />
            <span className="font-semibold text-dark-100">{t('common.monitoring')}</span>
          </div>
        </motion.header>
        
        {/* Page content */}
        <main className="flex-1 overflow-auto">
          <div ref={pageContentRef} className="p-6 lg:p-8">
            <div key={location.pathname} className="animate-page-enter">
              <Outlet />
            </div>
          </div>
        </main>
      </div>

      {/* Notes floating tab — right edge */}
      <AnimatePresence>
        {!notesOpen && (
          <Tooltip label={t('notes.title')} position="left">
            <motion.button
              initial={{ x: 20, opacity: 0 }}
              animate={{ x: 0, opacity: 1 }}
              exit={{ x: 20, opacity: 0 }}
              onClick={toggleNotes}
              className="fixed right-0 top-1/2 -translate-y-1/2 z-40
                         bg-amber-500/90 hover:bg-amber-400 text-dark-900
                         rounded-l-lg py-3 px-1.5 shadow-lg shadow-amber-500/20
                         transition-colors cursor-pointer"
              whileHover={{ x: -2 }}
            >
              <StickyNote className="w-4 h-4" />
            </motion.button>
          </Tooltip>
        )}
      </AnimatePresence>

      <NotesDrawer />
      <FAQDrawer />
    </div>
  )
}
