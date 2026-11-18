import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, render, screen, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import Dnat from '../src/pages/Dnat'
import {
  dnatProfilesApi,
  portUsageApi,
  proxyApi,
  serversApi,
  type DnatNodeState,
  type DnatRuleCounters,
  type DnatRuleData,
} from '../src/api/client'
import { formatMoment } from '../src/components/servers/SavedAnswerNote'
import { useServersStore } from '../src/stores/serversStore'
import { makeServer } from './helpers'

vi.mock('../src/components/FAQ', () => ({ FAQIcon: () => null }))

const SERVER_ID = 5
const WAIT = { timeout: 5000 }
// REFRESH_INTERVAL_MS в Dnat.tsx
const POLL_MS = 5000
const SAVED_AT = '2026-11-15T18:02:46.000Z'

const SILENT_TITLE = 'Сервер не отвечает'
const SAVED_READONLY =
  `Показано то, что панель сохранила ${formatMoment(SAVED_AT, 'ru')}. Менять здесь можно будет, когда он вернётся; страница обновится сама.`
const NOTHING_SAVED = /Сохранённых данных этой страницы у панели нет/
const NO_COPY_DETAIL = 'Node is offline and the panel has no saved copy of this answer'
const GATEWAY_ERROR = 'Request failed with status code 502'
const SPEED = /бит\/с/

// 1 250 000 байт за пять секунд между опросами — ровно 2 Мбит/с
const BYTES_PER_POLL = 1_250_000

const RULE: DnatRuleData = {
  name: 'web', protocol: 'tcp', listen_port: 443, listen_port_end: null, target_ip: '10.0.0.2',
  distribution: 'per_server', target_port: 0, masquerade: true, mask_ttl: false, enabled: true, comment: null,
}

const counters = (patch: Partial<DnatRuleCounters> = {}): DnatRuleCounters => ({
  name: RULE.name, present: true, conns: 12, packets_in: 100, bytes_in: 1024, packets_out: 80, bytes_out: 2048,
  targets: [],
  ...patch,
})

const dnatState = (patch: Partial<DnatNodeState> = {}): DnatNodeState => ({
  available: true, ip_forward: true, rules: [RULE], rules_hash: 'a1', healthy: true, missing: [],
  counters: [counters()], applied_at: null, message: null,
  ...patch,
})

// Тесту нужны только data и то, что страница читает из отказа: остальных полей ответа axios она не трогает
const respond = <T,>(data: T) => Promise.resolve({ data }) as never
const refuse = (status: number, detail?: string, headers: Record<string, string> = {}) =>
  Promise.reject({
    message: `Request failed with status code ${status}`,
    response: { status, headers, data: { detail } },
  }) as never
// Так панель отвечает, когда нода молчит, а её ответа в базе нет
const noCopy = () => refuse(409, NO_COPY_DETAIL, { 'x-node-answer-copy-missing': '1' })
// Нода уже не отвечает, а мониторинг ещё не признал её офлайн
const gatewayError = () => refuse(502)

const answerWith = (answer: () => never) => vi.spyOn(proxyApi, 'getDnatState').mockImplementation(answer)

function openPage() {
  render(
    <MemoryRouter initialEntries={[`/panel/server/${SERVER_ID}/dnat`]}>
      <Routes>
        <Route path="/:uid/server/:serverId/dnat" element={<Dnat />} />
      </Routes>
    </MemoryRouter>,
  )
}

/** Срабатывает таймер опроса страницы; к возврату она уже разобрала ответ подмены */
async function nextPoll() {
  await act(async () => {
    vi.advanceTimersByTime(POLL_MS)
  })
}

const ruleRow = () => within(screen.getByText(RULE.name).closest('tr') as HTMLElement)
const button = (name: string) => screen.getByRole('button', { name }) as HTMLButtonElement

describe('Страница DNAT, когда нода не отвечает', () => {
  beforeEach(() => {
    // Подменены только таймер опроса и часы: по ним страница считает скорость между опросами,
    // а ожидания testing-library остаются на настоящем setTimeout
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'Date'] })
    useServersStore.setState({ servers: [], isLoading: false, error: null, metricsUpdatedAt: null })
    vi.spyOn(serversApi, 'list').mockImplementation(
      () => respond({ count: 1, servers: [makeServer({ id: SERVER_ID, name: 'nl-5' })] }),
    )
    vi.spyOn(dnatProfilesApi, 'getAvailableServers').mockImplementation(() => respond([]))
    vi.spyOn(portUsageApi, 'get').mockImplementation(() => respond({}))
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('показывает сохранённую копию с плашкой и не даёт переприменить или снять правила', async () => {
    answerWith(() => respond(dnatState({ saved_at: SAVED_AT })))
    openPage()

    expect(await screen.findByText(SAVED_READONLY, {}, WAIT)).toBeTruthy()
    expect(screen.getByText(SILENT_TITLE)).toBeTruthy()
    expect(ruleRow().getByText('1 KB')).toBeTruthy()
    expect(ruleRow().getByText('активно')).toBeTruthy()
    expect(button('Переприменить').disabled).toBe(true)
    expect(button('Снять правила').disabled).toBe(true)
  })

  it('по копии скорость не считается: прочерк, а не ноль между двумя одинаковыми ответами', async () => {
    const getState = answerWith(() => respond(dnatState({ saved_at: SAVED_AT })))
    openPage()
    await screen.findByText(SAVED_READONLY, {}, WAIT)

    await nextPoll()

    expect(getState).toHaveBeenCalledTimes(2)
    expect(ruleRow().queryByText(SPEED)).toBeNull()
    expect(ruleRow().getAllByText('—')).toHaveLength(2)
    expect(ruleRow().getByText('2 KB')).toBeTruthy()
  })

  it('без копии говорит об этом, а не показывает ошибку запроса', async () => {
    answerWith(noCopy)
    openPage()

    expect(await screen.findByText(NOTHING_SAVED, {}, WAIT)).toBeTruthy()
    expect(screen.getByText(SILENT_TITLE)).toBeTruthy()
    expect(screen.queryByText(NO_COPY_DETAIL)).toBeNull()
    expect(screen.queryByRole('button', { name: 'Переприменить' })).toBeNull()
  })

  it('сбой очередного опроса не стирает уже показанные правила', async () => {
    const getState = answerWith(() => respond(dnatState()))
    openPage()
    await screen.findByText(RULE.name, {}, WAIT)

    getState.mockImplementation(gatewayError)
    await nextPoll()

    expect(getState).toHaveBeenCalledTimes(2)
    expect(ruleRow().getByText('1 KB')).toBeTruthy()
    // Таблица осталась, но устаревшей её видно: ошибка стоит строкой над ней
    expect(screen.getByText(GATEWAY_ERROR)).toBeTruthy()
  })

  it('удачный опрос убирает строку ошибки над таблицей', async () => {
    const getState = answerWith(gatewayError)
    getState.mockImplementationOnce(() => respond(dnatState()))
    openPage()
    await screen.findByText(RULE.name, {}, WAIT)
    await nextPoll()
    expect(screen.getByText(GATEWAY_ERROR)).toBeTruthy()

    getState.mockImplementation(() => respond(dnatState()))
    await nextPoll()

    expect(screen.queryByText(GATEWAY_ERROR)).toBeNull()
  })

  it('после «копии нет» другая ошибка показывается сама, а не прячется за плашкой', async () => {
    const getState = answerWith(noCopy)
    openPage()
    await screen.findByText(NOTHING_SAVED, {}, WAIT)

    getState.mockImplementation(gatewayError)
    await nextPoll()

    expect(screen.queryByText(NOTHING_SAVED)).toBeNull()
    expect(screen.getByText(GATEWAY_ERROR)).toBeTruthy()
  })

  it('сбой первого же опроса показывает ошибку: показать ещё нечего', async () => {
    answerWith(gatewayError)
    openPage()

    expect(await screen.findByText(GATEWAY_ERROR, {}, WAIT)).toBeTruthy()
    expect(screen.queryByText(SILENT_TITLE)).toBeNull()
  })

  it('вернувшаяся нода сама снимает плашку, а скорость считается с её первого ответа', async () => {
    const getState = answerWith(() => respond(dnatState({ saved_at: SAVED_AT })))
    openPage()
    await screen.findByText(SAVED_READONLY, {}, WAIT)

    getState.mockImplementation(() => respond(dnatState()))
    await nextPoll()

    expect(screen.queryByText(SILENT_TITLE)).toBeNull()
    expect(button('Переприменить').disabled).toBe(false)
    expect(button('Снять правила').disabled).toBe(false)
    // Счётчики копии и первого живого ответа не сравниваются: между ними всё время молчания
    expect(ruleRow().queryByText(SPEED)).toBeNull()
    expect(ruleRow().queryByText('—')).toBeNull()

    getState.mockImplementation(() => respond(dnatState({ counters: [counters({ bytes_in: 1024 + BYTES_PER_POLL })] })))
    await nextPoll()

    expect(ruleRow().getByText('2 Мбит/с')).toBeTruthy()
  })

  it('живая нода — без плашки, кнопки доступны, скорость считается между опросами', async () => {
    const getState = answerWith(() => respond(dnatState()))
    openPage()
    await screen.findByText(RULE.name, {}, WAIT)

    expect(screen.queryByText(SILENT_TITLE)).toBeNull()
    expect(button('Переприменить').disabled).toBe(false)
    expect(button('Снять правила').disabled).toBe(false)

    getState.mockImplementation(() => respond(dnatState({ counters: [counters({ bytes_in: 1024 + BYTES_PER_POLL })] })))
    await nextPoll()

    expect(ruleRow().getByText('2 Мбит/с')).toBeTruthy()
    expect(ruleRow().getByText('0 бит/с')).toBeTruthy()
  })
})
