import type { NodeWindow } from '../api/client'
import type { LiveValuesMode } from '../config/chartDisplay'

/** То, что меняет режим средних: есть и в полном ответе ноды, и в срезе списка серверов */
interface WindowedMetrics {
  window?: Pick<NodeWindow, 'cpu_avg' | 'per_cpu_avg' | 'net_rx_avg' | 'net_tx_avg'> | null
  cpu: { usage_percent: number; per_cpu_percent: number[] }
  network: { total: { rx_bytes_per_sec: number; tx_bytes_per_sec: number } | null }
}

/**
 * Подменяет секундные показатели средними за интервал опроса из блока `window` ноды.
 * Без блока (старая нода, ответ на живой запрос) метрики остаются как есть.
 * Окно без измеренного CPU (пустой per_cpu_avg) CPU не трогает — там нули, а не среднее.
 */
export function withWindowAverages<M extends WindowedMetrics>(metrics: M): M {
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

export function viewMetrics<M extends WindowedMetrics>(metrics: M, mode: LiveValuesMode): M {
  return mode === 'average' ? withWindowAverages(metrics) : metrics
}
