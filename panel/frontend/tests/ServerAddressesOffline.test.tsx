import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import ServerAddressesEditor from '../src/components/haproxy/ServerAddressesEditor'
import { proxyApi, type HAProxyServerStatus, type NetworkState } from '../src/api/client'

const WAIT = { timeout: 5000 }
const SAVED_AT = '2026-11-15T18:02:46.000Z'
const SAVED_QUEUED = /Показано состояние, которое панель получила от него/
const OFFLINE_NOTICE = 'Сервер офлайн — адреса появятся, когда он вернётся в сеть'

// Тесту нужны только data и заголовки отказа: остальные поля ответа axios блок не читает
const respond = <T,>(data: T) => Promise.resolve({ data }) as never
// Так панель отвечает, когда нода молчит, а её адресов у панели нет
const noCopy = () => Promise.reject({
  response: { status: 409, headers: { 'x-node-answer-copy-missing': '1' }, data: {} },
}) as never

const server = (patch: Partial<HAProxyServerStatus> = {}): HAProxyServerStatus => ({
  server_id: 6, server_name: 'макс 6', server_url: 'https://203.0.113.6:9100', online: false,
  sync_status: 'synced', config_hash: null, last_sync_at: null, haproxy_running: true, metrics: null,
  listen_ips: [], source_ips: [], addresses_supported: true, addresses_min_node_version: '10.29.0',
  ...patch,
})

const network = (patch: Partial<NetworkState> = {}): NetworkState => ({
  supported: true,
  min_node_version: '10.29.0',
  interfaces: [{
    name: 'eth0', is_up: true, is_default: true, kind: 'physical',
    addresses: [
      { address: '203.0.113.6', prefix: 24, family: 'ipv4', scope: 'global', managed: false, primary: true, dynamic: false },
      { address: '203.0.113.77', prefix: 32, family: 'ipv4', scope: 'global', managed: true, primary: false, dynamic: false },
    ],
  }],
  managed: [],
  transaction: null,
  history: [],
  job: null,
  ...patch,
})

const open = (status: HAProxyServerStatus) =>
  render(<ServerAddressesEditor profileId={1} server={status} onSaved={() => {}} />)

describe('Адреса сервера в профиле HAProxy, когда сервер офлайн', () => {
  it('показывает адреса, которые панель запомнила, и говорит, что правки доедут позже', async () => {
    const getState = vi.spyOn(proxyApi, 'getNetworkState').mockImplementation(
      () => respond(network({ saved_at: SAVED_AT })),
    )
    open(server())

    expect(await screen.findByText('203.0.113.77', {}, WAIT)).toBeTruthy()
    expect(screen.getByText('203.0.113.6')).toBeTruthy()
    expect(screen.getByText(SAVED_QUEUED)).toBeTruthy()
    expect(screen.queryByText(OFFLINE_NOTICE)).toBeNull()
    expect(getState).toHaveBeenCalledWith(6)
  })

  it('без запомненных адресов говорит прежнее: появятся, когда сервер вернётся', async () => {
    vi.spyOn(proxyApi, 'getNetworkState').mockImplementation(noCopy)
    open(server({ listen_ips: ['203.0.113.77'] }))

    expect(await screen.findByText(OFFLINE_NOTICE, {}, WAIT)).toBeTruthy()
    // Уже выбранный адрес остаётся в списке, чтобы его можно было снять
    expect(screen.getByText('203.0.113.77')).toBeTruthy()
  })

  it('у сервера в сети пометки нет', async () => {
    vi.spyOn(proxyApi, 'getNetworkState').mockImplementation(() => respond(network()))
    open(server({ online: true }))

    expect(await screen.findByText('203.0.113.77', {}, WAIT)).toBeTruthy()
    expect(screen.queryByText(SAVED_QUEUED)).toBeNull()
  })
})
