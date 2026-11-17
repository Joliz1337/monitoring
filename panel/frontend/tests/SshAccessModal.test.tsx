import { createElement, type ComponentProps } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { toast } from 'sonner'
import SshAccessModal from '../src/components/sshAccess/SshAccessModal'
import { sshAccessApi, type SshAccessOverview, type SshCheckState } from '../src/api/client'

// Блок «Сейчас» рисуется при каждой отрисовке окна — по нему видно, перерисовывалось ли окно
const overviewRenders = vi.hoisted(() => ({ count: 0 }))
vi.mock('../src/components/sshAccess/SshAccessOverview', async importOriginal => {
  const real = await importOriginal<typeof import('../src/components/sshAccess/SshAccessOverview')>()
  const Counted = (props: ComponentProps<typeof real.default>) => {
    overviewRenders.count += 1
    return createElement(real.default, props)
  }
  return { ...real, default: Counted }
})

const WAIT = { timeout: 5000 }

const OVERVIEW: SshAccessOverview = {
  global: { ssh_user: 'root', ssh_port: 22, auth: 'password', proxy: '1.2.3.4:1080', proxy_mode: 'fallback' },
  folders: [
    { folder: 'GONE', servers: 0, ssh_user: null, ssh_port: 2222, auth: null, proxy: null, proxy_mode: null },
    { folder: 'RU', servers: 2, ssh_user: null, ssh_port: null, auth: 'key', proxy: '5.6.7.8:1080', proxy_mode: 'always' },
  ],
  servers: [
    { server_id: 5, name: 'srv-5', folder: 'RU', ssh_user: 'root', ssh_port: 2200, auth: 'password', proxy: null, proxy_mode: null },
  ],
  summary: { total: 4, server: 1, folder: 1, global: 2, none: 0 },
  without_login: [],
  without_login_total: 0,
}

const NODES = [
  { id: 3, name: 'srv-3', url: 'https://10.0.0.3:9100', folder: 'RU' },
  { id: 5, name: 'srv-5', url: 'https://10.0.0.5:9100', folder: 'RU' },
  { id: 7, name: 'srv-7', url: 'https://10.0.0.7:9100', folder: null },
]

const checkState = (patch: Partial<SshCheckState>): SshCheckState => ({
  job_id: 'job-1', status: 'done', total: 3, done: 3, counts: { ok: 3 }, ok_via_proxy: 0, failed: [],
  started_at: 1, finished_at: 2, ...patch,
})

// Тесту нужны только data: остальные поля ответа axios окно не читает
const respond = <T,>(data: T) => Promise.resolve({ data }) as never

function mockApi(check: SshCheckState | null = null) {
  return {
    overview: vi.spyOn(sshAccessApi, 'overview').mockImplementation(() => respond(OVERVIEW)),
    checkState: vi.spyOn(sshAccessApi, 'checkState').mockImplementation(() => respond({ check })),
    save: vi.spyOn(sshAccessApi, 'save').mockImplementation(() => respond({ changed: 1 })),
    clear: vi.spyOn(sshAccessApi, 'clear').mockImplementation(() => respond({ changed: 1 })),
    startCheck: vi.spyOn(sshAccessApi, 'startCheck').mockImplementation(() => respond({ job_id: 'job-1' })),
    cancelCheck: vi.spyOn(sshAccessApi, 'cancelCheck').mockImplementation(() => respond({ cancelled: true })),
    forgetHostKeys: vi.spyOn(sshAccessApi, 'forgetHostKeys').mockImplementation(() => respond({ forgotten: 1 })),
  }
}

function open(initialServerIds?: number[]) {
  const onChanged = vi.fn()
  render(<SshAccessModal nodes={NODES} initialServerIds={initialServerIds} onChanged={onChanged} onClose={() => {}} />)
  return { onChanged }
}

const levelRow = (label: string) => screen.getByText(label).closest('li') as HTMLElement

describe('Окно «SSH-доступ»', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    vi.spyOn(toast, 'success')
    vi.spyOn(toast, 'error')
  })

  it('показывает, что задано на каждом уровне, и откуда серверы берут вход', async () => {
    mockApi()
    open()

    expect(within(await screen.findByText('Общий', {}, WAIT).then(() => levelRow('Общий')))
      .getByText('прокси 1.2.3.4:1080 (запасной)')).toBeTruthy()
    expect(within(levelRow('Общий')).getByText('root · пароль')).toBeTruthy()
    expect(within(levelRow('Общий')).getByText('порт 22')).toBeTruthy()
    expect(within(levelRow('Папка RU')).getByText('прокси 5.6.7.8:1080 (всегда)')).toBeTruthy()
    expect(within(levelRow('Папка GONE')).getByText('в папке нет серверов')).toBeTruthy()
    expect(screen.getByText('Серверов 4: свой вход 1 · вход папки 1 · общий 2 · без входа 0')).toBeTruthy()
  })

  it('«Всем» сохраняет только введённый пароль и перечитывает сводку', async () => {
    const api = mockApi()
    const { onChanged } = open()
    await screen.findByText('Общий', {}, WAIT)

    fireEvent.change(screen.getByPlaceholderText('SSH-пароль'), { target: { value: 'pw' } })
    fireEvent.click(screen.getByRole('button', { name: 'Сохранить' }))

    await waitFor(() => expect(api.save).toHaveBeenCalledWith({ scope: 'global' }, { ssh_password: 'pw' }), WAIT)
    await waitFor(() => expect(api.overview).toHaveBeenCalledTimes(2), WAIT)
    expect(onChanged).toHaveBeenCalledTimes(1)
    expect(toast.success).toHaveBeenCalledWith('SSH-доступ сохранён')
    expect((screen.getByPlaceholderText('SSH-пароль') as HTMLInputElement).value).toBe('')
  })

  it('папке можно отказаться от прокси, заданного выше', async () => {
    const api = mockApi()
    open()
    await screen.findByText('Общий', {}, WAIT)

    fireEvent.click(screen.getByLabelText('Папкам'))
    fireEvent.click(screen.getByLabelText('RU (2)'))
    fireEvent.click(screen.getByLabelText('без прокси'))
    fireEvent.click(screen.getByRole('button', { name: 'Сохранить' }))

    await waitFor(() => expect(api.save).toHaveBeenCalledWith(
      { scope: 'folders', folders: ['RU'] }, { ssh_proxy_mode: 'off' },
    ), WAIT)
  })

  it('без выбранных папок сохранять некуда', async () => {
    mockApi()
    open()
    await screen.findByText('Общий', {}, WAIT)

    fireEvent.click(screen.getByLabelText('Папкам'))
    fireEvent.change(screen.getByPlaceholderText('SSH-пароль'), { target: { value: 'pw' } })

    expect((screen.getByRole('button', { name: 'Сохранить' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('«Проверить доступ» недоступна, пока форма изменена: проверяется сохранённое', async () => {
    const api = mockApi()
    open()
    await screen.findByText('Общий', {}, WAIT)
    const check = screen.getByRole('button', { name: 'Проверить доступ' }) as HTMLButtonElement
    expect(check.disabled).toBe(false)

    fireEvent.change(screen.getByPlaceholderText('SSH-пароль'), { target: { value: 'pw' } })

    expect(check.disabled).toBe(true)
    fireEvent.click(check)
    expect(api.startCheck).not.toHaveBeenCalled()
  })

  it('открывается на «Серверам» с переданным выбором и проверяет его', async () => {
    const api = mockApi()
    open([3, 7])
    await screen.findByText('Общий', {}, WAIT)

    expect((screen.getByLabelText('Серверам') as HTMLInputElement).checked).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: 'Проверить доступ' }))

    await waitFor(() => expect(api.startCheck).toHaveBeenCalledWith({ scope: 'servers', server_ids: [3, 7] }), WAIT)
  })

  it('убирает прокси у папки, не трогая остальное', async () => {
    const api = mockApi()
    const { onChanged } = open()
    await screen.findByText('Папка RU', {}, WAIT)

    fireEvent.click(within(levelRow('Папка RU')).getByRole('button', { name: 'Убрать прокси' }))

    await waitFor(() => expect(api.clear).toHaveBeenCalledWith({ scope: 'folders', folders: ['RU'] }, ['proxy']), WAIT)
    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1), WAIT)
  })

  it('убирает общий доступ целиком', async () => {
    const api = mockApi()
    open()
    await screen.findByText('Общий', {}, WAIT)

    fireEvent.click(within(levelRow('Общий')).getByRole('button', { name: 'Убрать всё' }))

    await waitFor(() => expect(api.clear).toHaveBeenCalledWith({ scope: 'global' }, ['login', 'port', 'proxy']), WAIT)
  })

  it('показывает итог проверки и принимает новый ключ хоста у сервера', async () => {
    const api = mockApi(checkState({
      total: 4, done: 4, counts: { ok: 2, auth_failed: 1, host_key_changed: 1 }, ok_via_proxy: 1,
      failed: [
        { server_id: 3, name: 'srv-3', status: 'auth_failed', detail: null, login_level: 'global' },
        { server_id: 5, name: 'srv-5', status: 'host_key_changed', detail: null, login_level: 'server' },
      ],
    }))
    open()

    expect(await screen.findByText('Вход работает: 2', {}, WAIT)).toBeTruthy()
    expect(screen.getByText('из них через запасной прокси: 1')).toBeTruthy()
    expect(screen.getByText('Не вышло: 2')).toBeTruthy()
    expect(screen.getByText('неверный логин, пароль или ключ (общий вход)')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Принять новый ключ хоста' }))

    await waitFor(() => expect(api.forgetHostKeys).toHaveBeenCalledWith([5]), WAIT)
    expect(toast.success).toHaveBeenCalledWith('Ключ хоста сброшен — следующее подключение запомнит новый')
  })

  it('идущую проверку показывает ходом и даёт остановить', async () => {
    const api = mockApi(checkState({ status: 'running', total: 10, done: 3, counts: { ok: 3 }, finished_at: null }))
    open()

    expect(await screen.findByText('Проверено 3 из 10', {}, WAIT)).toBeTruthy()
    expect((screen.getByRole('button', { name: 'Проверить доступ' }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: 'Остановить' }))

    await waitFor(() => expect(api.cancelCheck).toHaveBeenCalledTimes(1), WAIT)
  })

  it('перерисовка страницы с теми же данными окно не трогает', async () => {
    // Страница «Обновления» перерисовывается на каждый ответ обхода нод — раз в 150 мс на парке в 2000
    mockApi()
    const noop = () => {}
    const chosen = [3]
    const Page = ({ tick }: { tick: number }) => (
      <div data-tick={tick}>
        <SshAccessModal nodes={NODES} initialServerIds={chosen} onChanged={noop} onClose={noop} />
      </div>
    )
    const { rerender } = render(<Page tick={1} />)
    await screen.findByText('Общий', {}, WAIT)
    const settled = overviewRenders.count

    rerender(<Page tick={2} />)
    rerender(<Page tick={3} />)

    expect(overviewRenders.count).toBe(settled)
  })

  it('новый список нод окно получает', async () => {
    mockApi()
    const noop = () => {}
    const { rerender } = render(<SshAccessModal nodes={NODES} onChanged={noop} onClose={noop} />)
    await screen.findByText('Общий', {}, WAIT)
    fireEvent.click(screen.getByLabelText('Папкам'))
    expect(screen.queryByLabelText('DE (1)')).toBeNull()

    rerender(<SshAccessModal nodes={[...NODES, { id: 9, name: 'srv-9', url: 'https://10.0.0.9:9100', folder: 'DE' }]} onChanged={noop} onClose={noop} />)

    expect(screen.getByLabelText('DE (1)')).toBeTruthy()
  })

  it('отказ с разбором полей вместо текста показывается словами, а не роняет страницу', async () => {
    const api = mockApi()
    api.save.mockImplementation(() => Promise.reject({
      response: { data: { detail: [{ loc: ['body', 'ssh_port'], msg: 'Input should be less than or equal to 65535' }] } },
    }))
    open()
    await screen.findByText('Общий', {}, WAIT)

    fireEvent.change(screen.getByPlaceholderText('SSH-пароль'), { target: { value: 'pw' } })
    fireEvent.click(screen.getByRole('button', { name: 'Сохранить' }))

    await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Не удалось выполнить'), WAIT)
  })

  it('кривой адрес прокси и порт за пределом не уходят на сервер', async () => {
    mockApi()
    open()
    await screen.findByText('Общий', {}, WAIT)
    const saveButton = screen.getByRole('button', { name: 'Сохранить' }) as HTMLButtonElement
    const proxy = screen.getByPlaceholderText('ip:port или ip:port@login:pass')

    fireEvent.change(proxy, { target: { value: 'socks5://1.2.3.4:1080' } })
    expect(saveButton.disabled).toBe(true)
    expect(screen.getByText('Прокси — в виде ip:port или ip:port@login:pass')).toBeTruthy()

    fireEvent.change(proxy, { target: { value: '1.2.3.4:1080' } })
    expect(saveButton.disabled).toBe(false)

    fireEvent.change(screen.getByPlaceholderText('22'), { target: { value: '70000' } })
    expect(saveButton.disabled).toBe(true)
    expect(screen.getByText('Порт — от 1 до 65535')).toBeTruthy()
  })

  it('отказ сервера при сохранении показывает его причину', async () => {
    const api = mockApi()
    api.save.mockImplementation(() => Promise.reject({ response: { data: { detail: 'В папке «RU» нет серверов' } } }))
    const { onChanged } = open()
    await screen.findByText('Общий', {}, WAIT)

    fireEvent.change(screen.getByPlaceholderText('SSH-пароль'), { target: { value: 'pw' } })
    fireEvent.click(screen.getByRole('button', { name: 'Сохранить' }))

    await waitFor(() => expect(toast.error).toHaveBeenCalledWith('В папке «RU» нет серверов'), WAIT)
    expect(onChanged).not.toHaveBeenCalled()
    expect((screen.getByPlaceholderText('SSH-пароль') as HTMLInputElement).value).toBe('pw')
  })
})
