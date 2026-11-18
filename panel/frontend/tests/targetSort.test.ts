import { describe, expect, it } from 'vitest'
import { describeTargets, nextSort, sortTargets, type TargetSort } from '../src/components/loss/targetSort'
import type { LossTarget } from '../src/api/client'

function address(target: string, losses: number[], over: Partial<LossTarget> = {}): LossTarget {
  const [ip, port] = target.split(':')
  const relays = losses
    .map((loss, index) => ({ server_id: index + 1, name: `релей ${index + 1}`, rtt_ms: 40, samples: 60, loss_pct: loss, answers: null }))
    .sort((left, right) => right.loss_pct - left.loss_pct)
  return { target, ip, port: Number(port), owner: null, total_only: false, relays, worst_loss: relays[0].loss_pct, episode: null, quarantined: [], ...over }
}

// Порядок панели: как пришло
const ROWS = describeTargets([
  address('10.0.0.20:443', [30, 30, 0]),
  address('10.0.0.3:8443', [80, 0, 0], { episode: { level: 2, opened_at: 100 } }),
  address('10.0.0.3:443', [5, 5, 5]),
])
const order = (sort: TargetSort | null) => sortTargets(ROWS, sort).map(row => row.target.target)

describe('таблица адресов: порядок по нажатию на заголовок столбца', () => {
  it('пока столбец не выбран — порядок панели', () => {
    expect(order(null)).toEqual(['10.0.0.20:443', '10.0.0.3:8443', '10.0.0.3:443'])
  })

  it('по адресу — по числам, а не по буквам, затем по порту', () => {
    expect(order({ key: 'address', descending: false })).toEqual(['10.0.0.3:443', '10.0.0.3:8443', '10.0.0.20:443'])
    expect(order({ key: 'address', descending: true })).toEqual(['10.0.0.20:443', '10.0.0.3:8443', '10.0.0.3:443'])
  })

  it('по худшим потерям', () => {
    expect(order({ key: 'worst', descending: true })).toEqual(['10.0.0.3:8443', '10.0.0.20:443', '10.0.0.3:443'])
    expect(order({ key: 'worst', descending: false })).toEqual(['10.0.0.3:443', '10.0.0.20:443', '10.0.0.3:8443'])
  })

  it('по числу релеев с потерями', () => {
    expect(order({ key: 'relays', descending: true })).toEqual(['10.0.0.3:443', '10.0.0.20:443', '10.0.0.3:8443'])
  })

  it('по алерту — адрес с открытым алертом выше, остальные в порядке панели', () => {
    expect(order({ key: 'episode', descending: true })).toEqual(['10.0.0.3:8443', '10.0.0.20:443', '10.0.0.3:443'])
  })

  it('исходный список не переставляется', () => {
    sortTargets(ROWS, { key: 'address', descending: false })
    expect(ROWS.map(row => row.target.target)).toEqual(['10.0.0.20:443', '10.0.0.3:8443', '10.0.0.3:443'])
  })

  it('первое нажатие — интересное сверху, повторное — в обратную сторону, другой столбец — заново', () => {
    const worst = nextSort(null, 'worst')
    expect(worst).toEqual({ key: 'worst', descending: true })
    expect(nextSort(worst, 'worst')).toEqual({ key: 'worst', descending: false })
    expect(nextSort(worst, 'address')).toEqual({ key: 'address', descending: false })
    expect(nextSort({ key: 'address', descending: false }, 'address')).toEqual({ key: 'address', descending: true })
  })
})
