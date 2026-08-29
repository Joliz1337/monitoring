import type { ServerMetrics } from '../api/client'
import type { LiveValuesMode } from '../config/chartDisplay'

/**
 * Подменяет секундные показатели средними за интервал опроса из блока `window` ноды.
 * Без блока (старая нода, ответ на живой запрос) метрики остаются как есть.
 * Окно без измеренного CPU (пустой per_cpu_avg) CPU не трогает — там нули, а не среднее.
 */
export function withWindowAverages(metrics: ServerMetrics): ServerMetrics {
  const window = metrics.window
  if (!window) return metrics

  const cpu = window.per_cpu_avg.length > 0
    ? { ...metrics.cpu, usage_percent: window.cpu_avg, per_cpu_percent: window.per_cpu_avg }
    : metrics.cpu
  const total = metrics.network.total
    ? { ...metrics.network.total, rx_bytes_per_sec: window.net_rx_avg, tx_bytes_per_sec: window.net_tx_avg }
    : metrics.network.total

  return { ...metrics, cpu, network: { ...metrics.network, total } }
}

export function viewMetrics(metrics: ServerMetrics, mode: LiveValuesMode): ServerMetrics {
  return mode === 'average' ? withWindowAverages(metrics) : metrics
}
