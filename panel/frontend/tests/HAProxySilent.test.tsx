import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import type { ReactElement, ReactNode } from 'react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import HAProxy from '../src/pages/HAProxy'
import {
  haproxyProfilesApi,
  lossApi,
  portUsageApi,
  proxyApi,
  serversApi,
  type Certificate,
  type FirewallRule,
  type HAProxyLiveStats,
  type HAProxyRule,
  type HAProxyStatus,
} from '../src/api/client'
import { formatMoment } from '../src/components/servers/SavedAnswerNote'
import { createServerCacheKey } from '../src/hooks/useCachedData'
import { useServersStore } from '../src/stores/serversStore'
import { useSettingsStore } from '../src/stores/settingsStore'
import { makeServer } from './helpers'

// У кнопок с одной иконкой подпись есть только в подсказке — по ней тест их и находит
vi.mock('../src/components/ui/Tooltip', () => ({
  Tooltip: ({ label, children }: { label: ReactNode; children: ReactElement }) => (
    <span title={typeof label === 'string' ? label : undefined}>{children}</span>
  ),
}))
vi.mock('../src/components/FAQ', () => ({ FAQIcon: () => null }))

const SERVER_ID = 5
const REFRESH_INTERVAL_S = 5
// С таким интервалом автообновление за время теста не срабатывает: видно, что сделала сама кнопка
const NO_AUTO_REFRESH_S = 3600
// Через столько после первой загрузки HAProxy.tsx идёт в ноду за живыми данными
const LIVE_REFRESH_DELAY_MS = 300
const WAIT = { timeout: 5000 }

const COPY_SAVED_AT = '2026-11-15T18:02:46.000Z'
const SILENT_SINCE = '2026-11-15T18:09:10.000Z'
const BROWSER_COPY_SAVED_AT = '2026-11-14T09:30:00.000Z'

const SAVED_READONLY = /Показано то, что панель сохранила/
const SNAPSHOT = /снимок на/
const SAVED_NOTE = /Сервер не отвечает — показано то, что панель сохранила/
const NO_COPY_NOTE = /копии этих данных у панели нет/
const NOTHING_SAVED = /Сохранённых данных этой страницы у панели нет/
const NOT_IN_COPY = 'В сохранённой копии этих данных нет.'

// Тесту нужны только data и заголовки отказа: остальные поля ответа axios страница не читает
const respond = <T,>(data: T) => Promise.resolve({ data }) as never
// Так панель отвечает, когда нода молчит, а её ответа в базе нет
const noCopy = () =>
  Promise.reject({ response: { status: 409, headers: { 'x-node-answer-copy-missing': '1' }, data: {} } }) as never

type PanelCopy = Awaited<ReturnType<typeof proxyApi.getHAProxyCached>>['data']

const haproxyStatus: HAProxyStatus = { running: true, config_valid: true, config_message: '' }
const proxyRule: HAProxyRule = {
  name: 'vless', rule_type: 'tcp', listen_port: 443, target_ip: '10.0.0.9', target_port: 443,
}
const certificate: Certificate = {
  domain: 'example.com', expiry_date: '2027-03-01T00:00:00Z', days_left: 105, expired: false,
}
const firewallRule: FirewallRule = {
  number: 1, port: 443, protocol: 'tcp', action: 'ALLOW', from_ip: 'Anywhere', direction: 'in', ipv6: false,
}
const liveStats: HAProxyLiveStats = {
  available: true, haproxy_version: '3.0.5', uptime_sec: 3600, curr_conns: 12,
  proxies: [{ name: 'vless', mode: 'tcp', frontend: null, backend: null, servers: [] }],
}

function panelCopy(patch: Partial<PanelCopy> = {}): PanelCopy {
  return {
    status: haproxyStatus,
    stats: liveStats,
    rules: { count: 1, rules: [proxyRule] },
    certs: { count: 1, certificates: [certificate] },
    firewall: { count: 1, active: true, rules: [firewallRule] },
    cached_at: COPY_SAVED_AT,
    ...patch,
  }
}

const silentCopy = (patch: Partial<PanelCopy> = {}) => panelCopy({ node_silent_since: SILENT_SINCE, ...patch })

function mockApi(cachedAnswer: () => never) {
  vi.spyOn(serversApi, 'list').mockImplementation(
    () => respond({ count: 1, servers: [makeServer({ id: SERVER_ID, name: 'relay-5' })] }),
  )
  vi.spyOn(haproxyProfilesApi, 'getAvailableServers').mockImplementation(() => respond([]))
  vi.spyOn(portUsageApi, 'get').mockImplementation(() => respond({}))

  return {
    cached: vi.spyOn(proxyApi, 'getHAProxyCached').mockImplementation(cachedAnswer),
    // Всё, за чем панель идёт в саму ноду
    live: {
      status: vi.spyOn(proxyApi, 'getHAProxyStatus').mockImplementation(() => respond(haproxyStatus)),
      rules: vi.spyOn(proxyApi, 'getHAProxyRules').mockImplementation(() => respond({ count: 1, rules: [proxyRule] })),
      certs: vi.spyOn(proxyApi, 'getHAProxyCerts').mockImplementation(
        () => respond({ certificates: [certificate.domain] }),
      ),
      firewall: vi.spyOn(proxyApi, 'getFirewallRules').mockImplementation(
        () => respond({ count: 1, active: true, rules: [firewallRule] }),
      ),
      allCerts: vi.spyOn(proxyApi, 'getAllCerts').mockImplementation(
        () => respond({ count: 1, certificates: [certificate] }),
      ),
      stats: vi.spyOn(proxyApi, 'getHAProxyStats').mockImplementation(() => respond(liveStats)),
      ways: vi.spyOn(lossApi, 'allWays').mockImplementation(() => respond({ status: 'ok', targets: [] })),
    },
  }
}

type MockedNode = ReturnType<typeof mockApi>

function renderPage() {
  return render(
    <MemoryRouter initialEntries={[`/panel/server/${SERVER_ID}/haproxy`]}>
      <Routes>
        <Route path="/:uid/server/:serverId/haproxy" element={<HAProxy />} />
      </Routes>
    </MemoryRouter>,
  )
}

const button = (name: string) => screen.getByRole('button', { name }) as HTMLButtonElement
const iconButton = (hint: string) => within(screen.getByTitle(hint)).getByRole('button') as HTMLButtonElement

// Всё, что без ноды не сработает: HAProxy, сертификаты, файрвол
const writeControls = () => [
  button('Перезагрузить'), button('Остановить'),
  button('Создать'), button('Загрузить'), button('Обновить все'), iconButton('Продлить'), iconButton('Удалить'),
  button('Выключить'), button('Открыть порт'), iconButton('Закрыть порт'),
]

// Мигающая точка в карточке статуса — знак, что состояние живое
const livePulse = () => document.querySelector('.rounded-full.bg-success')

const expectNoNodeRequests = (node: MockedNode) => {
  for (const endpoint of Object.values(node.live)) expect(endpoint).not.toHaveBeenCalled()
}

const expectLiveRefresh = (node: MockedNode) =>
  waitFor(() => {
    for (const endpoint of Object.values(node.live)) expect(endpoint).toHaveBeenCalled()
  }, WAIT)

// Отложенный поход в ноду после первой загрузки и столько-то кругов автообновления
const passRefreshRounds = (rounds: number) =>
  act(() => vi.advanceTimersByTimeAsync(LIVE_REFRESH_DELAY_MS + rounds * REFRESH_INTERVAL_S * 1000 + 100))

describe('Настройки сервера, когда нода не отвечает', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    localStorage.clear()
    useServersStore.setState({ servers: [], isLoading: false, error: null, metricsUpdatedAt: null })
    useSettingsStore.setState({ refreshInterval: REFRESH_INTERVAL_S })
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('показывает сохранённую копию с плашкой, в ноду не ходит и менять ничего не даёт', async () => {
    const node = mockApi(() => respond(silentCopy()))
    renderPage()

    const banner = await screen.findByText(SAVED_READONLY, {}, WAIT)
    expect(banner.textContent).toContain(formatMoment(COPY_SAVED_AT, 'ru'))
    expect(screen.getByText(SNAPSHOT).textContent).toContain(formatMoment(COPY_SAVED_AT, 'ru'))
    await passRefreshRounds(2)

    expectNoNodeRequests(node)
    for (const control of writeControls()) expect(control.disabled).toBe(true)
    // У кнопок страницы нет своего вида для disabled: без приглушения отключённые выглядели бы рабочими
    expect(writeControls()[0].closest('[class*="button:disabled"]')).not.toBeNull()
    expect(livePulse()).toBeNull()
    // Копию страница перечитывает и дальше — так она узнает, что нода вернулась
    expect(node.cached.mock.calls.length).toBeGreaterThan(1)
  })

  it('отвечающая нода — без плашки, страница идёт за живыми данными, кнопки работают', async () => {
    const node = mockApi(() => respond(panelCopy()))
    renderPage()

    await screen.findByRole('button', { name: 'Остановить' }, WAIT)
    await expectLiveRefresh(node)

    expect(screen.queryByText(SAVED_READONLY)).toBeNull()
    expect(screen.queryByText(SNAPSHOT)).toBeNull()
    for (const control of writeControls()) expect(control.disabled).toBe(false)
    expect(writeControls()[0].closest('[class*="button:disabled"]')).toBeNull()
    expect(livePulse()).not.toBeNull()
  })

  it('нода вернулась — плашка уходит сама, страница один раз идёт за живыми данными', async () => {
    const node = mockApi(() => respond(silentCopy()))
    renderPage()
    await screen.findByText(SAVED_READONLY, {}, WAIT)

    node.cached.mockImplementation(() => respond(panelCopy()))
    await passRefreshRounds(1)

    await waitFor(() => expect(screen.queryByText(SAVED_READONLY)).toBeNull(), WAIT)
    await expectLiveRefresh(node)
    for (const control of writeControls()) expect(control.disabled).toBe(false)

    await passRefreshRounds(2)
    // Статус отдельно никто не опрашивает: второй запрос значил бы второе полное обновление
    expect(node.live.status).toHaveBeenCalledTimes(1)
  })

  it('кнопка «Обновить» перечитывает копию, а не идёт в ноду', async () => {
    useSettingsStore.setState({ refreshInterval: NO_AUTO_REFRESH_S })
    const node = mockApi(() => respond(silentCopy()))
    renderPage()
    await screen.findByText(SAVED_READONLY, {}, WAIT)

    fireEvent.click(iconButton('Обновить данные'))

    expect(node.cached).toHaveBeenCalledTimes(2)
    await waitFor(() => expect(iconButton('Обновить данные').disabled).toBe(false), WAIT)
    expectNoNodeRequests(node)
    expect(screen.getByText(SAVED_READONLY)).toBeTruthy()
  })

  it('кнопка «Обновить» у вернувшейся ноды снимает плашку и идёт за живыми данными', async () => {
    useSettingsStore.setState({ refreshInterval: NO_AUTO_REFRESH_S })
    const node = mockApi(() => respond(silentCopy()))
    renderPage()
    await screen.findByText(SAVED_READONLY, {}, WAIT)

    node.cached.mockImplementation(() => respond(panelCopy()))
    fireEvent.click(iconButton('Обновить данные'))

    await expectLiveRefresh(node)
    expect(screen.queryByText(SAVED_READONLY)).toBeNull()
  })

  it('копия без статистики — пометка вместо вечной загрузки', async () => {
    mockApi(() => respond(silentCopy({ stats: undefined })))
    renderPage()
    await screen.findByText(SAVED_READONLY, {}, WAIT)

    expect(screen.getByText(NOT_IN_COPY)).toBeTruthy()
    expect(screen.queryByText(SNAPSHOT)).toBeNull()
    expect(document.querySelector('.animate-spin')).toBeNull()
  })

  it('без копии говорит об этом, а не об ошибке загрузки', async () => {
    const node = mockApi(noCopy)
    renderPage()

    expect(await screen.findByText(NOTHING_SAVED, {}, WAIT)).toBeTruthy()
    expect(screen.getByText('Сервер не отвечает')).toBeTruthy()
    expect(screen.queryByText('Не удалось загрузить данные HAProxy')).toBeNull()
    await passRefreshRounds(1)
    expectNoNodeRequests(node)
  })

  it('копии у панели нет, но страницу открывали в этом браузере — показано то, что он запомнил', async () => {
    localStorage.setItem(`cache_${createServerCacheKey(SERVER_ID, 'haproxy')}`, JSON.stringify({
      cachedAt: BROWSER_COPY_SAVED_AT,
      data: {
        status: haproxyStatus,
        rules: [proxyRule],
        certs: [certificate.domain],
        certDetails: { [certificate.domain]: certificate },
        firewallRules: [firewallRule],
        firewallActive: true,
        stats: liveStats,
      },
    }))
    const node = mockApi(noCopy)
    renderPage()

    const banner = await screen.findByText(SAVED_READONLY, {}, WAIT)
    expect(banner.textContent).toContain(formatMoment(BROWSER_COPY_SAVED_AT, 'ru'))
    await passRefreshRounds(1)

    expectNoNodeRequests(node)
    for (const control of writeControls()) expect(control.disabled).toBe(true)
    expect(screen.queryByText(NO_COPY_NOTE)).toBeNull()
  })

  it('конфиг открывается из копии с пометкой', async () => {
    mockApi(() => respond(silentCopy()))
    vi.spyOn(proxyApi, 'getHAProxyConfig').mockImplementation(() => respond({
      content: 'global\n    maxconn 50000', path: '/etc/haproxy/haproxy.cfg', saved_at: COPY_SAVED_AT,
    }))
    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: 'Конфиг' }, WAIT))

    expect(await screen.findByText(SAVED_NOTE, {}, WAIT)).toBeTruthy()
    expect(screen.getByText(/maxconn 50000/)).toBeTruthy()
  })

  it('конфиг без копии — пометка, а не строка ошибки', async () => {
    mockApi(() => respond(silentCopy()))
    vi.spyOn(proxyApi, 'getHAProxyConfig').mockImplementation(noCopy)
    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: 'Конфиг' }, WAIT))

    expect(await screen.findByText(NO_COPY_NOTE, {}, WAIT)).toBeTruthy()
    expect(screen.queryByText('Failed to load config')).toBeNull()
  })
})
