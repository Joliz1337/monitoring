import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import DeliverImageModal from '../src/components/servers/DeliverImageModal'
import { nodeImageApi, type ImageDeliverySettings } from '../src/api/client'

const WAIT = { timeout: 5000 }

const settings = (patch: Partial<ImageDeliverySettings>): ImageDeliverySettings => ({
  image_delivery: 'auto', ssh_host: '10.0.0.5', ssh_port: 22, ssh_user: 'root',
  ssh_access_level: null, ssh_access_folder: null, has_ssh_password: false, has_ssh_private_key: false,
  ...patch,
})

// Тесту нужны только data: остальные поля ответа axios окно не читает
const respond = <T,>(data: T) => Promise.resolve({ data }) as never

function open(current: ImageDeliverySettings) {
  const api = {
    getSettings: vi.spyOn(nodeImageApi, 'getSettings').mockImplementation(() => respond(current)),
    setSettings: vi.spyOn(nodeImageApi, 'setSettings').mockImplementation(() => respond({ success: true })),
    // Задача не стартует: тест смотрит только на отправленные креды
    deliver: vi.spyOn(nodeImageApi, 'deliver').mockImplementation(() => new Promise(() => {}) as never),
  }
  render(<DeliverImageModal serverId={5} serverName="srv-5" jobId={null} onStarted={() => {}} onClose={() => {}} />)
  return api
}

describe('Окно доставки образа: откуда берётся SSH-доступ', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('называет общий доступ и не предлагает его забыть', async () => {
    open(settings({ ssh_access_level: 'global' }))

    expect(await screen.findByText('Используется общий SSH-доступ (root@10.0.0.5:22)', {}, WAIT)).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Задать свой для этого сервера' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Забыть креды' })).toBeNull()
    expect(screen.queryByPlaceholderText('SSH-пароль')).toBeNull()
  })

  it('называет папку унаследованного доступа и её порт', async () => {
    open(settings({ ssh_access_level: 'folder', ssh_access_folder: 'RU', ssh_port: 2222 }))

    expect(await screen.findByText('Используется SSH-доступ папки «RU» (root@10.0.0.5:2222)', {}, WAIT)).toBeTruthy()
  })

  it('с унаследованным доступом запускает доставку без кредов', async () => {
    const api = open(settings({ ssh_access_level: 'global' }))
    await screen.findByText('Используется общий SSH-доступ (root@10.0.0.5:22)', {}, WAIT)

    fireEvent.click(screen.getByRole('button', { name: 'Доставить' }))

    await waitFor(() => expect(api.deliver).toHaveBeenCalledWith(5, {}), WAIT)
    expect(api.setSettings).not.toHaveBeenCalled()
  })

  it('поверх унаследованного доступа можно задать свой', async () => {
    const api = open(settings({ ssh_access_level: 'global' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Задать свой для этого сервера' }, WAIT))

    fireEvent.change(screen.getByPlaceholderText('SSH-пароль'), { target: { value: 'own-pw' } })
    fireEvent.click(screen.getByRole('button', { name: 'Доставить' }))

    await waitFor(() => expect(api.deliver).toHaveBeenCalledWith(
      5, { ssh_host: '10.0.0.5', ssh_port: 22, ssh_user: 'root', ssh_password: 'own-pw' },
    ), WAIT)
  })

  it('свой вход по-прежнему можно изменить и забыть', async () => {
    const api = open(settings({ ssh_access_level: 'server', has_ssh_password: true }))

    expect(await screen.findByText('Используются сохранённые SSH-креды (root@10.0.0.5:22)', {}, WAIT)).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Изменить креды' })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Забыть креды' }))

    await waitFor(() => expect(api.setSettings).toHaveBeenCalledWith(
      5, { ssh_password: '', ssh_private_key: '', ssh_passphrase: '' },
    ), WAIT)
    // Что останется после своего входа — общий доступ или форма — решает перечитанный ответ бэкенда
    await waitFor(() => expect(api.getSettings).toHaveBeenCalledTimes(2), WAIT)
  })

  it('сервер без входа на любом уровне получает форму', async () => {
    open(settings({}))

    expect(await screen.findByPlaceholderText('SSH-пароль', {}, WAIT)).toBeTruthy()
    expect(screen.queryByText(/Используется/)).toBeNull()
  })
})
