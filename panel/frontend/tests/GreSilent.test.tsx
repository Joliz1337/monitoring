import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import GreTunnels from '../src/pages/GreTunnels'
import { greApi, proxyApi, serversApi, type GreNodeView, type GreTunnelView } from '../src/api/client'
import { useServersStore } from '../src/stores/serversStore'
import { makeServer } from './helpers'

vi.mock('../src/components/FAQ', () => ({ FAQIcon: () => null }))

const WAIT = { timeout: 5000 }
const SERVER_ID = 5
const MINUTE_MS = 60_000
const LAST_STATE_AT = '2026-11-15T18:02:46.000Z'
const LAST_STATE = Date.parse(LAST_STATE_AT)
const CE = '203.0.113.2'

const BANNER_TITLE = 'Сервер не отвечает'
const QUEUED_NOTE = /Показано состояние, которое панель получила от него .+ Изменения сохраняются в панели/
const LOAD_ERROR = 'Не удалось получить GRE-туннели'

// Тесту нужны только data: остальные поля ответа axios страница не читает
const respond = <T,>(data: T) => Promise.resolve({ data }) as never
const refuse = () => Promise.reject({ response: { status: 502, data: {} } }) as never

const minutesBefore = (moment: number, minutes: number) => new Date(moment - minutes * MINUTE_MS).toISOString()

const tunnel = (patch: Partial<GreTunnelView> = {}): GreTunnelView => ({
  slot: 1, name: 'gre-ddg1', local: '192.0.2.10', remote: '185.129.101.255', pe: '203.0.113.1', ce: CE,
  health_net: '198.18.100.0/24', link: 'ok', mtu: 1476, conflict_with: null,
  rx_bytes: 4096, tx_bytes: 8192, rx_packets: 40, tx_packets: 80,
  activity: 'alive', last_rx_at: minutesBefore(LAST_STATE, 2),
  ...patch,
})

const node = (patch: Partial<GreNodeView> = {}): GreNodeView => ({
  server_id: SERVER_ID, name: 'srv-5', online: true, node_version: '10.36.1', min_node_version: '10.35.0',
  supported_by_node: true, install_status: 'active', sync_error: null, node_error: null, notify: true,
  tunnels: [tunnel()], max_tunnels: 8, default_remote: '185.129.101.255', default_health_net: '198.18.100.0/24',
  silent_after_seconds: 300, last_state_at: LAST_STATE_AT,
  ...patch,
})

function renderPage() {
  render(
    <MemoryRouter initialEntries={[`/panel/server/${SERVER_ID}/gre`]}>
      <Routes>
        <Route path="/:uid/server/:serverId/gre" element={<GreTunnels />} />
      </Routes>
    </MemoryRouter>,
  )
}

async function openPage(view: GreNodeView) {
  const getNode = vi.spyOn(greApi, 'getNode').mockImplementation(() => respond(view))
  renderPage()
  await screen.findByText(CE, {}, WAIT)
  return getNode
}

/** Вкладку свернули и открыли снова: страница перечитывает туннели, не дожидаясь таймера опроса */
async function pollAgain(getNode: { mock: { calls: unknown[] } }) {
  const pollsBefore = getNode.mock.calls.length
  const hidden = vi.spyOn(document, 'hidden', 'get').mockReturnValue(true)
  act(() => { document.dispatchEvent(new Event('visibilitychange')) })
  hidden.mockReturnValue(false)
  act(() => { document.dispatchEvent(new Event('visibilitychange')) })
  await waitFor(() => expect(getNode.mock.calls.length).toBeGreaterThan(pollsBefore), WAIT)
  await act(async () => {})
}

const button = (name: string) => screen.getByRole('button', { name }) as HTMLButtonElement

beforeEach(() => {
  useServersStore.setState({ servers: [], isLoading: false, error: null, metricsUpdatedAt: null })
  vi.spyOn(serversApi, 'list').mockImplementation(
    () => respond({ count: 1, servers: [makeServer({ id: SERVER_ID, name: 'srv-5' })] }),
  )
  // Адреса интерфейсов нужны только форме туннеля, а её тесты не открывают
  vi.spyOn(proxyApi, 'getNetworkState').mockImplementation(refuse)
})

describe('GRE-туннели, когда нода не отвечает', () => {
  it('плашка говорит, на какой момент показано состояние и что правки доедут позже', async () => {
    await openPage(node({ online: false }))

    expect(screen.getByText(BANNER_TITLE)).toBeTruthy()
    expect(screen.getByText(QUEUED_NOTE)).toBeTruthy()
  })

  it('без сохранённого состояния плашка остаётся с одним заголовком', async () => {
    await openPage(node({ online: false, last_state_at: null }))

    expect(screen.getByText(BANNER_TITLE)).toBeTruthy()
    expect(screen.queryByText(QUEUED_NOTE)).toBeNull()
  })

  it('«Проверить» и «Обновить» отключены: им нужна живая нода', async () => {
    await openPage(node({ online: false }))

    expect(button('Проверить').disabled).toBe(true)
    expect(button('Обновить данные').disabled).toBe(true)
  })

  it('правка туннелей не блокируется: она сохраняется в панели', async () => {
    const view = node({ online: false })
    const updateNode = vi.spyOn(greApi, 'updateNode').mockImplementation(() => respond({ ...view, notify: false }))
    await openPage(view)

    expect(button('Редактировать').disabled).toBe(false)
    expect(button('Удалить').disabled).toBe(false)
    expect(button('Добавить ещё туннель').disabled).toBe(false)

    fireEvent.click(screen.getByTitle(/Сообщение в Telegram/))

    await waitFor(() => expect(updateNode).toHaveBeenCalledWith(SERVER_ID, { notify: false }), WAIT)
  })

  it('минуты без пакетов отсчитаны до последнего состояния ноды, а не до «сейчас»', async () => {
    await openPage(node({
      online: false,
      tunnels: [
        tunnel({ activity: 'alive', last_rx_at: minutesBefore(LAST_STATE, 2) }),
        tunnel({
          slot: 2, name: 'gre-ddg2', pe: '203.0.113.5', ce: '203.0.113.6',
          activity: 'silent', last_rx_at: minutesBefore(LAST_STATE, 7),
        }),
      ],
    }))

    expect(screen.getByText('Пакеты идут — последний 2 минуты назад')).toBeTruthy()
    expect(screen.getByText(/^Пакетов нет уже 7 минут —/)).toBeTruthy()
  })

  it('сбой очередного опроса не убирает показанные туннели: ошибка встаёт над ними', async () => {
    const getNode = await openPage(node({ online: false }))
    getNode.mockImplementation(refuse)

    await pollAgain(getNode)

    expect(screen.getByText(CE)).toBeTruthy()
    expect(screen.getByText(BANNER_TITLE)).toBeTruthy()
    expect(screen.getByText(LOAD_ERROR)).toBeTruthy()

    getNode.mockImplementation(() => respond(node({ online: false })))
    await pollAgain(getNode)

    expect(screen.queryByText(LOAD_ERROR)).toBeNull()
  })

  it('вернувшаяся нода снимает плашку и снова даёт проверять туннель', async () => {
    const getNode = await openPage(node({ online: false }))
    getNode.mockImplementation(() => respond(node()))

    await pollAgain(getNode)

    expect(screen.queryByText(BANNER_TITLE)).toBeNull()
    expect(button('Проверить').disabled).toBe(false)
    expect(button('Обновить данные').disabled).toBe(false)
  })
})

describe('GRE-туннели отвечающей ноды', () => {
  it('без плашки, «Проверить» и «Обновить» доступны, минуты считаются от «сейчас»', async () => {
    await openPage(node({ tunnels: [tunnel({ last_rx_at: minutesBefore(Date.now(), 3.5) })] }))

    expect(screen.queryByText(BANNER_TITLE)).toBeNull()
    expect(button('Проверить').disabled).toBe(false)
    expect(button('Обновить данные').disabled).toBe(false)
    expect(screen.getByText('Пакеты идут — последний 3 минуты назад')).toBeTruthy()
  })

  it('первый же запрос не прошёл — показана ошибка загрузки', async () => {
    vi.spyOn(greApi, 'getNode').mockImplementation(refuse)
    renderPage()

    expect(await screen.findByText(LOAD_ERROR, {}, WAIT)).toBeTruthy()
  })
})
