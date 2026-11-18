import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import SourcePool from '../src/pages/SourcePool'
import { serversApi, sourcePoolApi, type SourcePoolNodeView } from '../src/api/client'
import { formatMoment } from '../src/components/servers/SavedAnswerNote'
import { useServersStore } from '../src/stores/serversStore'
import { makeServer } from './helpers'

vi.mock('../src/components/FAQ', () => ({ FAQIcon: () => null }))

const SERVER_ID = 5
const WAIT = { timeout: 5000 }
const LAST_STATE_AT = '2026-11-15T18:02:46+00:00'
const FIRST_ADDRESS = '203.0.113.7'
const SECOND_ADDRESS = '203.0.113.8'

const SILENT_TITLE = 'Сервер не отвечает'
const SAVED_QUEUED =
  `Показано состояние, которое панель получила от него ${formatMoment(LAST_STATE_AT, 'ru')}. Изменения сохраняются в панели и доедут до сервера, когда он вернётся.`
const SAVED_QUEUED_ANY_TIME = /Показано состояние, которое панель получила от него/
const NO_COPY_NOTE = /копии этих данных у панели нет/
const STATUS_ACTIVE = 'Работает'

const poolView = (patch: Partial<SourcePoolNodeView> = {}): SourcePoolNodeView => ({
  server_id: SERVER_ID, name: 'nl-5', online: true, node_version: '10.40.0',
  min_node_version: '10.30.0', min_node_version_manual: '10.35.0', supported_by_node: true, supports_manual: true,
  enabled: true, mode: 'auto', assignments: {},
  bindings: [{ mark: 101, address: FIRST_ADDRESS }, { mark: 102, address: SECOND_ADDRESS }],
  unavailable_marks: [], install_status: 'active', sync_error: null, interface: 'eth0',
  addresses: [
    { address: FIRST_ADDRESS, excluded: false, marks: 1 },
    { address: SECOND_ADDRESS, excluded: false, marks: 1 },
  ],
  excluded: [], active_count: 2, mark_count: 2, mark_base: 101, in_sync: true,
  missing_marks: [], conflict: null, node_error: null, last_state_at: LAST_STATE_AT,
  ...patch,
})

const silentView = (patch: Partial<SourcePoolNodeView> = {}) => poolView({ online: false, ...patch })

// Тесту нужны только data: остальные поля ответа axios страница не читает
const respond = <T,>(data: T) => Promise.resolve({ data }) as never

/** Панель отдаёт страницу из своей базы и тем же видом отвечает на сохранение */
function mockPanel(view: SourcePoolNodeView) {
  vi.spyOn(sourcePoolApi, 'getNode').mockImplementation(() => respond(view))
  return {
    updateNode: vi.spyOn(sourcePoolApi, 'updateNode').mockImplementation(() => respond(view)),
    refreshNode: vi.spyOn(sourcePoolApi, 'refreshNode').mockImplementation(() => respond(view)),
  }
}

function openPage() {
  render(
    <MemoryRouter initialEntries={[`/panel/server/${SERVER_ID}/source-pool`]}>
      <Routes>
        <Route path="/:uid/server/:serverId/source-pool" element={<SourcePool />} />
      </Routes>
    </MemoryRouter>,
  )
}

// У кнопки «Обновить» нет подписи — только значок
const refreshButton = () => document.querySelector('svg.lucide-refresh-cw')?.closest('button') as HTMLButtonElement
const button = (name: string) => screen.getByRole('button', { name }) as HTMLButtonElement
const poolToggle = () => screen.getByRole('button', { pressed: true }) as HTMLButtonElement
const addressCheckboxes = () => screen.getAllByRole('checkbox') as HTMLInputElement[]

describe('Страница «Исходящие адреса», когда нода не отвечает', () => {
  beforeEach(() => {
    useServersStore.setState({ servers: [], isLoading: false, error: null, metricsUpdatedAt: null })
    vi.spyOn(serversApi, 'list').mockImplementation(
      () => respond({ count: 1, servers: [makeServer({ id: SERVER_ID, name: 'nl-5' })] }),
    )
  })

  it('помечает состояние временем его получения и не даёт опрашивать ноду кнопкой', async () => {
    const { refreshNode } = mockPanel(silentView())
    openPage()

    expect(await screen.findByText(SAVED_QUEUED, {}, WAIT)).toBeTruthy()
    expect(screen.getByText(SILENT_TITLE)).toBeTruthy()
    expect(screen.getByText(STATUS_ACTIVE)).toBeTruthy()
    expect(refreshButton().disabled).toBe(true)

    fireEvent.click(refreshButton())
    expect(refreshNode).not.toHaveBeenCalled()
  })

  it('тумблер, галочки адресов и режим остаются доступны, правка уходит в панель', async () => {
    const { updateNode } = mockPanel(silentView())
    openPage()
    await screen.findByText(SAVED_QUEUED, {}, WAIT)

    expect(poolToggle().disabled).toBe(false)
    expect(addressCheckboxes().map(checkbox => checkbox.disabled)).toEqual([false, false])
    expect(button('По кругу').disabled).toBe(false)
    expect(button('Вручную').disabled).toBe(false)

    fireEvent.click(addressCheckboxes()[1])

    await waitFor(() => expect(updateNode).toHaveBeenCalledWith(SERVER_ID, { excluded: [SECOND_ADDRESS] }), WAIT)
  })

  it('ручную раскладку можно изменить и сохранить', async () => {
    const { updateNode } = mockPanel(silentView({ mode: 'manual', assignments: { 101: FIRST_ADDRESS } }))
    openPage()
    await screen.findByText(SAVED_QUEUED, {}, WAIT)

    fireEvent.change(screen.getAllByRole('combobox')[1], { target: { value: SECOND_ADDRESS } })
    expect(button('Сохранить раскладку').disabled).toBe(false)
    fireEvent.click(button('Сохранить раскладку'))

    await waitFor(
      () => expect(updateNode).toHaveBeenCalledWith(SERVER_ID, { assignments: { 101: FIRST_ADDRESS, 102: SECOND_ADDRESS } }),
      WAIT,
    )
  })

  it('пока панель не получала состояние ноды, в плашке только заголовок', async () => {
    mockPanel(silentView({
      last_state_at: null, install_status: 'pending', interface: null, addresses: [], bindings: [],
      active_count: 0, mark_count: null, mark_base: null, in_sync: false,
    }))
    openPage()

    expect(await screen.findByText(SILENT_TITLE, {}, WAIT)).toBeTruthy()
    expect(screen.queryByText(SAVED_QUEUED_ANY_TIME)).toBeNull()
    expect(screen.queryByText(NO_COPY_NOTE)).toBeNull()
    expect(refreshButton().disabled).toBe(true)
    expect(poolToggle().disabled).toBe(false)
  })

  it('живая нода — без плашки, кнопка «Обновить» опрашивает ноду', async () => {
    const { refreshNode } = mockPanel(poolView())
    openPage()
    await screen.findByText(STATUS_ACTIVE, {}, WAIT)

    expect(screen.queryByText(SILENT_TITLE)).toBeNull()
    expect(refreshButton().disabled).toBe(false)

    fireEvent.click(refreshButton())

    await waitFor(() => expect(refreshNode).toHaveBeenCalledWith(SERVER_ID), WAIT)
  })
})
