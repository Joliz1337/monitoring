import { motion, AnimatePresence } from 'framer-motion'
import { GitBranch, Shield, FlaskConical, AlertTriangle } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { useSettingsStore } from '../../stores/settingsStore'
import { SettingsSection, TAB_MOTION, TAB_GRID } from './SettingsSection'
import { SettingRow } from './SettingRow'
import { SegmentedControl, type SegmentedOption } from './SegmentedControl'
import { PanelHostStatsCard } from './PanelHostStatsCard'
import { PanelHostChartsCard } from './PanelHostChartsCard'
import { PanelCertificateCard } from './PanelCertificateCard'

export function SystemTab() {
  const { t } = useTranslation()
  const { updateBranch, setUpdateBranch } = useSettingsStore()

  const channelOptions: SegmentedOption<string>[] = [
    { value: 'main', label: <><Shield className="w-4 h-4" />{t('settings.update_channel_stable')}</> },
    { value: 'dev', label: <><FlaskConical className="w-4 h-4" />{t('settings.update_channel_dev')}</> },
  ]

  return (
    <motion.div {...TAB_MOTION} className={TAB_GRID}>
      <PanelHostStatsCard />
      <PanelCertificateCard />
      <PanelHostChartsCard className="2xl:col-span-2 min-[2200px]:col-span-3" />

      <SettingsSection icon={GitBranch} title={t('settings.update_channel')} description={t('settings.update_channel_desc')}>
        <SettingRow label={t('settings.update_channel_label')} hint={t('settings.update_channel_hint')}>
          <SegmentedControl value={updateBranch} options={channelOptions} onChange={setUpdateBranch} />
        </SettingRow>
        <AnimatePresence>
          {updateBranch === 'dev' && (
            <motion.div
              initial={{ opacity: 0, y: -10 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -10 }}
              className="flex items-center gap-3 p-3 mt-4 rounded-xl bg-warning/10 border border-warning/20"
            >
              <AlertTriangle className="w-4 h-4 text-warning flex-shrink-0" />
              <span className="text-sm text-warning">{t('settings.update_channel_dev_warning')}</span>
            </motion.div>
          )}
        </AnimatePresence>
      </SettingsSection>
    </motion.div>
  )
}
