import type { ServerCardMetrics } from '../src/api/client'
import type { ServerWithMetrics } from '../src/stores/serversStore'

export function fullMetrics(overrides: Partial<ServerCardMetrics> = {}): ServerCardMetrics {
  return {
    cpu: {
      usage_percent: 12.5,
      per_cpu_percent: [10, 15],
      cores_logical: 2,
      load_avg_1: 0.5,
      load_avg_5: 0.4,
      load_avg_15: 0.3,
    },
    memory: { ram: { percent: 40, used: 2 * 1024 ** 3, total: 5 * 1024 ** 3 } },
    disk: { partitions: [{ percent: 55, used: 11 * 1024 ** 3, total: 20 * 1024 ** 3 }] },
    network: { total: { rx_bytes_per_sec: 125_000, tx_bytes_per_sec: 250_000 }, interfaces: [] },
    system: { hostname: 'node-host', os: 'Ubuntu 24.04', uptime_seconds: 3 * 86400 + 2 * 3600 },
    ...overrides,
  }
}

export function makeServer(overrides: Partial<ServerWithMetrics> = {}): ServerWithMetrics {
  return {
    id: 1,
    name: 'srv-1',
    url: 'https://10.0.0.1:9100',
    position: 0,
    is_active: true,
    status: 'online',
    metrics: fullMetrics(),
    ...overrides,
  }
}

/** Срез, каким его отдаёт бэкенд, когда нода прислала не всё: тип обещает больше, чем есть */
export function brokenMetrics(shape: unknown): ServerCardMetrics {
  return shape as ServerCardMetrics
}

type ObserverCallback = (entries: Array<{ target: Element; isIntersecting: boolean }>) => void

/**
 * IntersectionObserver для jsdom: раскладки там нет, поэтому «рядом с экраном»
 * тест объявляет сам через moveSlots
 */
export class FakeIntersectionObserver {
  static instances: FakeIntersectionObserver[] = []

  observed = new Set<Element>()

  constructor(private readonly callback: ObserverCallback) {
    FakeIntersectionObserver.instances.push(this)
  }

  observe(target: Element) {
    this.observed.add(target)
  }

  unobserve(target: Element) {
    this.observed.delete(target)
  }

  disconnect() {
    this.observed.clear()
  }

  static observedSlots(): Element[] {
    return FakeIntersectionObserver.instances.flatMap(observer => [...observer.observed])
  }

  static moveSlots(targets: Element[], isIntersecting: boolean) {
    for (const observer of FakeIntersectionObserver.instances) {
      const entries = targets.filter(target => observer.observed.has(target)).map(target => ({ target, isIntersecting }))
      if (entries.length > 0) observer.callback(entries)
    }
  }
}
