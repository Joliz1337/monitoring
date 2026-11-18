import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import Traffic from '../src/pages/Traffic'
import {
  proxyApi, serversApi, trafficApi,
  type HistoryResponse, type ServerMetrics, type TrafficSeries, type TrafficStatus, type TrafficSummary,
} from '../src/api/client'
import { useServersStore } from '../src/stores/serversStore'
import { makeServer } from './helpers'

vi.mock('../src/components/FAQ', () => ({ FAQIcon: () => null }))
// apexcharts в jsdom не рисуется, а странице важно одно: с каким периодом она просит подписи дат
vi.mock('../src/components/Charts/MultiLineChart', () => ({
  default: ({ period }: { period?: string }) => <div data-testid="chart" data-period={period} />,
}))

const WAIT = { timeout: 5000 }
const SERVER_ID = 5
const SILENT_SINCE = '2026-11-15T18:02:46.000Z'
const TRACKED_PORT = 443
const GIGABYTE = 1024 ** 3

const BANNER_TITLE = 'Сервер не отвечает'
const LAST_DATA_NOTE = /Показаны последние данные, которые панель получила от него/
const CHARTS_FROZEN_NOTE = /^Графики — за последний период работы сервера, до/
const TRAFFIC_FROZEN_NOTE = /^Трафик — за последний период работы сервера, до/
const DAILY_TOTALS_NOTE = /\. Часовой истории за это время уже нет: показаны суточные итоги за последний месяц работы$/
const SNAPSHOT_NOTE = /^снимок на /
const ANY_STALE_NOTE = /за последний период работы сервера|снимок на /
const PORTS_LOCKED_NOTE = /Менять здесь можно будет, когда он вернётся/

// Тесту нужны только data: остальные поля ответа axios страница не читает
const respond = <T,>(data: T) => Promise.resolve({ data }) as never

const status: TrafficStatus = {
  supported: true, node_version: '10.36.1', min_version: '10.13.0', ips_supported: true, min_ip_version: '10.33.0',
  import: { status: 'imported', imported_at: null }, first_data_at: null,
}

// Из ответа метрик странице трафика нужны только соединения, эфемерные порты и пометка о молчании
const metrics = (patch: Partial<ServerMetrics> = {}) => ({
  system: {
    connections_detailed: {
      tcp: { total: 120, established: 90, listen: 12, time_wait: 10, close_wait: 3, syn_sent: 2, fin_wait: 3, other: 0 },
      udp: { total: 7 },
    },
    ephemeral_ports: {
      range_low: 32768, range_high: 60999, reserved: 0, capacity: 28232, tw_reuse: 1,
      sources: [{
        ip: '192.0.2.10', used: 150, time_wait: 20, destinations_total: 1,
        destinations: [{ ip: '198.51.100.7', port: 443, used: 150, time_wait: 20, held: 170, free: 28062 }],
      }],
    },
  },
  ...patch,
}) as unknown as ServerMetrics

const history = (patch: Partial<HistoryResponse> = {}): HistoryResponse => ({
  period: '1h', data_source: 'raw', bucket_sec: null,
  from_time: '2026-11-15T17:02:46Z', to_time: '2026-11-15T18:02:46Z', count: 0, data: [], gaps: [],
  ...patch,
})

const summary = (patch: Partial<TrafficSummary> = {}): TrafficSummary => ({
  days: 30,
  total: { rx_bytes: 5 * GIGABYTE, tx_bytes: 3 * GIGABYTE, days: 30 },
  by_interface: [{ interface: 'eth0', rx_bytes: 5 * GIGABYTE, tx_bytes: 3 * GIGABYTE }],
  by_port: [{ port: TRACKED_PORT, rx_bytes: GIGABYTE, tx_bytes: GIGABYTE }],
  by_ip: [{ ip: '192.0.2.10', rx_bytes: GIGABYTE, tx_bytes: GIGABYTE }],
  tracked_ports: [TRACKED_PORT],
  ...patch,
})

const series = (patch: Partial<TrafficSeries> = {}): TrafficSeries => ({
  period: '24h', scope: 'total', key: '',
  points: [{ timestamp: '2026-11-15T17:00:00Z', rx: 1024, tx: 2048 }], gaps: [],
  ...patch,
})

interface PanelAnswers {
  metrics: ServerMetrics
  history: HistoryResponse
  summary: TrafficSummary
  series: TrafficSeries
}

const LIVE: PanelAnswers = { metrics: metrics(), history: history(), summary: summary(), series: series() }
const SILENT: PanelAnswers = {
  metrics: metrics({ node_silent_since: SILENT_SINCE }),
  history: history({ frozen_at: SILENT_SINCE }),
  summary: summary({ frozen_at: SILENT_SINCE }),
  series: series({ frozen_at: SILENT_SINCE }),
}

async function openPage(answers: PanelAnswers) {
  vi.spyOn(trafficApi, 'getStatus').mockImplementation(() => respond(status))
  vi.spyOn(trafficApi, 'getSummary').mockImplementation(() => respond(answers.summary))
  vi.spyOn(trafficApi, 'getSeries').mockImplementation(() => respond(answers.series))
  vi.spyOn(proxyApi, 'getMetrics').mockImplementation(() => respond(answers.metrics))
  vi.spyOn(proxyApi, 'getHistory').mockImplementation(() => respond(answers.history))
  render(
    <MemoryRouter initialEntries={[`/panel/server/${SERVER_ID}/traffic`]}>
      <Routes>
        <Route path="/:uid/server/:serverId/traffic" element={<Traffic />} />
      </Routes>
    </MemoryRouter>,
  )
  await screen.findByText('История трафика', {}, WAIT)
}

const cardTitled = (title: string) => screen.getByText(title).closest('.card') as HTMLElement
const trafficChartPeriod = () => within(cardTitled('История трафика')).getByTestId('chart').dataset.period

const addPortButton = () => screen.getByRole('button', { name: 'Добавить порт' }) as HTMLButtonElement
const removePortButton = () => {
  const row = within(cardTitled('Отслеживание портов')).getByText(`:${TRACKED_PORT}`).closest('.rounded-lg') as HTMLElement
  return within(row).getByRole('button') as HTMLButtonElement
}
const typeNewPort = () =>
  fireEvent.change(screen.getByPlaceholderText('Номер порта (напр. 80, 443)'), { target: { value: '8443' } })

beforeEach(() => {
  useServersStore.setState({ servers: [], isLoading: false, error: null, metricsUpdatedAt: null })
  vi.spyOn(serversApi, 'list').mockImplementation(
    () => respond({ count: 1, servers: [makeServer({ id: SERVER_ID, name: 'srv-5' })] }),
  )
})

describe('Трафик, когда нода не отвечает', () => {
  it('плашка вверху страницы говорит, что показаны последние данные', async () => {
    await openPage(SILENT)

    expect(screen.getByText(BANNER_TITLE)).toBeTruthy()
    expect(screen.getByText(LAST_DATA_NOTE)).toBeTruthy()
  })

  it('графики скорости и состояний TCP подписаны моментом последнего ответа', async () => {
    await openPage(SILENT)

    expect(within(cardTitled('Скорость сети')).getByText(CHARTS_FROZEN_NOTE)).toBeTruthy()
  })

  it('история трафика подписана моментом последнего ответа, часовые точки — с часовыми датами', async () => {
    await openPage(SILENT)

    const note = within(cardTitled('История трафика')).getByText(TRAFFIC_FROZEN_NOTE)
    expect(note.textContent).not.toMatch(DAILY_TOTALS_NOTE)
    expect(trafficChartPeriod()).toBe('24h')
  })

  it('без часовых бакетов история трафика — суточные итоги с суточными датами', async () => {
    await openPage({ ...SILENT, series: series({ frozen_at: SILENT_SINCE, coarser_than_period: true }) })

    const note = within(cardTitled('История трафика')).getByText(TRAFFIC_FROZEN_NOTE)
    expect(note.textContent).toMatch(DAILY_TOTALS_NOTE)
    expect(trafficChartPeriod()).toBe('30d')
  })

  it('итоги за 30 дней и таблицы подписаны, от какого момента отсчитаны дни', async () => {
    await openPage({ ...SILENT, series: series() })

    const notes = screen.getAllByText(TRAFFIC_FROZEN_NOTE)
    expect(notes).toHaveLength(2)

    const [totalsNote, tablesNote] = notes
    const follows = (earlier: Element, later: Element) =>
      Boolean(earlier.compareDocumentPosition(later) & Node.DOCUMENT_POSITION_FOLLOWING)
    expect(follows(totalsNote, screen.getByText('Всего загружено (30д)'))).toBe(true)
    expect(follows(tablesNote, screen.getByText('Трафик по интерфейсам (30д)'))).toBe(true)
    expect(follows(screen.getByText('История трафика'), tablesNote)).toBe(true)
  })

  it('соединения и эфемерные порты показаны снимком, без «живой» точки', async () => {
    await openPage(SILENT)

    expect(within(cardTitled('TCP соединения')).getByText(SNAPSHOT_NOTE)).toBeTruthy()
    expect(within(cardTitled('UDP сокеты')).getByText(SNAPSHOT_NOTE)).toBeTruthy()
    expect(within(cardTitled('Эфемерные порты')).getByText(SNAPSHOT_NOTE)).toBeTruthy()
    expect(cardTitled('UDP сокеты').querySelector('.animate-pulse')).toBeNull()
  })

  it('добавление и удаление отслеживаемого порта отключены и объяснены', async () => {
    await openPage(SILENT)
    typeNewPort()

    expect(addPortButton().disabled).toBe(true)
    expect(removePortButton().disabled).toBe(true)
    expect(within(cardTitled('Отслеживание портов')).getByText(PORTS_LOCKED_NOTE)).toBeTruthy()
  })
})

describe('Трафик отвечающей ноды', () => {
  it('без плашки и пометок об устаревших данных', async () => {
    await openPage(LIVE)

    expect(screen.queryByText(BANNER_TITLE)).toBeNull()
    expect(screen.queryByText(ANY_STALE_NOTE)).toBeNull()
    expect(screen.queryByText(PORTS_LOCKED_NOTE)).toBeNull()
    expect(cardTitled('UDP сокеты').querySelector('.animate-pulse')).not.toBeNull()
    expect(trafficChartPeriod()).toBe('24h')
  })

  it('отслеживаемые порты можно добавлять и удалять', async () => {
    const removeTrackedPort = vi.spyOn(proxyApi, 'removeTrackedPort').mockImplementation(
      () => respond({ success: true, message: '' }),
    )
    await openPage(LIVE)
    typeNewPort()

    expect(addPortButton().disabled).toBe(false)
    fireEvent.click(removePortButton())

    await waitFor(() => expect(removeTrackedPort).toHaveBeenCalledWith(SERVER_ID, TRACKED_PORT), WAIT)
  })
})
