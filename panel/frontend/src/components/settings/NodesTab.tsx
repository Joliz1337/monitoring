import { useEffect, useId, useState } from 'react'
import { motion } from 'framer-motion'
import { Activity, Waypoints, Zap, Save } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import {
  useSettingsStore, METRICS_INTERVAL_OPTIONS, HAPROXY_INTERVAL_OPTIONS, type CollectorIntervalOption,
} from '../../stores/settingsStore'
import { SettingsSection, TAB_MOTION, TAB_GRID } from './SettingsSection'
import { SettingRow } from './SettingRow'
import { SegmentedControl, type SegmentedOption } from './SegmentedControl'
import { TimeSyncSection } from './TimeSyncSection'

const toIntervalOptions = (options: CollectorIntervalOption[]): SegmentedOption<number>[] =>
  options.map(option => ({
    value: option.value,
    label: <>{option.label}{option.recommended && <Zap className="w-3 h-3" />}</>,
  }))

const METRICS_OPTIONS = toIntervalOptions(METRICS_INTERVAL_OPTIONS)
const HAPROXY_OPTIONS = toIntervalOptions(HAPROXY_INTERVAL_OPTIONS)

export function NodesTab() {
  const { t } = useTranslation()
  const {
    metricsCollectInterval, haproxyCollectInterval, remnawaveNginxPath,
    setMetricsCollectInterval, setHaproxyCollectInterval, setRemnawaveNginxPath,
  } = useSettingsStore()
  const pathInputId = useId()

  // Черновик пути до явного сохранения
  const [pathDraft, setPathDraft] = useState(remnawaveNginxPath)
  useEffect(() => { setPathDraft(remnawaveNginxPath) }, [remnawaveNginxPath])

  const handleSavePath = async () => {
    const path = pathDraft.trim()
    if (!path.startsWith('/')) {
      toast.error(t('settings.remnawave_path_invalid'))
      return
    }
    await setRemnawaveNginxPath(path)
  }

  return (
    <motion.div {...TAB_MOTION} className={TAB_GRID}>
      <SettingsSection icon={Activity} title={t('settings.collector_intervals')} description={t('settings.collector_intervals_desc')}>
        <SettingRow label={t('settings.metrics_interval')}>
          <SegmentedControl value={metricsCollectInterval} options={METRICS_OPTIONS} onChange={setMetricsCollectInterval} />
        </SettingRow>
        <SettingRow label={t('settings.haproxy_interval')}>
          <SegmentedControl value={haproxyCollectInterval} options={HAPROXY_OPTIONS} onChange={setHaproxyCollectInterval} />
        </SettingRow>
        <p className="text-xs text-dark-500 mt-4 flex items-center gap-1">
          <Zap className="w-3 h-3 text-accent-500" />
          {t('settings.recommended_values')}
        </p>
      </SettingsSection>

      <TimeSyncSection />

      <SettingsSection icon={Waypoints} title={t('settings.remnawave_path_title')} description={t('settings.remnawave_path_desc')}>
        <SettingRow label={t('settings.remnawave_path_label')} hint={t('settings.remnawave_path_hint')} htmlFor={pathInputId}>
          <div className="flex items-center gap-2">
            <input
              id={pathInputId}
              type="text"
              value={pathDraft}
              onChange={e => setPathDraft(e.target.value)}
              placeholder="/opt/remnawave"
              spellCheck={false}
              className="input py-2 text-sm font-mono sm:w-64"
            />
            <button
              onClick={handleSavePath}
              disabled={pathDraft.trim() === remnawaveNginxPath}
              className="btn btn-primary text-sm py-2 disabled:opacity-50 disabled:hover:translate-y-0"
            >
              <Save className="w-4 h-4" />
              {t('common.save')}
            </button>
          </div>
        </SettingRow>
      </SettingsSection>
    </motion.div>
  )
}
