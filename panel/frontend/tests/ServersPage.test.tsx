import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { serversApi, systemApi } from '../src/api/client'
import { useServersStore, type ServerWithMetrics } from '../src/stores/serversStore'
import Servers from '../src/pages/Servers'
import { FakeIntersectionObserver, makeServer } from './helpers'

const renderedCards = vi.hoisted(() => [] as string[])

vi.mock('../src/utils/nodeCapabilities', async importOriginal => {
  const actual = await importOriginal<typeof import('../src/utils/nodeCapabilities')>()
  return {
    ...actual,
    // Карточка зовёт её ровно один раз за отрисовку — по вызовам видно, какие карточки перерисованы
    nodeIsRestricted: (server: ServerWithMetrics) => {
      renderedCards.push(server.name)
      return actual.nodeIsRestricted(server)
    },
  }
})
vi.mock('../src/components/Infra/InfraTree', () => ({ default: () => null }))
vi.mock('../src/components/MigrationBanner', () => ({ default: () => null }))
vi.mock('../src/components/FAQ', () => ({ FAQIcon: () => null }))

vi.stubGlobal('IntersectionObserver', FakeIntersectionObserver)

const LARGE_FLEET = 400
const NEAR_VIEWPORT = 12
const SEARCH_PLACEHOLDER = 'Поиск по имени или IP...'

type ListResponse = Awaited<ReturnType<typeof serversApi.list>>

function mockFleet(size: number) {
  const servers = Array.from({ length: size }, (_, index) =>
    makeServer({ id: index + 1, name: `srv-${index + 1}`, url: `https://10.0.${Math.floor(index / 250)}.${index % 250}:9100`, position: index }))
  vi.spyOn(serversApi, 'list').mockResolvedValue({ data: { count: size, servers } } as unknown as ListResponse)
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/panel/servers']}>
      <Routes>
        <Route path="/:uid/servers" element={<Servers />} />
      </Routes>
    </MemoryRouter>,
  )
}

/** Страница с большим парком, у которой рядом с экраном первые NEAR_VIEWPORT серверов */
async function renderLargeFleet() {
  mockFleet(LARGE_FLEET)
  renderPage()
  await waitFor(() => expect(FakeIntersectionObserver.observedSlots()).toHaveLength(LARGE_FLEET))
  const slots = FakeIntersectionObserver.observedSlots()
  act(() => FakeIntersectionObserver.moveSlots(slots.slice(0, NEAR_VIEWPORT), true))
  renderedCards.length = 0
}

describe('Страница «Серверы»', () => {
  beforeEach(() => {
    renderedCards.length = 0
    useServersStore.setState({ servers: [], isLoading: false, error: null, metricsUpdatedAt: null })
    vi.spyOn(systemApi, 'getPanelIp').mockRejectedValue(new Error('unavailable'))
  })

  it('в большом парке монтирует карточки только рядом с экраном', async () => {
    mockFleet(LARGE_FLEET)
    renderPage()
    await waitFor(() => expect(FakeIntersectionObserver.observedSlots()).toHaveLength(LARGE_FLEET))
    expect(renderedCards).toEqual([])

    const slots = FakeIntersectionObserver.observedSlots()
    act(() => FakeIntersectionObserver.moveSlots(slots.slice(0, NEAR_VIEWPORT), true))

    expect(screen.getByText('srv-1')).toBeTruthy()
    expect(screen.getByText(`srv-${NEAR_VIEWPORT}`)).toBeTruthy()
    expect(screen.queryByText(`srv-${NEAR_VIEWPORT + 1}`)).toBeNull()
    expect(renderedCards).toHaveLength(NEAR_VIEWPORT)
    expect(document.body.querySelectorAll('*').length).toBeLessThan(2000)
  })

  it('ввод в поиске не перерисовывает смонтированные карточки', async () => {
    // dnd-kit в dev-сборке предупреждает о смене числа сенсоров, когда поиск отключает перетаскивание
    vi.spyOn(console, 'error').mockImplementation(() => {})
    await renderLargeFleet()
    const search = screen.getByPlaceholderText(SEARCH_PLACEHOLDER)

    // Первый символ отключает перетаскивание: у ручек меняются обработчики, это одна перерисовка
    fireEvent.change(search, { target: { value: 's' } })
    renderedCards.length = 0

    fireEvent.change(search, { target: { value: 'sr' } })
    fireEvent.change(search, { target: { value: 'srv-1' } })

    expect(renderedCards).toEqual([])
    expect(screen.getByText('srv-1')).toBeTruthy()
    expect(screen.getByText('srv-12')).toBeTruthy()
    expect(screen.queryByText('srv-2')).toBeNull()
  })

  it('выделение сервера перерисовывает только его карточку', async () => {
    await renderLargeFleet()
    const card = screen.getByText('srv-3').closest('.card') as HTMLElement

    fireEvent.click(card)

    // Сама карточка может отрисоваться дважды: useTranslation при монтировании ставит
    // в очередь обновление, и React доигрывает его после первого же срочного рендера
    expect(new Set(renderedCards)).toEqual(new Set(['srv-3']))
    expect(card.className).toContain('ring-accent-500/30')
  })

  it('небольшой парк рисует все карточки сразу, без наблюдателя', async () => {
    mockFleet(5)
    renderPage()

    expect(await screen.findByText('srv-5')).toBeTruthy()
    expect(FakeIntersectionObserver.observedSlots()).toHaveLength(0)
  })
})
