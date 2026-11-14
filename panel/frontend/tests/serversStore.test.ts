import { beforeEach, describe, expect, it, vi } from 'vitest'
import { serversApi } from '../src/api/client'
import { useServersStore } from '../src/stores/serversStore'
import { useSettingsStore } from '../src/stores/settingsStore'
import { brokenMetrics, fullMetrics, makeServer } from './helpers'

type ListResponse = Awaited<ReturnType<typeof serversApi.list>>

const listResponse = (servers: ReturnType<typeof makeServer>[]) =>
  ({ data: { count: servers.length, servers } }) as unknown as ListResponse

describe('serversStore.fetchServersWithMetrics', () => {
  beforeEach(() => {
    useServersStore.setState({ servers: [], isLoading: false, error: null, metricsUpdatedAt: null })
    useSettingsStore.setState({ liveValues: 'instant' })
  })

  it('запоминает ошибку неудачного опроса и оставляет прежние серверы', async () => {
    const list = vi.spyOn(serversApi, 'list')
    list.mockResolvedValueOnce(listResponse([makeServer()]))
    await useServersStore.getState().fetchServersWithMetrics()
    const updatedAt = useServersStore.getState().metricsUpdatedAt

    list.mockRejectedValueOnce(new Error('Network Error'))
    await useServersStore.getState().fetchServersWithMetrics()

    const state = useServersStore.getState()
    expect(state.error).toBe('Network Error')
    expect(state.isLoading).toBe(false)
    expect(state.servers).toHaveLength(1)
    expect(state.metricsUpdatedAt).toBe(updatedAt)
  })

  it('держит ошибку, пока идёт следующий запрос, и снимает её после успешного ответа', async () => {
    const list = vi.spyOn(serversApi, 'list')
    list.mockRejectedValueOnce(new Error('Network Error'))
    await useServersStore.getState().fetchServersWithMetrics()

    let respond: (response: ListResponse) => void = () => {}
    list.mockReturnValueOnce(new Promise<ListResponse>(resolve => { respond = resolve }))
    const retry = useServersStore.getState().fetchServersWithMetrics()
    expect(useServersStore.getState().error).toBe('Network Error')

    respond(listResponse([makeServer()]))
    await retry

    const state = useServersStore.getState()
    expect(state.error).toBeNull()
    expect(state.metricsUpdatedAt).toBeInstanceOf(Date)
    expect(state.servers[0].status).toBe('online')
  })

  it('в режиме средних подменяет секундные значения средними за окно', async () => {
    useSettingsStore.setState({ liveValues: 'average' })
    const metrics = fullMetrics({ window: { cpu_avg: 77, per_cpu_avg: [70, 84], net_rx_avg: 10, net_tx_avg: 20 } })
    vi.spyOn(serversApi, 'list').mockResolvedValueOnce(listResponse([makeServer({ metrics })]))

    await useServersStore.getState().fetchServersWithMetrics()

    const stored = useServersStore.getState().servers[0].metrics
    expect(stored?.cpu.usage_percent).toBe(77)
    expect(stored?.network.total?.rx_bytes_per_sec).toBe(10)
  })

  it('сервер с неполным окном не срывает обновление всего списка', async () => {
    useSettingsStore.setState({ liveValues: 'average' })
    const partialWindow = makeServer({
      id: 1,
      metrics: fullMetrics({ window: brokenMetrics({ window: { cpu_avg: 50 } }).window }),
    })
    const noNetwork = makeServer({
      id: 2,
      metrics: brokenMetrics({ cpu: {}, window: { cpu_avg: 1, per_cpu_avg: [1], net_rx_avg: 1, net_tx_avg: 1 } }),
    })
    vi.spyOn(serversApi, 'list').mockResolvedValueOnce(listResponse([partialWindow, noNetwork]))

    await useServersStore.getState().fetchServersWithMetrics()

    const state = useServersStore.getState()
    expect(state.error).toBeNull()
    expect(state.servers).toHaveLength(2)
    expect(state.servers[0].metrics?.cpu.usage_percent).toBe(12.5)
  })
})

describe('serversStore.fetchServers', () => {
  beforeEach(() => {
    useServersStore.setState({ servers: [], isLoading: false, error: null, metricsUpdatedAt: null })
  })

  it('ставит ошибку при сбое и снимает после успешного ответа', async () => {
    const list = vi.spyOn(serversApi, 'list')
    list.mockRejectedValueOnce(new Error('timeout of 30000ms exceeded'))
    await useServersStore.getState().fetchServers()
    expect(useServersStore.getState().error).toBe('timeout of 30000ms exceeded')

    list.mockResolvedValueOnce(listResponse([makeServer()]))
    await useServersStore.getState().fetchServers()
    expect(useServersStore.getState().error).toBeNull()
  })
})
