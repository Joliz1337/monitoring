import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import BandwidthLimitCard from '../src/components/servers/BandwidthLimitCard'
import HosterAccessCard from '../src/components/servers/HosterAccessCard'
import NetworkAddressesCard from '../src/components/servers/NetworkAddressesCard'
import {
  proxyApi,
  type BandwidthLimitState,
  type HosterScanState,
  type NetworkState,
} from '../src/api/client'

const WAIT = { timeout: 5000 }
const SAVED_AT = '2026-11-15T18:02:46.000Z'

const SAVED_NOTE = /Сервер не отвечает — показано то, что панель сохранила/
const FROM_METRICS_NOTE = /адреса взяты из его последнего ответа/
const NO_COPY_NOTE = /копии этих данных у панели нет/

// Тесту нужны только data и заголовки отказа: остальные поля ответа axios карточки не читают
const respond = <T,>(data: T) => Promise.resolve({ data }) as never
const refuse = (status: number, headers: Record<string, string> = {}) =>
  Promise.reject({ response: { status, headers, data: {} } }) as never
// Так панель отвечает, когда нода молчит, а её ответа в базе нет
const noCopy = () => refuse(409, { 'x-node-answer-copy-missing': '1' })

const limit = (patch: Partial<BandwidthLimitState> = {}): BandwidthLimitState => ({
  enabled: true, mbit: 500, iface: 'eth0', applied: true, applied_mbit: 500, qdisc: 'tbf', in_sync: true,
  ...patch,
})

const network = (patch: Partial<NetworkState> = {}): NetworkState => ({
  supported: true,
  min_node_version: '10.29.0',
  node_version: '10.36.1',
  interfaces: [{
    name: 'eth0', is_up: true, is_default: true, kind: 'physical',
    addresses: [{
      address: '203.0.113.7', prefix: 24, family: 'ipv4', scope: 'global',
      managed: true, primary: false, dynamic: false,
    }],
  }],
  managed: [],
  transaction: null,
  history: [],
  job: null,
  ...patch,
})

const scan = (patch: Partial<HosterScanState> = {}): HosterScanState => ({
  supported: true,
  findings: [{
    id: 'qemu-ga', category: 'agent', title: 'Гостевой агент QEMU', detail: 'qemu-guest-agent установлен',
    severity: 'danger', access_critical: true, default_selected: true, remove_hint: '',
  }],
  ...patch,
})

describe('Лимит полосы, когда нода не отвечает', () => {
  it('показывает сохранённый лимит с пометкой и не даёт его менять', async () => {
    vi.spyOn(proxyApi, 'getBandwidthLimit').mockImplementation(() => respond(limit({ saved_at: SAVED_AT })))
    render(<BandwidthLimitCard serverId={5} />)

    expect(await screen.findByText(SAVED_NOTE, {}, WAIT)).toBeTruthy()
    expect((screen.getByRole('spinbutton') as HTMLInputElement).value).toBe('500')
    expect((screen.getByRole('button', { name: 'Применить' }) as HTMLButtonElement).disabled).toBe(true)
    expect((screen.getByRole('checkbox') as HTMLInputElement).disabled).toBe(true)
  })

  it('живой ответ ноды — без пометки, лимит можно менять', async () => {
    vi.spyOn(proxyApi, 'getBandwidthLimit').mockImplementation(() => respond(limit()))
    render(<BandwidthLimitCard serverId={5} />)

    expect(await screen.findByDisplayValue('500', {}, WAIT)).toBeTruthy()
    expect(screen.queryByText(SAVED_NOTE)).toBeNull()
    expect((screen.getByRole('button', { name: 'Применить' }) as HTMLButtonElement).disabled).toBe(false)
  })

  it('без копии говорит об этом, а не показывает пустую форму', async () => {
    vi.spyOn(proxyApi, 'getBandwidthLimit').mockImplementation(noCopy)
    render(<BandwidthLimitCard serverId={5} />)

    expect(await screen.findByText(NO_COPY_NOTE, {}, WAIT)).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Применить' })).toBeNull()
  })
})

describe('IP-адреса, когда нода не отвечает', () => {
  it('показывает сохранённые адреса с пометкой и не даёт их менять', async () => {
    vi.spyOn(proxyApi, 'getNetworkState').mockImplementation(() => respond(network({ saved_at: SAVED_AT })))
    render(<NetworkAddressesCard serverId={5} />)

    expect(await screen.findByText(SAVED_NOTE, {}, WAIT)).toBeTruthy()
    expect(screen.getByText('203.0.113.7/24')).toBeTruthy()
    expect((screen.getByRole('button', { name: /Добавить IP/ }) as HTMLButtonElement).disabled).toBe(true)
    expect((screen.getByRole('button', { name: /Удалить/ }) as HTMLButtonElement).disabled).toBe(true)
    expect(screen.queryByRole('checkbox')).toBeNull()
  })

  it('живой ответ ноды — без пометки, адреса можно менять', async () => {
    vi.spyOn(proxyApi, 'getNetworkState').mockImplementation(() => respond(network()))
    render(<NetworkAddressesCard serverId={5} />)

    expect(await screen.findByText('203.0.113.7/24', {}, WAIT)).toBeTruthy()
    expect(screen.queryByText(SAVED_NOTE)).toBeNull()
    expect((screen.getByRole('button', { name: /Добавить IP/ }) as HTMLButtonElement).disabled).toBe(false)
  })

  it('адреса из последних метрик подписаны как неполные', async () => {
    vi.spyOn(proxyApi, 'getNetworkState').mockImplementation(
      () => respond(network({ saved_at: SAVED_AT, from_metrics: true })),
    )
    render(<NetworkAddressesCard serverId={5} />)

    expect(await screen.findByText(FROM_METRICS_NOTE, {}, WAIT)).toBeTruthy()
    expect(screen.getByText('203.0.113.7/24')).toBeTruthy()
    expect(screen.queryByText(SAVED_NOTE)).toBeNull()
  })

  it('без копии говорит об этом, а не об ошибке загрузки', async () => {
    vi.spyOn(proxyApi, 'getNetworkState').mockImplementation(noCopy)
    render(<NetworkAddressesCard serverId={5} />)

    expect(await screen.findByText(NO_COPY_NOTE, {}, WAIT)).toBeTruthy()
    expect(screen.queryByText('Не удалось получить состояние сети ноды')).toBeNull()
  })
})

describe('Доступы хостера, когда нода не отвечает', () => {
  it('показывает сохранённые находки с пометкой и не даёт их вырезать', async () => {
    vi.spyOn(proxyApi, 'scanHosterAccess').mockImplementation(() => respond(scan({ saved_at: SAVED_AT })))
    render(<HosterAccessCard serverId={5} />)

    expect(await screen.findByText(SAVED_NOTE, {}, WAIT)).toBeTruthy()
    expect(screen.getByText('Гостевой агент QEMU')).toBeTruthy()
    expect((screen.getByRole('button', { name: /Вырезать выбранное/ }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('живой ответ ноды — без пометки, находки можно вырезать', async () => {
    vi.spyOn(proxyApi, 'scanHosterAccess').mockImplementation(() => respond(scan()))
    render(<HosterAccessCard serverId={5} />)

    expect(await screen.findByText('Гостевой агент QEMU', {}, WAIT)).toBeTruthy()
    expect(screen.queryByText(SAVED_NOTE)).toBeNull()
    expect((screen.getByRole('button', { name: /Вырезать выбранное/ }) as HTMLButtonElement).disabled).toBe(false)
  })

  it('без копии не пишет, что на сервере чисто', async () => {
    vi.spyOn(proxyApi, 'scanHosterAccess').mockImplementation(noCopy)
    render(<HosterAccessCard serverId={5} />)

    expect(await screen.findByText(NO_COPY_NOTE, {}, WAIT)).toBeTruthy()
    expect(screen.queryByText(/Ничего лишнего не найдено/)).toBeNull()
  })

  it('сбой сканирования не выдаётся за чистый сервер', async () => {
    vi.spyOn(proxyApi, 'scanHosterAccess').mockImplementation(() => refuse(500))
    render(<HosterAccessCard serverId={5} />)

    expect(await screen.findByText('Не удалось просканировать сервер', {}, WAIT)).toBeTruthy()
    expect(screen.queryByText(/Ничего лишнего не найдено/)).toBeNull()
  })
})

describe('Нода замолчала или вернулась, пока страница открыта', () => {
  it('карточка перечитывает данные сама и показывает копию', async () => {
    const getLimit = vi.spyOn(proxyApi, 'getBandwidthLimit').mockImplementation(() => respond(limit()))
    const { rerender } = render(<BandwidthLimitCard serverId={5} nodeSilent={false} />)
    await screen.findByDisplayValue('500', {}, WAIT)

    getLimit.mockImplementation(() => respond(limit({ mbit: 300, saved_at: SAVED_AT })))
    rerender(<BandwidthLimitCard serverId={5} nodeSilent />)

    expect(await screen.findByText(SAVED_NOTE, {}, WAIT)).toBeTruthy()
    expect((screen.getByRole('spinbutton') as HTMLInputElement).value).toBe('300')
    expect(getLimit).toHaveBeenCalledTimes(2)
  })

  it('вернувшаяся нода снимает пометку и снова даёт менять адреса', async () => {
    const getState = vi.spyOn(proxyApi, 'getNetworkState').mockImplementation(
      () => respond(network({ saved_at: SAVED_AT })),
    )
    const { rerender } = render(<NetworkAddressesCard serverId={5} nodeSilent />)
    await screen.findByText(SAVED_NOTE, {}, WAIT)

    getState.mockImplementation(() => respond(network()))
    rerender(<NetworkAddressesCard serverId={5} nodeSilent={false} />)

    await waitFor(() => expect(screen.queryByText(SAVED_NOTE)).toBeNull(), WAIT)
    expect((screen.getByRole('button', { name: /Добавить IP/ }) as HTMLButtonElement).disabled).toBe(false)
  })

  it('идущее вырезание доступов хостера не прерывается повторным сканом', async () => {
    const scanNode = vi.spyOn(proxyApi, 'scanHosterAccess').mockImplementation(() => respond(scan()))
    // Вырезание не завершается: тест смотрит, что происходит, пока оно идёт
    vi.spyOn(proxyApi, 'purgeHosterAccess').mockImplementation(() => new Promise(() => {}) as never)
    const { rerender } = render(<HosterAccessCard serverId={5} nodeSilent={false} />)
    fireEvent.click(await screen.findByRole('button', { name: /Вырезать выбранное/ }, WAIT))
    fireEvent.click(screen.getByText(/У меня есть независимый доступ/))
    fireEvent.click(screen.getByRole('button', { name: 'Вырезать' }))

    rerender(<HosterAccessCard serverId={5} nodeSilent />)
    await new Promise(resolve => setTimeout(resolve, 50))

    expect(scanNode).toHaveBeenCalledTimes(1)
    expect(screen.getByText('Гостевой агент QEMU')).toBeTruthy()
  })
})
