import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, render, screen } from '@testing-library/react'
import BackendLoss, { useAllWays } from '../src/components/loss/BackendLoss'
import { lossApi, type LossAllWaysResult, type LossWay } from '../src/api/client'

const TARGET = '176.97.210.52:8443'
const USED: LossWay = { local_ip: '146.185.243.157', used: true, loss_pct: 0, rtt_ms: 41, samples: 60, answers: false, span_sec: 120 }
const SPARE: LossWay = { local_ip: '10.0.0.97', used: false, loss_pct: 0, rtt_ms: 38, samples: 30, answers: true, span_sec: 60 }
const PROBE = { loss_pct: 100, rtt_ms: 41, samples: 60, answers: false }
const REFRESH_MS = 15_000

const show = (ways?: LossWay[], addr: string | undefined = TARGET) =>
  render(<BackendLoss serverId={7} serverName="Вк 84 1" addr={addr} probe={PROBE} ways={ways} />)

function answer(result: LossAllWaysResult) {
  return vi.spyOn(lossApi, 'allWays').mockResolvedValue({ data: result } as Awaited<ReturnType<typeof lossApi.allWays>>)
}

function Probe({ enabled = true }: { enabled?: boolean }) {
  const ways = useAllWays(7, enabled)
  return <p>{[...ways.keys()].join(',') || 'пусто'}</p>
}

describe('страница HAProxy: потери бэкенда и вывод по другим IP релея', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('с выхода молчит, с другого адреса отвечает — значок виден сразу, без наведения', () => {
    show([USED, SPARE])

    expect(screen.getByText('не отвечает')).toBeTruthy()
    const mark = screen.getByText('с другого IP отвечает')
    expect(mark.getAttribute('title')).toContain('с 10.0.0.97 отвечает')
  })

  it('потери с выхода и чистый другой адрес — «с другого IP лучше»', () => {
    show([{ ...USED, answers: null, loss_pct: 60 }, { ...SPARE, answers: null }])

    expect(screen.getByText('с другого IP лучше')).toBeTruthy()
  })

  it('данных по адресам нет или вывода нет — только потери', () => {
    show(undefined)
    expect(screen.queryByText(/с другого IP/)).toBeNull()

    show([USED, { ...SPARE, answers: false }])
    expect(screen.queryByText(/с другого IP/)).toBeNull()
  })

  it('строка без адреса показывает одни потери', () => {
    show([USED, SPARE], '')

    expect(screen.getByText('не отвечает')).toBeTruthy()
    expect(screen.queryByText(/с другого IP/)).toBeNull()
  })

  it('разбивка по всем бэкендам читается одним запросом и обновляется, пока страница открыта', async () => {
    const all = answer({ status: 'ok', targets: [{ target: TARGET, ways: [USED, SPARE] }] })

    const { unmount } = render(<Probe />)
    await act(async () => { await Promise.resolve() })
    expect(screen.getByText(TARGET)).toBeTruthy()
    expect(all).toHaveBeenCalledWith(7)

    await act(async () => { vi.advanceTimersByTime(REFRESH_MS) })
    expect(all).toHaveBeenCalledTimes(2)

    unmount()
    await act(async () => { vi.advanceTimersByTime(REFRESH_MS * 3) })
    expect(all).toHaveBeenCalledTimes(2)
  })

  it('нода без запроса больше не опрашивается: до обновления она ему не научится', async () => {
    const all = answer({ status: 'unsupported', targets: [] })

    render(<Probe />)
    await act(async () => { await Promise.resolve() })
    await act(async () => { vi.advanceTimersByTime(REFRESH_MS * 4) })

    expect(all).toHaveBeenCalledTimes(1)
    expect(screen.getByText('пусто')).toBeTruthy()
  })

  it('сбой запроса страницу не роняет и повторяется позже; без статистики запроса нет', async () => {
    const all = vi.spyOn(lossApi, 'allWays').mockRejectedValue(new Error('network'))

    const { unmount } = render(<Probe />)
    await act(async () => { await Promise.resolve() })
    await act(async () => { vi.advanceTimersByTime(REFRESH_MS) })
    expect(all).toHaveBeenCalledTimes(2)
    unmount()

    all.mockClear()
    render(<Probe enabled={false} />)
    await act(async () => { vi.advanceTimersByTime(REFRESH_MS) })
    expect(all).not.toHaveBeenCalled()
  })
})
