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
  ShieldCheck,
  ShieldBan,
  FileCode2,
  KeyRound,
  Radio,
  Bell,
  CreditCard,
  StickyNote,
  Settings2,
  type LucideIcon
} from 'lucide-react'
import { useState } from 'react'
import { useExtStore } from '../../stores/_extStore'
import { useNotesStore } from '../../stores/notesStore'
import { useTranslation } from 'react-i18next'
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
  Radio
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

export default function Layout() {
  const { uid } = useParams()
  const location = useLocation()
  const navItem = useExtStore(s => s.navItem)
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const toggleNotes = useNotesStore(s => s.toggle)
  const notesOpen = useNotesStore(s => s.isOpen)
  const { t } = useTranslation()
  
  const baseNavItems = [
    { to: `/${uid}`, icon: LayoutDashboard, label: t('common.dashboard'), end: true },
    { to: `/${uid}/servers`, icon: Server, label: t('common.servers'), end: false },
    { to: `/${uid}/bulk-actions`, icon: Layers, label: t('bulk_actions.title'), end: false },
    { to: `/${uid}/haproxy-configs`, icon: FileCode2, label: t('haproxy_configs.title'), end: false },
    { to: `/${uid}/alerts`, icon: Bell, label: t('common.alerts'), end: false },
    { to: `/${uid}/billing`, icon: CreditCard, label: t('common.billing'), end: false },
    { to: `/${uid}/blocklist`, icon: Shield, label: t('common.blocklist'), end: false },
    { to: `/${uid}/torrent-blocker`, icon: ShieldBan, label: t('torrent_blocker.title'), end: false },
    { to: `/${uid}/ssh-security`, icon: KeyRound, label: t('ssh_security.title'), end: false },
    { to: `/${uid}/remnawave`, icon: Radio, label: t('common.remnawave'), end: false },
    { to: `/${uid}/wildcard-ssl`, icon: ShieldCheck, label: t('wildcard_ssl.title'), end: false },
    { to: `/${uid}/updates`, icon: Package, label: t('common.updates'), end: false },
    { to: `/${uid}/system-optimizations`, icon: Settings2, label: t('sys_opt.title'), end: false },
    { to: `/${uid}/settings`, icon: Settings, label: t('common.settings'), end: false },
  ]
  
  const navItems = navItem 
    ? [...baseNavItems.slice(0, 3), { to: `/${uid}/${navItem.path}`, icon: iconMap[navItem.icon] || Search, label: navItem.label, end: false }, ...baseNavItems.slice(3)]
    : baseNavItems
  
  return (
    <div className="min-h-screen bg-dark-950 flex overflow-hidden">
      {/* Animated background */}
      <div className="fixed inset-0 z-0 pointer-events-none">
        <div className="absolute inset-0 bg-gradient-to-br from-accent-500/5 via-transparent to-purple/5" />
        <motion.div
          className="absolute top-0 left-0 w-[500px] h-[500px] bg-accent-500/10 rounded-full blur-[100px]"
          animate={{
            x: [0, 100, 0],
            y: [0, 50, 0],
            scale: [1, 1.1, 1],
          }}
          transition={{ duration: 20, repeat: Infinity, ease: 'easeInOut' }}
        />
        <motion.div
          className="absolute bottom-0 right-0 w-[400px] h-[400px] bg-purple/10 rounded-full blur-[100px]"
          animate={{
            x: [0, -50, 0],
            y: [0, -100, 0],
            scale: [1, 1.2, 1],
          }}
          transition={{ duration: 15, repeat: Infinity, ease: 'easeInOut' }}
        />
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
            onClick={() => setSidebarOpen(false)}
          />
        )}
      </AnimatePresence>
      
      {/* Sidebar */}
      <motion.aside 
        className={`
          fixed lg:static inset-y-0 left-0 z-50
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
                    <Sparkles className="w-3 h-3 text-accent-400" />
                  </h1>
                </div>
              </motion.div>
            </Link>
          </div>
          
          {/* Close button for mobile */}
          <motion.button
            className="absolute top-4 right-4 p-2 rounded-lg hover:bg-dark-800 text-dark-400 lg:hidden"
            onClick={() => setSidebarOpen(false)}
            whileHover={{ scale: 1.1 }}
            whileTap={{ scale: 0.9 }}
          >
            <X className="w-5 h-5" />
          </motion.button>
          
          {/* Navigation */}
          <nav className="flex-1 p-4 space-y-1">
            {navItems.map((item, index) => {
              const isActive = item.end 
                ? location.pathname === item.to 
                : location.pathname.startsWith(item.to)
              
              return (
                <motion.div
                  key={item.to}
                  custom={index}
                  variants={navItemVariants}
                  initial="hidden"
                  animate="visible"
                >
                  <NavLink
                    to={item.to}
                    end={item.end}
                    onClick={() => setSidebarOpen(false)}
                    className="block"
                  >
                    <motion.div
                      className={`
                        relative flex items-center gap-3 px-4 py-3 rounded-xl transition-all duration-200
                        ${isActive 
                          ? 'bg-accent-500/10 text-accent-400' 
                          : 'text-dark-400 hover:text-dark-200 hover:bg-dark-800/50'
                        }
                      `}
                      whileHover={{ x: 4 }}
                      whileTap={{ scale: 0.98 }}
                    >
                      <motion.div
                        animate={isActive ? { rotate: [0, -10, 10, 0] } : {}}
                        transition={{ duration: 0.5 }}
                      >
                        <item.icon className="w-5 h-5" />
                      </motion.div>
                      <span className="font-medium">{item.label}</span>
                      
                      {/* Glow effect for active item */}
                      {isActive && (
                        <motion.div
                          className="absolute inset-0 rounded-xl bg-accent-500/5"
                          initial={{ opacity: 0 }}
                          animate={{ opacity: 1 }}
                        />
                      )}
                    </motion.div>
                  </NavLink>
                </motion.div>
              )
            })}
          </nav>
          
        </div>
      </motion.aside>
      
      {/* Main content */}
      <div className="flex-1 flex flex-col min-w-0 relative z-10">
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
            className="p-2 rounded-xl hover:bg-dark-800 text-dark-400"
            whileHover={{ scale: 1.05 }}
            whileTap={{ scale: 0.95 }}
          >
            <Menu className="w-6 h-6" />
          </motion.button>
          
          <div className="ml-4 flex items-center gap-2">
            <Activity className="w-5 h-5 text-accent-500" />
            <span className="font-semibold text-dark-100">{t('common.monitoring')}</span>
          </div>
        </motion.header>
        
        {/* Page content */}
        <main className="flex-1 overflow-auto">
          <div className="p-6 lg:p-8">
            <div key={location.pathname} className="animate-page-enter">
              <Outlet />
            </div>
          </div>
        </main>
      </div>

      {/* Notes floating tab — right edge */}
      <AnimatePresence>
        {!notesOpen && (
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
            title={t('notes.title')}
          >
            <StickyNote className="w-4 h-4" />
          </motion.button>
        )}
      </AnimatePresence>

      <NotesDrawer />
      <FAQDrawer />
    </div>
  )
}
