import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { toast } from 'sonner'
import Updates from '../src/pages/Updates'
import { haproxyUpgradeApi, nodeImageApi, systemApi } from '../src/api/client'

const settings = vi.hoisted(() => ({ updateBranch: 'main', fetchSettings: () => {} }))
vi.mock('../src/stores/settingsStore', () => ({ useSettingsStore: () => settings }))

const FLEET_SIZE = 40
// NODE_SWEEP_POOL_SIZE в Updates.tsx
const POOL_SIZE = 12
const REFUSING_NODES = new Set([3, 17, 29])
const REFUSAL = 'Update already in progress'
const WAIT = { timeout: 5000 }

const fleet = Array.from({ length: FLEET_SIZE }, (_, index) => ({
  id: index + 1, name: `srv-${index + 1}`, url: `https://10.0.0.${index + 1}:9100`, folder: null, has_ssh_creds: false,
}))

// Тесту нужны только data: остальные поля ответа axios страница не читает
const respond = <T,>(data: T) => Promise.resolve({ data }) as never

function mockApi() {
  const accepted: number[] = []
  const load = { inFlight: 0, peak: 0 }

  vi.spyOn(systemApi, 'getVersionBase').mockImplementation(() => respond({
    panel: { version: '10.0.0', latest_version: '10.0.0', update_available: false },
    node: { latest_version: '2.0.0' },
    optimizations: { latest_version: null },
    nodes: fleet,
    update_in_progress: false,
    pending_node_rollout: 0,
  }))
  vi.spyOn(systemApi, 'getUpdateSummary').mockImplementation(() => respond(null))
  vi.spyOn(systemApi, 'getNodeVersionById').mockImplementation(serverId => respond({
    ...fleet[serverId - 1], version: '1.0.0', status: 'online', haproxy: null, download_proxy: null,
  }))
  vi.spyOn(systemApi, 'nodeUpdates').mockImplementation(() => respond({
    updates: accepted.map(server_id => ({ server_id, step: null, started_at: '2026-01-01T12:00:00Z' })),
  }))
  vi.spyOn(nodeImageApi, 'jobs').mockImplementation(() => respond({ jobs: [] }))
  vi.spyOn(haproxyUpgradeApi, 'jobs').mockImplementation(() => respond({ jobs: [] }))

  const updateNode = vi.spyOn(systemApi, 'updateNode').mockImplementation(serverId => new Promise((resolve, reject) => {
    load.inFlight += 1
    load.peak = Math.max(load.peak, load.inFlight)
    setTimeout(() => {
      load.inFlight -= 1
      if (REFUSING_NODES.has(serverId)) {
        reject({ response: { data: { detail: REFUSAL } } })
        return
      }
      accepted.push(serverId)
      resolve({ data: {} } as never)
    }, 5)
  }))

  return { updateNode, load }
}

const cardOf = (nodeName: string) => screen.getByText(nodeName).closest('.card') as HTMLElement

describe('Updates: запуск обновления нод со страницы', () => {
  beforeEach(() => {
    vi.spyOn(toast, 'success')
    vi.spyOn(toast, 'warning')
    vi.spyOn(toast, 'error')
  })

  it('«Обновить все ноды» шлёт команды пачкой и сообщает итог одним тостом', async () => {
    const { updateNode, load } = mockApi()
    render(<Updates />)

    fireEvent.click(await screen.findByRole('button', { name: `Обновить все ноды (${FLEET_SIZE})` }, WAIT))

    await waitFor(() => expect(toast.warning).toHaveBeenCalledTimes(1), WAIT)
    expect(toast.warning).toHaveBeenCalledWith(
      `Команду обновиться приняли ноды: ${FLEET_SIZE - REFUSING_NODES.size}, не приняли: ${REFUSING_NODES.size}`,
    )
    expect(toast.error).not.toHaveBeenCalled()
    expect(updateNode).toHaveBeenCalledTimes(FLEET_SIZE)
    expect(load.peak).toBe(POOL_SIZE)
    // Причина отказа остаётся на карточке ноды, принявшие команду показывают ход обновления
    expect(within(cardOf('srv-17')).getByText(REFUSAL)).toBeTruthy()
    expect(within(cardOf('srv-1')).getByText('Обновляется…')).toBeTruthy()
  })

  it('отказ одной ноды, запущенной со своей карточки, называет её в тосте', async () => {
    const { updateNode } = mockApi()
    render(<Updates />)
    await screen.findByRole('button', { name: `Обновить все ноды (${FLEET_SIZE})` }, WAIT)

    fireEvent.click(within(cardOf('srv-3')).getByRole('button', { name: 'Обновить' }))

    await waitFor(() => expect(toast.error).toHaveBeenCalledWith(`srv-3: ${REFUSAL}`), WAIT)
    expect(updateNode).toHaveBeenCalledTimes(1)
    expect(toast.warning).not.toHaveBeenCalled()
  })
})
