import { motion } from 'framer-motion'
import { LayoutGrid, Check } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { useSettingsStore } from '../../stores/settingsStore'
import { TOGGLEABLE_MODULES } from '../../config/modules'
import { SettingsSection, TAB_MOTION } from './SettingsSection'

export function ModulesTab() {
  const { t } = useTranslation()
  const { hiddenModules, setHiddenModules } = useSettingsStore()

  const toggleModule = (moduleId: string) => {
    const next = hiddenModules.includes(moduleId)
      ? hiddenModules.filter(id => id !== moduleId)
      : [...hiddenModules, moduleId]
    setHiddenModules(next)
  }

  const headerActions = (
    <>
      <span className="text-xs text-dark-500 hidden sm:inline">
        {t('settings.modules_enabled_count', {
          count: TOGGLEABLE_MODULES.length - hiddenModules.length,
          total: TOGGLEABLE_MODULES.length,
        })}
      </span>
      <button
        onClick={() => setHiddenModules([])}
        disabled={hiddenModules.length === 0}
        className="px-3 py-1.5 rounded-lg text-xs font-medium bg-dark-800/60 border border-dark-700/50
                   text-dark-300 hover:bg-dark-700 transition-colors disabled:opacity-40 disabled:hover:bg-dark-800/60"
      >
        {t('settings.modules_enable_all')}
      </button>
    </>
  )

  return (
    <motion.div {...TAB_MOTION} className="space-y-6">
      <SettingsSection icon={LayoutGrid} title={t('settings.modules_title')} description={t('settings.modules_desc')} right={headerActions}>
        <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-2">
          {TOGGLEABLE_MODULES.map(module => {
            const enabled = !hiddenModules.includes(module.id)
            return (
              <button
                key={module.id}
                type="button"
                role="switch"
                aria-checked={enabled}
                onClick={() => toggleModule(module.id)}
                className={`flex items-center gap-2.5 px-3 py-2.5 rounded-xl text-sm font-medium border transition-colors
                            focus:outline-none focus-visible:ring-2 focus-visible:ring-accent-500/40 ${
                  enabled
                    ? 'bg-accent-500/10 border-accent-500/30 text-accent-300'
                    : 'bg-dark-800/40 border-dark-700/50 text-dark-500 hover:bg-dark-800 hover:text-dark-300'
                }`}
              >
                <module.icon className="w-4 h-4 flex-shrink-0" />
                <span className="truncate text-left">{t(module.labelKey)}</span>
                {enabled && <Check className="w-4 h-4 ml-auto flex-shrink-0" />}
              </button>
            )
          })}
        </div>
        <p className="text-xs text-dark-500 mt-4">{t('settings.modules_hint')}</p>
      </SettingsSection>
    </motion.div>
  )
}
