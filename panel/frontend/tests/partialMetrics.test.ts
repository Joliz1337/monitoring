import { describe, expect, it } from 'vitest'
import { summarizeLoad } from '../src/utils/fleetLoad'
import { matchesServerSearch } from '../src/utils/serverSearch'
import { brokenMetrics, makeServer } from './helpers'

// Срез, каким его собирает card_metrics() для ноды с пустым ответом: блоки есть, чисел нет.
// Карточка такое рисует прочерками, но те же метрики читают сводка парка и поиск
const emptyBlocks = brokenMetrics({
  cpu: {},
  memory: { ram: {} },
  disk: { partitions: [] },
  network: { total: null, interfaces: [] },
  system: {},
})

describe('срез метрик без чисел', () => {
  it('не ломает сводку парка и считается сервером без нагрузки', () => {
    const load = summarizeLoad([makeServer({ id: 1, metrics: emptyBlocks }), makeServer({ id: 2 })])

    expect(load).toMatchObject({ count: 2, cores: 3, ramUsed: 2 * 1024 ** 3, rx: 125_000 })
    expect(load?.cpuPercent).toBeCloseTo((12.5 * 2) / 3)
  })

  it('не ломает поиск по имени и адресу', () => {
    const server = makeServer({ metrics: emptyBlocks })

    expect(matchesServerSearch(server, 'srv')).toBe(true)
    expect(matchesServerSearch(server, '192.168')).toBe(false)
  })
})
