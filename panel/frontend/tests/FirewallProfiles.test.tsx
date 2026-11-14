import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, within } from '@testing-library/react'
import type { ReactNode } from 'react'
import FirewallProfiles from '../src/pages/FirewallProfiles'
import { firewallProfilesApi, type FirewallProfile, type FirewallProfileWithServers } from '../src/api/client'

const tooltipLabels = vi.hoisted(() => [] as string[])

// Строка привязанного сервера рисует подсказку «Отвязать» ровно один раз за отрисовку —
// по числу таких подсказок видно, сколько строк перерисовано
vi.mock('../src/components/ui/Tooltip', () => ({
  Tooltip: ({ label, children }: { label: string; children: ReactNode }) => {
    tooltipLabels.push(label)
    return children
  },
}))
vi.mock('../src/components/FAQ', () => ({ FAQIcon: () => null }))

const LINKED_SERVERS = 300
const POLL_INTERVAL_MS = 3000

const renderedRows = () => tooltipLabels.filter(label => label === 'Отвязать').length

// Каждый вызов — заново разобранный ответ бэкенда: объекты новые, содержимое прежнее
function polledProfile(pendingServerId?: number): FirewallProfileWithServers {
  const summary: FirewallProfile = {
    id: 1, name: 'edge', description: null, rules: [], default_incoming: 'deny', default_outgoing: 'allow',
    position: 0, linked_servers_count: LINKED_SERVERS, synced_servers_count: LINKED_SERVERS,
    node_port_allowed: true, node_api_port: 9100, ssh_port_allowed: true, ssh_ports: [22], ssh_ports_blocked: [],
    ssh_default_port: 22, created_at: null, updated_at: null,
  }
  return {
    ...summary,
    rules_hash: 'abc',
    servers: Array.from({ length: LINKED_SERVERS }, (_, index) => ({
      server_id: index + 1,
      server_name: `srv-${index + 1}`,
      server_url: `https://10.0.0.${index + 1}:9100`,
      ssh_port: 22,
      sync_status: index + 1 === pendingServerId ? 'pending' : 'synced',
      rules_hash: 'abc',
      is_synced: index + 1 !== pendingServerId,
      last_sync_at: '2026-01-01T12:00:00Z',
    })),
  }
}

// Тесту нужны только data: остальные поля ответа axios страница не читает
const respond = <T,>(data: T) => Promise.resolve({ data }) as never

async function openServersTab() {
  const getProfile = vi.spyOn(firewallProfilesApi, 'get').mockImplementation(() => respond(polledProfile()))
  vi.spyOn(firewallProfilesApi, 'list').mockImplementation(() => respond([polledProfile()]))
  vi.spyOn(firewallProfilesApi, 'getAvailableServers').mockImplementation(() => respond([]))
  vi.spyOn(firewallProfilesApi, 'syncState').mockImplementation(() => respond({ job: null }))

  render(<FirewallProfiles />)
  fireEvent.click(await screen.findByRole('button', { name: `Серверы (${LINKED_SERVERS})` }))
  await screen.findByText(`srv-${LINKED_SERVERS}`)
  tooltipLabels.length = 0
  return getProfile
}

const nextPoll = () => act(() => vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS + 100))

describe('FirewallProfiles: привязанные серверы открытого профиля', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('опрос без изменений не перерисовывает ни одной строки', async () => {
    const getProfile = await openServersTab()
    const requestsBefore = getProfile.mock.calls.length

    await nextPoll()

    expect(getProfile.mock.calls.length).toBeGreaterThan(requestsBefore)
    expect(renderedRows()).toBe(0)
  })

  it('сменился статус одного сервера — перерисована одна строка', async () => {
    const getProfile = await openServersTab()
    getProfile.mockImplementation(() => respond(polledProfile(42)))

    await nextPoll()

    expect(renderedRows()).toBe(1)
    const row = screen.getByText('srv-42').closest('.rounded-lg') as HTMLElement
    expect(within(row).getByText('Ожидает')).toBeTruthy()
  })

  it('галочка у сервера перерисовывает только его строку', async () => {
    await openServersTab()
    const row = screen.getByText('srv-7').closest('.rounded-lg') as HTMLElement

    fireEvent.click(within(row).getByRole('checkbox'))

    expect(renderedRows()).toBe(1)
    expect(screen.getByText('Синхронизировать выбранные (1)')).toBeTruthy()
  })

  it('в скрытой вкладке браузера профили не опрашиваются', async () => {
    const getProfile = await openServersTab()
    const requestsBefore = getProfile.mock.calls.length
    const listRequestsBefore = vi.mocked(firewallProfilesApi.list).mock.calls.length
    vi.spyOn(document, 'hidden', 'get').mockReturnValue(true)

    await nextPoll()

    expect(getProfile.mock.calls.length).toBe(requestsBefore)
    expect(vi.mocked(firewallProfilesApi.list).mock.calls.length).toBe(listRequestsBefore)
  })
})
