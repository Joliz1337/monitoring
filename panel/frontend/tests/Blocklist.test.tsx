import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { toast } from 'sonner'
import Blocklist from '../src/pages/Blocklist'
import { blocklistApi, serversApi, type BlocklistRule, type Server } from '../src/api/client'

vi.mock('../src/components/blocklist/PingBlockCard', () => ({ default: () => null }))
vi.mock('../src/components/blocklist/AllowSourcesCard', () => ({ default: () => null }))

const FLEET_SIZE = 40
const SERVER_WITH_RULES = 7

const fleet = Array.from({ length: FLEET_SIZE }, (_, index) => ({ id: index + 1, name: `srv-${index + 1}` })) as Server[]

const rule = (id: number, ip_cidr: string, direction: 'in' | 'out'): BlocklistRule => ({
  id, ip_cidr, direction, server_id: SERVER_WITH_RULES, is_permanent: true, comment: null, source: 'manual', created_at: '',
})

const ownRules = {
  in: [rule(1, '203.0.113.1', 'in'), rule(2, '203.0.113.2', 'in'), rule(3, '203.0.113.3', 'in')],
  out: [rule(4, '198.51.100.9', 'out')],
}

// Тесту нужны только data: остальные поля ответа axios страница не читает
const respond = <T,>(data: T) => Promise.resolve({ data }) as never

function mockApi() {
  vi.spyOn(serversApi, 'list').mockImplementation(() => respond({ servers: fleet }))
  vi.spyOn(blocklistApi, 'getGlobal').mockImplementation(() => respond({ rules: [] }))
  vi.spyOn(blocklistApi, 'getSources').mockImplementation(() => respond({ sources: [] }))
  vi.spyOn(blocklistApi, 'getServersSummary').mockImplementation(() => respond({
    global: { in: 40, out: 2 },
    servers: { [SERVER_WITH_RULES]: { in: 3, out: 1 } },
  }))
  return vi.spyOn(blocklistApi, 'getServer').mockImplementation((_serverId, direction = 'in') => respond({
    rules: ownRules[direction],
  }))
}

async function openServersTab() {
  render(<Blocklist />)
  fireEvent.click(await screen.findByRole('button', { name: 'По серверам' }))
  await screen.findByText(`srv-${FLEET_SIZE}`)
}

const cardOf = (serverName: string) => screen.getByText(serverName).closest('.card') as HTMLElement

describe('Blocklist: вкладка «По серверам»', () => {
  beforeEach(() => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
  })

  it('счётчики всех карточек приходят одним запросом, списки правил не запрашиваются', async () => {
    const getServer = mockApi()

    await openServersTab()

    await waitFor(() => expect(cardOf(`srv-${SERVER_WITH_RULES}`).textContent).toContain('3 IN1 OUT|42 G'))
    expect(cardOf('srv-1').textContent).toContain('0 IN0 OUT|42 G')
    expect(blocklistApi.getServersSummary).toHaveBeenCalledTimes(1)
    expect(getServer).not.toHaveBeenCalled()
  })

  it('тело карточки и её правила появляются только после раскрытия', async () => {
    const getServer = mockApi()
    await openServersTab()
    expect(screen.queryByPlaceholderText('192.168.1.0/24')).toBeNull()

    fireEvent.click(screen.getByText(`srv-${SERVER_WITH_RULES}`))

    expect(await screen.findByText('203.0.113.2')).toBeTruthy()
    expect(screen.getByText('198.51.100.9')).toBeTruthy()
    expect(screen.getAllByPlaceholderText('192.168.1.0/24')).toHaveLength(1)
    expect(getServer.mock.calls).toEqual([[SERVER_WITH_RULES, 'in'], [SERVER_WITH_RULES, 'out']])
  })

  it('несостоявшаяся загрузка закрывает карточку, а не показывает «Нет правил»', async () => {
    mockApi().mockImplementation(() => Promise.reject(new Error('timeout')))
    const toastError = vi.spyOn(toast, 'error')
    await openServersTab()

    fireEvent.click(screen.getByText(`srv-${SERVER_WITH_RULES}`))

    await waitFor(() => expect(toastError).toHaveBeenCalledWith('Действие не удалось'))
    await waitFor(() => expect(screen.queryByText('Загрузка...')).toBeNull())
    expect(screen.queryByText('Нет правил')).toBeNull()
    expect(cardOf(`srv-${SERVER_WITH_RULES}`).textContent).toContain('3 IN1 OUT')
  })
})
