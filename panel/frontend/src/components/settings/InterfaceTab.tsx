import { useId, useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { Palette, Clock, LineChart, LayoutGrid, List, ChevronDown } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { useSettingsStore, TIMEZONE_OPTIONS, TRAFFIC_PERIOD_OPTIONS } from '../../stores/settingsStore'
import { CHART_METRICS, type ChartMode, type LiveValuesMode } from '../../config/chartDisplay'
import { SettingsSection, TAB_MOTION, TAB_GRID } from './SettingsSection'
import { SettingRow } from './SettingRow'
import { SegmentedControl, type SegmentedOption } from './SegmentedControl'
import { Switch } from './Switch'

const LANGUAGE_OPTIONS: SegmentedOption<string>[] = [
  { value: 'en', label: '🇺🇸 English' },
  { value: 'ru', label: '🇷🇺 Русский' },
]

const REFRESH_SECONDS = [5, 10, 30, 60, 120, 300] as const

type LayoutMode = 'grid' | 'list'
// В сторе переопределение метрики — null; для сегмент-контрола нужен строковый вариант
type OverrideChoice = ChartMode | 'inherit'

export function InterfaceTab() {
  const { t, i18n } = useTranslation()
  const {
    refreshInterval, compactView, timezone, trafficPeriod,
    chartMode, chartPeaks, chartModeOverrides, liveValues,
    setRefreshInterval, setCompactView, setTimezone, setTrafficPeriod,
    setChartMode, setChartPeaks, setChartModeOverride, setLiveValues,
  } = useSettingsStore()
  const [showPerMetric, setShowPerMetric] = useState(false)
  const timezoneSelectId = useId()

  const layoutOptions: SegmentedOption<LayoutMode>[] = [
    { value: 'grid', label: <><LayoutGrid className="w-4 h-4" />{t('settings.grid_view')}</> },
    { value: 'list', label: <><List className="w-4 h-4" />{t('settings.list_view')}</> },
  ]

  const refreshOptions: SegmentedOption<number>[] = REFRESH_SECONDS.map(seconds => ({
    value: seconds,
    label: seconds < 60
      ? `${seconds} ${t('common.seconds')}`
      : `${seconds / 60} ${seconds === 60 ? t('common.minute') : t('common.minutes')}`,
  }))

  const trafficOptions: SegmentedOption<number>[] = TRAFFIC_PERIOD_OPTIONS.map(option => ({
    value: option.value,
    label: t(`settings.traffic_${option.value}d`),
  }))

  const chartModeOptions: SegmentedOption<ChartMode>[] = [
    { value: 'smooth', label: t('settings.chart_mode_smooth'), hint: t('settings.chart_mode_smooth_hint') },
    { value: 'raw', label: t('settings.chart_mode_raw'), hint: t('settings.chart_mode_raw_hint') },
  ]

  const liveValuesOptions: SegmentedOption<LiveValuesMode>[] = [
    { value: 'instant', label: t('settings.live_values_instant'), hint: t('settings.live_values_instant_hint') },
    { value: 'average', label: t('settings.live_values_average'), hint: t('settings.live_values_average_hint') },
  ]

  const overrideOptions: SegmentedOption<OverrideChoice>[] = [
    { value: 'inherit', label: t('settings.chart_inherit') },
    { value: 'smooth', label: t('settings.chart_mode_smooth') },
    { value: 'raw', label: t('settings.chart_mode_raw') },
  ]

  return (
    <motion.div {...TAB_MOTION} className={TAB_GRID}>
      <SettingsSection icon={Palette} title={t('settings.section_appearance')}>
        <SettingRow label={t('settings.language')} hint={t('settings.language_desc')}>
          <SegmentedControl value={i18n.language} options={LANGUAGE_OPTIONS} onChange={lng => i18n.changeLanguage(lng)} />
        </SettingRow>
        <SettingRow label={t('settings.layout')} hint={t('settings.layout_desc')}>
          <SegmentedControl<LayoutMode>
            value={compactView ? 'list' : 'grid'}
            options={layoutOptions}
            onChange={mode => setCompactView(mode === 'list')}
          />
        </SettingRow>
        <SettingRow label={t('settings.auto_refresh')} hint={t('settings.auto_refresh_desc')}>
          <SegmentedControl value={refreshInterval} options={refreshOptions} onChange={setRefreshInterval} />
        </SettingRow>
        <SettingRow label={t('settings.traffic_period')} hint={t('settings.traffic_period_desc')}>
          <SegmentedControl value={trafficPeriod} options={trafficOptions} onChange={setTrafficPeriod} />
        </SettingRow>
      </SettingsSection>

      <SettingsSection icon={Clock} title={t('settings.section_time')}>
        <SettingRow label={t('settings.timezone')} hint={t('settings.timezone_desc')} htmlFor={timezoneSelectId}>
          <select
            id={timezoneSelectId}
            className="input py-2 text-sm sm:w-64"
            value={timezone}
            onChange={e => setTimezone(e.target.value)}
          >
            {TIMEZONE_OPTIONS.map(option => (
              <option key={option.value} value={option.value}>{option.label} ({option.offset})</option>
            ))}
          </select>
        </SettingRow>
      </SettingsSection>

      <SettingsSection icon={LineChart} title={t('settings.charts')} description={t('settings.charts_desc')} faq="SETTINGS_CHARTS">
        <SettingRow label={t('settings.chart_mode')}>
          <SegmentedControl value={chartMode} options={chartModeOptions} onChange={setChartMode} />
        </SettingRow>
        <SettingRow label={t('settings.chart_peaks')} hint={t('settings.chart_peaks_hint')}>
          <Switch checked={chartPeaks} onChange={setChartPeaks} />
        </SettingRow>
        <SettingRow label={t('settings.live_values')} hint={t('settings.live_values_hint')}>
          <SegmentedControl value={liveValues} options={liveValuesOptions} onChange={setLiveValues} />
        </SettingRow>

        <div className="pt-4 border-t border-dark-800/50">
          <button
            type="button"
            onClick={() => setShowPerMetric(open => !open)}
            aria-expanded={showPerMetric}
            className="w-full flex items-center justify-between text-sm text-dark-400 hover:text-dark-200 transition-colors"
          >
            <span>{t('settings.chart_per_metric')}</span>
            <motion.span animate={{ rotate: showPerMetric ? 180 : 0 }} transition={{ duration: 0.2 }}>
              <ChevronDown className="w-4 h-4" />
            </motion.span>
          </button>
          <AnimatePresence>
            {showPerMetric && (
              <motion.div
                initial={{ height: 0, opacity: 0 }}
                animate={{ height: 'auto', opacity: 1 }}
                exit={{ height: 0, opacity: 0 }}
                transition={{ duration: 0.25 }}
                className="overflow-hidden"
              >
                <div className="mt-3 space-y-2">
                  {CHART_METRICS.map(metric => (
                    <div key={metric} className="flex items-center justify-between gap-3 p-2.5 bg-dark-800/40 rounded-xl border border-dark-700/50">
                      <span className="text-sm text-dark-300">{t(`chart_metric.${metric}`)}</span>
                      <SegmentedControl<OverrideChoice>
                        size="sm"
                        aria-label={t(`chart_metric.${metric}`)}
                        value={chartModeOverrides[metric] ?? 'inherit'}
                        options={overrideOptions}
                        onChange={choice => setChartModeOverride(metric, choice === 'inherit' ? null : choice)}
                      />
                    </div>
                  ))}
                </div>
              </motion.div>
            )}
          </AnimatePresence>
        </div>
      </SettingsSection>
    </motion.div>
  )
}
