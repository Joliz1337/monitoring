import { useEffect, useState } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { DoorOpen, ListChecks, Loader2, ScrollText, Server, Settings as SettingsIcon } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { FAQIcon } from '../components/FAQ'
import ChecksTab from '../components/exitproxy/ChecksTab'
import LogTab from '../components/exitproxy/LogTab'
import NodesTab from '../components/exitproxy/NodesTab'
import SettingsTab from '../components/exitproxy/SettingsTab'
import WorkerStatusCard from '../components/exitproxy/WorkerStatusCard'
import { useSmartRefresh } from '../hooks/useAutoRefresh'
import { useExitProxyStore } from '../stores/exitProxyStore'

type TabType = 'nodes' | 'checks' | 'settings' | 'log'

export default function ExitProxy() {
  const { t } = useTranslation()
  const [activeTab, setActiveTab] = useState<TabType>('nodes')
  const [refreshing, setRefreshing] = useState(false)
  const loaded = useExitProxyStore(s => s.loaded)
  const settings = useExitProxyStore(s => s.settings)
  const status = useExitProxyStore(s => s.status)
  const fetchAll = useExitProxyStore(s => s.fetchAll)
  const fetchNodes = useExitProxyStore(s => s.fetchNodes)
  const fetchStatus = useExitProxyStore(s => s.fetchStatus)

  useEffect(() => {
    fetchAll()
  }, [fetchAll])

  useSmartRefresh(
    () => Promise.all([fetchNodes(), fetchStatus()]).then(() => undefined),
    fetchStatus,
    { immediate: false },
  )

  const refresh = async () => {
    setRefreshing(true)
    await Promise.all([fetchNodes(), fetchStatus()])
    setRefreshing(false)
  }

  const tabs: { id: TabType; icon: typeof Server; label: string }[] = [
    { id: 'nodes', icon: Server, label: t('exit_proxy.tab_nodes') },
    { id: 'checks', icon: ListChecks, label: t('exit_proxy.tab_checks') },
    { id: 'settings', icon: SettingsIcon, label: t('exit_proxy.tab_settings') },
    { id: 'log', icon: ScrollText, label: t('exit_proxy.tab_log') },
  ]

  if (!loaded) {
    return <div className="flex items-center justify-center h-64"><Loader2 className="w-6 h-6 animate-spin text-dark-500" /></div>
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-3">
        <DoorOpen className="w-6 h-6 text-accent-500" />
        <div>
          <h1 className="text-xl font-semibold flex items-center gap-2">
            {t('exit_proxy.title')}
            <FAQIcon screen="PAGE_EXIT_PROXY" />
          </h1>
          <p className="text-sm text-dark-400">{t('exit_proxy.subtitle')}</p>
        </div>
      </div>

      {settings && !settings.enabled && (
        <div className="flex flex-wrap items-center gap-3 bg-amber-500/10 border border-amber-500/20 rounded-lg p-3 text-sm text-amber-300">
          <span>{t('exit_proxy.disabled_banner')}</span>
          <button onClick={() => setActiveTab('settings')} className="btn btn-secondary text-xs py-1 ml-auto">
            {t('exit_proxy.go_settings')}
          </button>
        </div>
      )}

      <WorkerStatusCard status={status} settings={settings} onRefresh={refresh} refreshing={refreshing} />

      <div className="flex gap-2 border-b border-dark-800 overflow-x-auto">
        {tabs.map(tab => (
          <button
            key={tab.id}
            onClick={() => setActiveTab(tab.id)}
            className={`flex items-center gap-2 px-4 py-2 text-sm font-medium border-b-2 -mb-px transition-colors whitespace-nowrap ${
              activeTab === tab.id ? 'border-accent-500 text-accent-400' : 'border-transparent text-dark-400 hover:text-dark-200'
            }`}
          >
            <tab.icon className="w-4 h-4" /> {tab.label}
          </button>
        ))}
      </div>

      <AnimatePresence mode="wait">
        <motion.div key={activeTab} initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }} transition={{ duration: 0.15 }}>
          {activeTab === 'nodes' && <NodesTab />}
          {activeTab === 'checks' && <ChecksTab />}
          {activeTab === 'settings' && <SettingsTab />}
          {activeTab === 'log' && <LogTab />}
        </motion.div>
      </AnimatePresence>
    </div>
  )
}
