import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { toast } from 'sonner'
import AntiDdos from '../src/pages/AntiDdos'
import {
  antiDdosApi, serversApi,
  type AntiDdosFleetAction, type AntiDdosRollout, type AntiDdosStatus,
} from '../src/api/client'

const FLEET_SIZE = 40
const STARTED_AT = '2026-11-14T10:00:00+00:00'
const FINISHED_AT = '2026-11-14T10:00:40+00:00'

const failedNodes = [
  { server_id: 7, server_name: 'nl-7', message: 'timeout' },
  { server_id: 19, server_name: 'de-19', message: 'watchdog not installed' },
]

const action = (overrides: Partial<AntiDdosFleetAction> = {}): AntiDdosFleetAction => ({
  action: 'emergency', enabled: true, started_at: STARTED_AT, finished_at: null,
  total: FLEET_SIZE, ok: 0, failed_count: 0, failed: [], ...overrides,
})

const partlyApplied = action({ finished_at: FINISHED_AT, ok: FLEET_SIZE - 2, failed_count: 2, failed: failedNodes })
const applied = action({ finished_at: FINISHED_AT, ok: FLEET_SIZE })

const status = (last_action: AntiDdosFleetAction | null, rollout: AntiDdosRollout | null = null): AntiDdosStatus => ({
  nodes: [], active_count: 0, total: FLEET_SIZE, last_action, rollout,
})

// Тесту нужны только data: остальные поля ответа axios страница не читает
const respond = <T,>(data: T) => Promise.resolve({ data }) as never

/** Страница видит статусы по очереди; последний остаётся на все следующие опросы */
function mockApi(...statuses: AntiDdosStatus[]) {
  const queue = [...statuses]
  vi.spyOn(serversApi, 'list').mockImplementation(() => respond({ servers: [] }))
  vi.spyOn(antiDdosApi, 'getSettings').mockImplementation(() => respond({
    enabled: true, whitelist_push_interval: 3600, status_poll_interval: 60, watchdog_default_enabled: true,
    last_push_at: null, last_push_status: null, last_push_count: 0,
  }))
  vi.spyOn(antiDdosApi, 'getStatus').mockImplementation(() => respond(queue.length > 1 ? queue.shift() : queue[0]))
}

async function openPage() {
  render(<AntiDdos />)
  await screen.findByRole('button', { name: 'Включить на всех' })
}

describe('AntiDdos: итог команды на весь парк', () => {
  beforeEach(() => {
    vi.spyOn(toast, 'success')
  })

  it('команда, не дошедшая до части нод, показана предупреждением с нодами и причинами', async () => {
    mockApi(status(partlyApplied))

    await openPage()

    expect(screen.getByText(`Включение аварийного режима: не применено на 2 из ${FLEET_SIZE} нод`)).toBeTruthy()
    expect(screen.getByText('nl-7').parentElement?.textContent).toBe('nl-7timeout')
    expect(screen.getByText('de-19').parentElement?.textContent).toBe('de-19watchdog not installed')
    expect(screen.getByRole('button', { name: 'Повторить' })).toBeTruthy()
  })

  it('список не прошедших нод, не поместившийся в ответ, досчитан строкой', async () => {
    mockApi(status(action({ finished_at: FINISHED_AT, ok: 0, failed_count: 250, total: 250, failed: failedNodes })))

    await openPage()

    expect(screen.getByText('…и ещё 248')).toBeTruthy()
  })

  it('пока команда обходит парк, виден её ход, а тоста об успехе нет', async () => {
    const running = action({ ok: 10, failed_count: 1 })
    mockApi(status(null), status(running))
    vi.spyOn(antiDdosApi, 'emergencyAll').mockImplementation(() => respond({ success: true, started: true, action: action() }))
    await openPage()

    fireEvent.click(screen.getByRole('button', { name: 'Включить на всех' }))

    expect(await screen.findByText(`Включение аварийного режима: обработано 11 из ${FLEET_SIZE} нод`)).toBeTruthy()
    expect(antiDdosApi.emergencyAll).toHaveBeenCalledWith(true)
    expect(toast.success).not.toHaveBeenCalled()
    expect(screen.queryByRole('button', { name: 'Повторить' })).toBeNull()
  })

  it('команда, прошедшая на всех нодах, заканчивается обычным тостом', async () => {
    mockApi(status(null), status(applied))
    vi.spyOn(antiDdosApi, 'emergencyAll').mockImplementation(() => respond({ success: true, started: true, action: action() }))
    await openPage()

    fireEvent.click(screen.getByRole('button', { name: 'Включить на всех' }))

    await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Готово'))
    expect(screen.queryByText(/не применено/)).toBeNull()
  })

  it('команда, которую запустили не с этой страницы, тост не вызывает', async () => {
    mockApi(status(applied))

    await openPage()

    expect(toast.success).not.toHaveBeenCalled()
  })

  it('«Повторить» отправляет команду только не прошедшим нодам и убирает предупреждение после успеха', async () => {
    const retryStarted = '2026-11-14T10:05:00+00:00'
    const retried = action({ started_at: retryStarted, finished_at: FINISHED_AT, total: 2, ok: 2 })
    mockApi(status(partlyApplied), status(retried))
    const retryFailed = vi.spyOn(antiDdosApi, 'retryFailed').mockImplementation(() => respond({
      success: true, started: true, action: action({ started_at: retryStarted, total: 2 }),
    }))
    await openPage()

    fireEvent.click(screen.getByRole('button', { name: 'Повторить' }))

    await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Готово'))
    expect(retryFailed).toHaveBeenCalledTimes(1)
    expect(screen.queryByText(/не применено/)).toBeNull()
  })

  it('отказ сервера запустить команду виден сразу', async () => {
    mockApi(status(null))
    vi.spyOn(antiDdosApi, 'emergencyAll').mockImplementation(() => Promise.reject(new Error('500')))
    const toastError = vi.spyOn(toast, 'error')
    await openPage()

    fireEvent.click(screen.getByRole('button', { name: 'Выключить на всех' }))

    await waitFor(() => expect(toastError).toHaveBeenCalledWith('Не удалось выполнить действие'))
  })

  it('остановленное обновление сторожа показано с версией и не принявшими её нодами', async () => {
    mockApi(status(null, {
      version: '2.7.0', confirmed: 1, pending: 0, failed: 3, halted: true,
      failed_nodes: [{ server_id: 7, server_name: 'nl-7', message: 'watchdog script failed the syntax check' }],
    }))

    await openPage()

    expect(screen.getByText(/Обновление сторожа до версии 2\.7\.0 остановлено: он не встал на 3 нодах/)).toBeTruthy()
    expect(screen.getByText('watchdog script failed the syntax check')).toBeTruthy()
  })
})
