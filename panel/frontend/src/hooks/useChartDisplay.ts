import { useMemo } from 'react'
import { useSettingsStore } from '../stores/settingsStore'
import { resolveChartDisplay, type ChartDisplay, type ChartMetric } from '../config/chartDisplay'

/** Режим графика для метрики: переопределение из настроек, иначе общий режим */
export function useChartDisplay(metric: ChartMetric): ChartDisplay {
  const mode = useSettingsStore(s => s.chartModeOverrides[metric] ?? s.chartMode)
  const showPeaks = useSettingsStore(s => s.chartPeaks)
  return useMemo(() => resolveChartDisplay(mode, showPeaks), [mode, showPeaks])
}
