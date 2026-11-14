import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { serversApi } from '../src/api/client'
import { useServersStore } from '../src/stores/serversStore'
import Dashboard from '../src/pages/Dashboard'
import { makeServer } from './helpers'

// Графики парка и папок тянут apexcharts и свои запросы — плашке они не нужны
vi.mock('../src/components/Dashboard/FleetSummary', () => ({ default: () => null }))
vi.mock('../src/components/Dashboard/FolderStats', () => ({
  FolderStatusCounts: () => null,
  FolderLoadBadges: () => null,
  FolderLoadChart: () => null,
}))
vi.mock('../src/components/FAQ', () => ({ FAQIcon: () => null }))

type ListResponse = Awaited<ReturnType<typeof serversApi.list>>

const oneServer = () => ({ data: { count: 1, servers: [makeServer()] } }) as unknown as ListResponse
const pollAgain = () => act(() => useServersStore.getState().fetchServersWithMetrics())

function renderDashboard() {
  return render(
    <MemoryRouter initialEntries={['/panel']}>
      <Routes>
        <Route path="/:uid" element={<Dashboard />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('Дашборд: плашка «нет связи с панелью»', () => {
  beforeEach(() => {
    useServersStore.setState({ servers: [], isLoading: false, error: null, metricsUpdatedAt: null })
  })

  it('видна, пока опрос не проходит, и пропадает после успешного ответа', async () => {
    const list = vi.spyOn(serversApi, 'list')
    list.mockResolvedValueOnce(oneServer())
    renderDashboard()
    expect(await screen.findByText('srv-1')).toBeTruthy()
    expect(screen.queryByRole('alert')).toBeNull()

    list.mockRejectedValueOnce(new Error('Network Error'))
    await pollAgain()

    expect(screen.getByRole('alert').textContent).toMatch(/^Нет связи с панелью, данные от \d{1,2}:\d{2}/)
    expect(screen.getByText('srv-1')).toBeTruthy()

    list.mockResolvedValueOnce(oneServer())
    await pollAgain()

    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('без единого успешного ответа показывается без времени', async () => {
    vi.spyOn(serversApi, 'list').mockRejectedValue(new Error('Network Error'))
    renderDashboard()

    expect((await screen.findByRole('alert')).textContent).toBe('Нет связи с панелью')
  })
})
