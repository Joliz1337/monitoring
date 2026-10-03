import type { ServerWithMetrics } from '../stores/serversStore'

export interface FleetLoad {
  count: number
  cores: number
  cpuPercent: number
  ramUsed: number
  ramTotal: number
  ramPercent: number
  rx: number
  tx: number
}

// Считаются только онлайн-серверы: у оффлайн в metrics лежит последний
// закэшированный срез, и сумма завышалась бы устаревшими цифрами.
// CPU взвешен по логическим ядрам — простое среднее уравняло бы 2 ядра и 64
export function summarizeLoad(servers: ServerWithMetrics[]): FleetLoad | null {
  let count = 0
  let cores = 0
  let cpuWeighted = 0
  let ramUsed = 0
  let ramTotal = 0
  let rx = 0
  let tx = 0

  for (const s of servers) {
    if (!s.is_active || s.status !== 'online' || !s.metrics) continue
    const m = s.metrics
    count++
    const serverCores = m.cpu.cores_logical > 0 ? m.cpu.cores_logical : 1
    cores += serverCores
    cpuWeighted += (m.cpu.usage_percent || 0) * serverCores
    ramUsed += m.memory.ram.used || 0
    ramTotal += m.memory.ram.total || 0
    rx += m.network.total?.rx_bytes_per_sec || 0
    tx += m.network.total?.tx_bytes_per_sec || 0
  }

  if (count === 0) return null

  return {
    count,
    cores,
    cpuPercent: cpuWeighted / cores,
    ramUsed,
    ramTotal,
    ramPercent: ramTotal > 0 ? (ramUsed / ramTotal) * 100 : 0,
    rx,
    tx,
  }
}
