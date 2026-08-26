import { useEffect } from 'react'
import { useSearchParams } from 'react-router-dom'
import { motion, AnimatePresence } from 'framer-motion'
import {
  Settings as SettingsIcon, SlidersHorizontal, Server, LayoutGrid, Cog, Database, type LucideIcon,
} from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { useSettingsStore } from '../stores/settingsStore'
import { FAQIcon } from '../components/FAQ'
import { InterfaceTab } from '../components/settings/InterfaceTab'
import { NodesTab } from '../components/settings/NodesTab'
import { ModulesTab } from '../components/settings/ModulesTab'
import { SystemTab } from '../components/settings/SystemTab'
import { BackupsTab } from '../components/settings/BackupsTab'

type SettingsTab = 'interface' | 'nodes' | 'modules' | 'system' | 'backups'

const SETTINGS_TABS: { id: SettingsTab; labelKey: string; icon: LucideIcon }[] = [
  { id: 'interface', labelKey: 'settings.tab_interface', icon: SlidersHorizontal },
  { id: 'nodes', labelKey: 'settings.tab_nodes', icon: Server },
  { id: 'modules', labelKey: 'settings.tab_modules', icon: LayoutGrid },
  { id: 'system', labelKey: 'settings.tab_system', icon: Cog },
  { id: 'backups', labelKey: 'settings.tab_backups', icon: Database },
]

const DEFAULT_TAB: SettingsTab = 'interface'
const TAB_PARAM = 'tab'

const isSettingsTab = (value: string | null): value is SettingsTab =>
  SETTINGS_TABS.some(tab => tab.id === value)

export default function Settings() {
  const { t } = useTranslation()
  const fetchSettings = useSettingsStore(state => state.fetchSettings)
  const [searchParams, setSearchParams] = useSearchParams()

  const requestedTab = searchParams.get(TAB_PARAM)
  const activeTab: SettingsTab = isSettingsTab(requestedTab) ? requestedTab : DEFAULT_TAB

  const selectTab = (id: SettingsTab) => {
    setSearchParams(prev => {
      prev.set(TAB_PARAM, id)
      return prev
    }, { replace: true })
  }

  useEffect(() => { fetchSettings() }, [fetchSettings])

  return (
    <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} className="space-y-6">
      <motion.div
        className="flex items-center gap-3"
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.4 }}
      >
        <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-accent-500/20 to-blue-500/20 flex items-center justify-center">
          <SettingsIcon className="w-5 h-5 text-accent-400" />
        </div>
        <div>
          <h1 className="text-xl font-semibold text-dark-50 flex items-center gap-2">
            {t('settings.title')}
            <FAQIcon screen="PAGE_SETTINGS" />
          </h1>
          <p className="text-sm text-dark-400">{t('settings.subtitle')}</p>
        </div>
      </motion.div>

      <div
        role="tablist"
        className="flex gap-1 p-1 bg-dark-900/50 rounded-xl border border-dark-800/50 w-fit max-w-full overflow-x-auto
                   [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
      >
        {SETTINGS_TABS.map(tab => (
          <button
            key={tab.id}
            role="tab"
            aria-selected={activeTab === tab.id}
            onClick={() => selectTab(tab.id)}
            className={`flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium shrink-0 whitespace-nowrap transition-all duration-200 ${
              activeTab === tab.id
                ? 'bg-accent-500/15 text-accent-400 shadow-sm'
                : 'text-dark-400 hover:text-dark-200 hover:bg-dark-800/50'
            }`}
          >
            <tab.icon className="w-4 h-4" />
            {t(tab.labelKey)}
          </button>
        ))}
      </div>

      <AnimatePresence mode="wait">
        {activeTab === 'interface' ? <InterfaceTab key="interface" />
          : activeTab === 'nodes' ? <NodesTab key="nodes" />
          : activeTab === 'modules' ? <ModulesTab key="modules" />
          : activeTab === 'system' ? <SystemTab key="system" />
          : <BackupsTab key="backups" />}
      </AnimatePresence>

      <p className="text-xs text-dark-500">{t('settings.storage_notice')}</p>
    </motion.div>
  )
}
