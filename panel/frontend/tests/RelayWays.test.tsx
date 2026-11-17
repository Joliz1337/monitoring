import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import RelayWays, { RelayWaysCard } from '../src/components/loss/RelayWays'
import { lossApi, type LossWay, type LossWaysResult } from '../src/api/client'

const TARGET = '176.97.210.52:8443'
const RELAY = { serverId: 7, serverName: 'Вк 84 1', target: TARGET }
const USED: LossWay = {
  local_ip: '146.185.243.157', used: true, loss_pct: 0, rtt_ms: 41.5, samples: 60, answers: false, span_sec: 120,
}
const SPARE: LossWay = {
  local_ip: '10.0.0.97', used: false, loss_pct: 3.3, rtt_ms: 38, samples: 30, answers: true, span_sec: 300,
}
const REFRESH_MS = 10_000

function answer(result: LossWaysResult) {
  return vi.spyOn(lossApi, 'ways').mockResolvedValue({ data: result } as Awaited<ReturnType<typeof lossApi.ways>>)
}

// Ответ приходит промисом: даём ему дойти до состояния компонента
const settle = () => act(async () => { await Promise.resolve() })

describe('потери с каждого IP релея', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    // В jsdom его нет, а всплывающая карточка следит им за своим размером
    vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  it('показывает выход правила и неиспользуемый адрес — с потерями, ответом и сроком', async () => {
    const ways = answer({ status: 'ok', ways: [USED, SPARE] })

    render(<RelayWays {...RELAY} />)
    await settle()

    expect(ways).toHaveBeenCalledWith(7, TARGET)
    expect(screen.getByText(`${TARGET} · релей «Вк 84 1»`)).toBeTruthy()
    const [used, spare] = screen.getAllByRole('listitem').map(row => row.textContent)
    for (const part of ['146.185.243.157', 'выход правила', 'потери 0 %', 'не отвечает', 'за 2 мин']) {
      expect(used).toContain(part)
    }
    for (const part of ['10.0.0.97', 'не используется', 'потери 3.3 %', 'отвечает', 'за 5 мин']) {
      expect(spare).toContain(part)
    }
    expect(spare).not.toContain('не отвечает')
  })

  it('адрес по умолчанию без IP назван словами, неизвестный ответ не выдумывается', async () => {
    answer({ status: 'ok', ways: [{ ...USED, local_ip: null, answers: null }] })

    render(<RelayWays {...RELAY} />)
    await settle()

    const [row] = screen.getAllByRole('listitem').map(item => item.textContent)
    expect(row).toContain('адрес по умолчанию')
    expect(row).not.toContain('отвечает')
  })

  it.each([
    ['unsupported', 'Нода не умеет — обновите ноду'],
    ['denied', 'Панели закрыт раздел «Система» этой ноды'],
    ['unreachable', 'Нода недоступна'],
  ] as const)('ответ «%s» объяснён словами', async (status, text) => {
    answer({ status, ways: [] })

    render(<RelayWays {...RELAY} />)
    await settle()

    expect(screen.getByText(text)).toBeTruthy()
    expect(screen.queryAllByRole('listitem')).toEqual([])
  })

  it('нода ещё не набрала попыток — так и сказано', async () => {
    answer({ status: 'ok', ways: [] })

    render(<RelayWays {...RELAY} />)
    await settle()

    expect(screen.getByText('Данных пока нет: нода ещё не набрала попыток')).toBeTruthy()
  })

  it('сбой запроса показан как недоступная нода, страница не падает', async () => {
    vi.spyOn(lossApi, 'ways').mockRejectedValue(new Error('network'))

    render(<RelayWays {...RELAY} />)
    await settle()

    expect(screen.getByText('Нода недоступна')).toBeTruthy()
  })

  it('пока открыта — перечитывает раз в 10 секунд, после закрытия — нет', async () => {
    const ways = answer({ status: 'ok', ways: [USED] })

    const { unmount } = render(<RelayWays {...RELAY} />)
    await settle()
    expect(ways).toHaveBeenCalledTimes(1)

    await act(async () => { vi.advanceTimersByTime(REFRESH_MS) })
    expect(ways).toHaveBeenCalledTimes(2)

    unmount()
    await act(async () => { vi.advanceTimersByTime(REFRESH_MS * 3) })
    expect(ways).toHaveBeenCalledTimes(2)
  })

  it('карточка встаёт сбоку от подписи и ловит курсор: соседние подписи не закрыты, IP можно выделить', async () => {
    answer({ status: 'ok', ways: [USED] })
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({
      left: 100, right: 180, top: 50, bottom: 70, width: 80, height: 20, x: 100, y: 50, toJSON: () => ({}),
    })

    render(<RelayWaysCard {...RELAY}><span>по IP релея</span></RelayWaysCard>)
    fireEvent.mouseEnter(screen.getByText('по IP релея'))
    await act(async () => { vi.advanceTimersByTime(1000) })
    await settle()

    const card = screen.getByRole('dialog')
    expect([card.style.left, card.style.top]).toEqual(['188px', '50px'])
    expect(card.className).not.toContain('pointer-events-none')
  })

  it('карточка спрашивает ноду, только когда её открыли', async () => {
    const ways = answer({ status: 'ok', ways: [USED] })

    render(<RelayWaysCard {...RELAY}><span>по IP релея</span></RelayWaysCard>)
    await act(async () => { vi.advanceTimersByTime(REFRESH_MS) })
    expect(ways).not.toHaveBeenCalled()

    fireEvent.mouseEnter(screen.getByText('по IP релея'))
    await act(async () => { vi.advanceTimersByTime(1000) })
    await settle()

    expect(ways).toHaveBeenCalledTimes(1)
    expect(screen.getByText('146.185.243.157')).toBeTruthy()
  })
})
