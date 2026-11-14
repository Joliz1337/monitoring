import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import ServerCard, { areServerCardPropsEqual, type ServerCardProps } from '../src/components/Dashboard/ServerCard'
import { brokenMetrics, fullMetrics, makeServer } from './helpers'

const NOW = new Date('2026-01-01T12:00:00Z')
const secondsAgo = (seconds: number) => new Date(NOW.getTime() - seconds * 1000).toISOString()

function renderCards(...cards: ServerCardProps[]) {
  const tree = (list: ServerCardProps[]) => (
    <MemoryRouter>
      {list.map(props => <ServerCard key={props.server.id} {...props} />)}
    </MemoryRouter>
  )
  const view = render(tree(cards))
  return { ...view, update: (...next: ServerCardProps[]) => view.rerender(tree(next)) }
}

afterEach(() => {
  vi.useRealTimers()
})

describe('ServerCard: неполные метрики', () => {
  // Что отдаёт бэкенд, когда нода прислала пустой ответ: блоки на месте, чисел нет
  const emptyBlocks = brokenMetrics({
    cpu: {},
    memory: { ram: {} },
    disk: { partitions: [{}] },
    network: { total: {}, interfaces: [] },
    system: {},
  })
  const missingBlocks = brokenMetrics({ cpu: {} })

  const views: Array<Pick<ServerCardProps, 'compact' | 'detailLevel'>> = [
    { compact: true },
    { detailLevel: 'minimal' },
    { detailLevel: 'standard' },
    { detailLevel: 'detailed' },
  ]

  for (const view of views) {
    const viewName = view.compact ? 'compact' : view.detailLevel

    it(`вид ${viewName}: вместо пропущенных чисел прочерки`, () => {
      renderCards({ server: makeServer({ metrics: emptyBlocks }), ...view })

      expect(screen.getByText('srv-1')).toBeTruthy()
      expect(screen.getAllByText('—').length).toBeGreaterThanOrEqual(2)
      expect(screen.queryByText(/NaN|undefined/)).toBeNull()
    })

    it(`вид ${viewName}: карточка рисуется без блоков памяти, диска, сети и системы`, () => {
      renderCards({ server: makeServer({ metrics: missingBlocks }), ...view })

      expect(screen.getByText('srv-1')).toBeTruthy()
      expect(screen.getAllByText('—').length).toBeGreaterThanOrEqual(2)
      expect(screen.queryByText(/NaN|undefined/)).toBeNull()
    })
  }

  it('скорость сети без данных — прочерк, а не ноль', () => {
    renderCards({ server: makeServer({ metrics: missingBlocks }), detailLevel: 'standard' })

    expect(screen.getByText('↓ —')).toBeTruthy()
    expect(screen.getByText('↑ —')).toBeTruthy()
  })

  it('полные метрики показываются как раньше', () => {
    renderCards({ server: makeServer(), detailLevel: 'standard' })

    expect(screen.getByText('12.5%')).toBeTruthy()
    expect(screen.getByText('40.0%')).toBeTruthy()
    expect(screen.getByText('2 GB / 5 GB')).toBeTruthy()
    expect(screen.getByText('3d 2h')).toBeTruthy()
    expect(screen.getByText('0.50')).toBeTruthy()
    expect(screen.queryByText('—')).toBeNull()
  })
})

describe('ServerCard: граница ошибок', () => {
  // Число вместо массива ядер: такого карточка не ждёт и падает на .map
  const unrenderable = brokenMetrics({ ...fullMetrics(), cpu: { ...fullMetrics().cpu, per_cpu_percent: 5 } })

  it('сломанная карточка заменяется заглушкой, соседние остаются на месте', () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    const healthy = { server: makeServer({ id: 2, name: 'srv-healthy' }), detailLevel: 'detailed' as const }

    renderCards({ server: makeServer({ id: 1, name: 'srv-broken', metrics: unrenderable }), detailLevel: 'detailed' }, healthy)

    expect(screen.getByText('srv-broken')).toBeTruthy()
    expect(screen.getByText('Не удалось показать карточку этого сервера')).toBeTruthy()
    expect(screen.getByText('srv-healthy')).toBeTruthy()
    expect(screen.getByText('12.5%')).toBeTruthy()
  })

  it('с новыми данными сервера карточка рисуется снова', () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    const view = renderCards({ server: makeServer({ metrics: unrenderable }), detailLevel: 'detailed' })
    expect(screen.getByText('Не удалось показать карточку этого сервера')).toBeTruthy()

    view.update({ server: makeServer(), detailLevel: 'detailed' })

    expect(screen.queryByText('Не удалось показать карточку этого сервера')).toBeNull()
    expect(screen.getByText('12.5%')).toBeTruthy()
  })
})

describe('ServerCard: оффлайн-сервер', () => {
  const offline = (overrides: Parameters<typeof makeServer>[0] = {}) =>
    makeServer({ status: 'offline', metrics: null, last_error: 'Connection refused', ...overrides })

  it('перерисовывается, когда меняется текст и код ошибки', () => {
    const view = renderCards({ server: offline({ error_code: 502 }) })
    expect(screen.getByText('Connection refused')).toBeTruthy()
    expect(screen.getByText('Код ошибки: 502')).toBeTruthy()

    view.update({ server: offline({ last_error: 'Timeout', error_code: 504 }) })

    expect(screen.getByText('Timeout')).toBeTruthy()
    expect(screen.getByText('Код ошибки: 504')).toBeTruthy()
    expect(screen.queryByText('Connection refused')).toBeNull()
  })

  it('перерисовывается, когда меняется last_seen', () => {
    vi.useFakeTimers()
    vi.setSystemTime(NOW)
    const view = renderCards({ server: offline({ last_seen: secondsAgo(5 * 60) }) })
    expect(screen.getByText('Был в сети: 5m ago')).toBeTruthy()

    view.update({ server: offline({ last_seen: secondsAgo(2 * 60) }) })

    expect(screen.getByText('Был в сети: 2m ago')).toBeTruthy()
  })

  it('«был в сети» идёт вперёд само, без новых данных', () => {
    vi.useFakeTimers()
    vi.setSystemTime(NOW)
    renderCards({ server: offline({ last_seen: secondsAgo(60) }) })
    expect(screen.getByText('Был в сети: 1m ago')).toBeTruthy()

    act(() => {
      vi.advanceTimersByTime(2 * 60 * 1000)
    })

    expect(screen.getByText('Был в сети: 3m ago')).toBeTruthy()
  })

  it('перерисовывается, когда сервер переименовали или сменили адрес', () => {
    const view = renderCards({ server: offline() })

    view.update({ server: offline({ name: 'srv-renamed', url: 'https://10.0.0.99:9100' }) })

    expect(screen.getByText('srv-renamed')).toBeTruthy()
    expect(screen.getByText('10.0.0.99')).toBeTruthy()
  })
})

describe('areServerCardPropsEqual', () => {
  it('те же данные в новых объектах — карточка не перерисовывается', () => {
    expect(areServerCardPropsEqual({ server: makeServer() }, { server: makeServer() })).toBe(true)
  })

  it('неполные метрики сравниваются без исключений', () => {
    const partial = { server: makeServer({ metrics: brokenMetrics({ cpu: {} }) }), detailLevel: 'detailed' as const }

    expect(areServerCardPropsEqual(partial, { ...partial, server: { ...partial.server } })).toBe(true)
    expect(areServerCardPropsEqual(partial, { server: makeServer(), detailLevel: 'detailed' })).toBe(false)
  })
})
