import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { toast } from 'sonner'
import QuarantineBlock from '../src/components/loss/QuarantineBlock'
import { spreadLevel } from '../src/components/loss/quarantineSpread'
import { lossApi, type LossRelay, type LossTarget, type QuarantineItem, type QuarantineOverview } from '../src/api/client'

const NOW = 1_800_000_000
const MINUTE = 60
const MERGE_PORTS_KEY = 'loss_quarantine_merge_ports'

type Relay = [id: number, name: string]

const BRIDGE_3: Relay = [3, 'Вк мост 3']
const BRIDGE_5: Relay = [5, 'Вк мост 5']
const relays = (count: number): Relay[] => Array.from({ length: count }, (_, index) => [100 + index, `релей ${index + 1}`])

function item(target: string, relay: Relay, over: Partial<QuarantineItem> = {}): QuarantineItem {
  const [ip, port] = target.split(':')
  return {
    target, ip, port: Number(port), owner: 'Тубо 2', server_id: relay[0], server_name: relay[1],
    state: 'disabled', reason: { kind: 'no_reply', loss_pct: 100, downs: 0 },
    disabled_at: NOW - 61 * MINUTE, until: NOW + 59 * MINUTE, strikes: 1, sessions: 0,
    blocked_reason: null, partial_reason: null, ...over,
  }
}

const on = (target: string, where: Relay[], over: Partial<QuarantineItem> = {}) =>
  where.map(relay => item(target, relay, over))

const refused = { state: 'blocked', blocked_reason: 'share_limit', until: null } as const

const overview = (items: QuarantineItem[], over: Partial<QuarantineOverview> = {}): QuarantineOverview =>
  ({ mode: 'enforce', items, chronic: [], paused_relays: [], ...over })

const LOSS_THRESHOLD = 50

const measured = ([server_id, name]: Relay, loss_pct: number, answers: boolean | null = null): LossRelay =>
  ({ server_id, name, loss_pct, rtt_ms: 40, samples: 60, answers })

// Строка таблицы потерь: все релеи, где стоит адрес, и что видит каждый
function probed(target: string, readings: LossRelay[]): LossTarget {
  const [ip, port] = target.split(':')
  return {
    target, ip, port: Number(port), owner: 'Тубо 2', total_only: false, relays: readings,
    worst_loss: Math.max(...readings.map(reading => reading.loss_pct)), episode: null, quarantined: [],
  }
}

interface Probes {
  // null — таблица потерь ещё не загружена
  targets?: LossTarget[] | null
  lossThreshold?: number | null
}

function show(view: QuarantineOverview, { targets = null, lossThreshold = LOSS_THRESHOLD }: Probes = {}) {
  const handlers = { onReleased: vi.fn(), onExempt: vi.fn(), onEdit: vi.fn() }
  const block = (shown: QuarantineOverview) => (
    <QuarantineBlock
      view={shown} targets={targets} lossThreshold={lossThreshold} sustainedSeconds={180} canExempt {...handlers}
    />
  )
  const rendered = render(block(view))
  // Страница перечитывает список раз в 10 с — блок получает новый ответ, не теряя своего состояния
  const refresh = (next: QuarantineOverview) => rendered.rerender(block(next))
  return { ...handlers, ...rendered, refresh }
}

const card = (owner: string) => screen.getByRole('region', { name: owner })
const releaseAllButton = (owner: string, name: string | RegExp) =>
  within(card(owner)).getByRole('button', { name }) as HTMLButtonElement
const rowsOf = (owner: string) => within(card(owner)).getAllByRole('listitem')
const rowOf = (address: string) => screen.getByText(address).closest('li') as HTMLElement
const expand = (address: string) => fireEvent.click(screen.getByText(address))
const mergePorts = () => fireEvent.click(screen.getByRole('checkbox'))

function pickAction(address: string, action: string) {
  fireEvent.click(within(rowOf(address)).getByRole('button', { name: 'Действия' }))
  fireEvent.click(screen.getByRole('menuitem', { name: action }))
}

describe('Потери: блок «Отключённые адреса»', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.spyOn(Date, 'now').mockReturnValue(NOW * 1000)
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('адреса разложены по карточкам нод', () => {
    show(overview([
      ...on('176.97.210.169:8449', [BRIDGE_3, BRIDGE_5], { owner: 'тубо' }),
      ...on('193.141.60.121:8449', [BRIDGE_3]),
      ...on('193.141.60.242:8449', [BRIDGE_3]),
    ]))

    expect(rowsOf('тубо')).toHaveLength(1)
    expect(rowsOf('Тубо 2')).toHaveLength(2)
    expect(card('Тубо 2').textContent).toContain('2 адреса · отключено 2')
  })

  it('в строке — где, почему, сколько соединений переведено и когда вернётся', () => {
    show(overview([
      item('176.97.210.169:8449', BRIDGE_3, { sessions: 9 }),
      item('176.97.210.169:8449', BRIDGE_5, { sessions: 5 }),
    ]))

    const row = rowOf('176.97.210.169:8449').textContent
    expect(row).toContain('Вк мост 3, Вк мост 5 · не отвечает как клиент · 14 соед.')
    expect(row).toContain('через 59 мин')
  })

  it('длинный список релеев свёрнут в число, подробности — по клику на строку', () => {
    show(overview(on('193.141.60.129:8443', relays(12), { sessions: 100 })))
    const row = rowOf('193.141.60.129:8443')
    // Тысячи отделены неразрывным пробелом — так числа пишет русская локаль
    expect(row.textContent).toContain('12 релеев · не отвечает как клиент · 1 200 соед.')
    expect(row.textContent).not.toContain('релей 7')

    expand('193.141.60.129:8443')

    expect(row.textContent).toContain('Отключён на 12 релеях')
    expect(row.textContent).toContain('релей 7')
    expect(row.textContent).toContain(
      'не отвечает на проверку как клиент · 1 ч 1 мин назад · вернётся не раньше чем через 59 мин, когда будет отвечать на проверку · соединений переведено: 1 200',
    )
  })

  it('в подробностях имена релеев обрезаются: адрес бывает отключён на сотнях', () => {
    show(overview(on('193.141.60.129:8443', relays(40))))
    const row = rowOf('193.141.60.129:8443')

    expand('193.141.60.129:8443')

    expect(row.textContent).toContain('релей 12')
    expect(row.textContent).not.toContain('релей 13')
    expect(row.textContent).toContain('и ещё 28')
  })

  it('адрес, который релей отключить отказался, помечен отдельной строкой', () => {
    show(overview([
      ...on('193.141.60.242:8449', [BRIDGE_3, BRIDGE_5], refused),
      ...on('193.141.60.243:8449', [BRIDGE_3], refused),
      ...on('193.141.60.243:8449', [BRIDGE_5]),
    ]))

    expect(rowOf('193.141.60.242:8449').textContent).toContain('не отключён на 2 релеях: предел доли серверов правила')
    expect(within(rowOf('193.141.60.242:8449')).getByText('не отключён')).toBeTruthy()
    expect(rowOf('193.141.60.243:8449').textContent).toContain('через 59 мин')
    expect(rowOf('193.141.60.243:8449').textContent).toContain('не отключён на 1 релее: предел доли серверов правила')
    expect(card('Тубо 2').textContent).toContain('2 адреса · отключено 1 · не удалось отключить 2')
  })

  it('«Не различать порты» склеивает порты одного IP в одну строку и запоминается', () => {
    const view = overview([
      ...on('193.141.60.129:8443', [BRIDGE_3], { sessions: 7 }),
      ...on('193.141.60.129:8449', [BRIDGE_5], { sessions: 4 }),
    ])
    const { unmount } = show(view)
    expect(rowsOf('Тубо 2')).toHaveLength(2)

    mergePorts()

    expect(rowsOf('Тубо 2')).toHaveLength(1)
    const row = rowOf('193.141.60.129').textContent
    expect(row).toContain(':8443, :8449')
    expect(row).toContain('Вк мост 3, Вк мост 5 · не отвечает как клиент · 11 соед.')
    expect(card('Тубо 2').textContent).toContain('1 адрес · отключено 1')
    expect(localStorage.getItem(MERGE_PORTS_KEY)).toBe('1')

    unmount()
    show(view)
    expect(rowsOf('Тубо 2')).toHaveLength(1)
  })

  it('релей, где IP отключён на двух портах, считается один раз', () => {
    show(overview([...on('193.141.60.129:8443', relays(12)), ...on('193.141.60.129:8449', relays(12))]))

    mergePorts()

    expect(rowOf('193.141.60.129').textContent).toContain('12 релеев · не отвечает как клиент')
  })

  it('число постоянных проблем у склеенного IP — худшее из его портов', () => {
    show(overview(
      [...on('193.141.60.129:8443', [BRIDGE_3]), ...on('193.141.60.129:8449', [BRIDGE_5])],
      { chronic: [
        { target: '193.141.60.129:8443', ip: '193.141.60.129', port: 8443, owner: 'Тубо 2', strikes: 7 },
        { target: '193.141.60.129:8449', ip: '193.141.60.129', port: 8449, owner: 'Тубо 2', strikes: 8 },
      ] },
    ))
    expect(rowOf('193.141.60.129:8443').textContent).toContain('7 за сутки')

    mergePorts()

    expect(rowOf('193.141.60.129').textContent).toContain('8 за сутки')
  })

  it('«Вернуть» возвращает адрес на всех его релеях, а без портов — и на всех портах', async () => {
    const release = vi.spyOn(lossApi, 'releaseQuarantine').mockResolvedValue({ data: { released: true } } as never)
    const { onReleased } = show(overview([
      ...on('193.141.60.129:8443', [BRIDGE_3, BRIDGE_5]),
      ...on('193.141.60.129:8449', [BRIDGE_3]),
    ]))

    pickAction('193.141.60.129:8449', 'Вернуть')
    await waitFor(() => expect(onReleased).toHaveBeenCalledTimes(1))
    expect(release.mock.calls).toEqual([[3, '193.141.60.129:8449']])

    release.mockClear()
    mergePorts()
    pickAction('193.141.60.129', 'Вернуть')
    await waitFor(() => expect(onReleased).toHaveBeenCalledTimes(2))
    expect(release.mock.calls).toEqual([
      [3, '193.141.60.129:8443'], [5, '193.141.60.129:8443'], [3, '193.141.60.129:8449'],
    ])
  })

  it('«Не отключать» и замена берут один порт, а без портов — IP целиком', () => {
    const { onExempt, onEdit } = show(overview([
      ...on('193.141.60.129:8443', [BRIDGE_3]),
      ...on('193.141.60.129:8449', [BRIDGE_5]),
    ]))

    pickAction('193.141.60.129:8449', 'Не отключать')
    pickAction('193.141.60.129:8449', 'Заменить / удалить')
    expect(onExempt).toHaveBeenLastCalledWith({ ip: '193.141.60.129', port: 8449 })
    expect(onEdit).toHaveBeenLastCalledWith({ ip: '193.141.60.129', port: 8449 })

    mergePorts()
    pickAction('193.141.60.129', 'Не отключать')
    pickAction('193.141.60.129', 'Заменить / удалить')
    expect(onExempt).toHaveBeenLastCalledWith({ ip: '193.141.60.129', port: null })
    // Окно замены по умолчанию правит все порты IP — ему хватает любого из них
    expect(onEdit).toHaveBeenLastCalledWith({ ip: '193.141.60.129', port: 8443 })
  })

  it('«Вернуть все» срабатывает только со второго нажатия', async () => {
    const release = vi.spyOn(lossApi, 'releaseQuarantine').mockResolvedValue({ data: { released: true } } as never)
    const { onReleased } = show(overview([
      ...on('193.141.60.121:8449', [BRIDGE_3]),
      ...on('193.141.60.129:8449', [BRIDGE_3, BRIDGE_5]),
      ...on('193.141.60.242:8449', [BRIDGE_5], refused),
    ]))

    fireEvent.click(within(card('Тубо 2')).getByRole('button', { name: 'Вернуть все 2' }))
    expect(release).not.toHaveBeenCalled()

    fireEvent.click(within(card('Тубо 2')).getByRole('button', { name: 'Точно вернуть 2?' }))
    await waitFor(() => expect(onReleased).toHaveBeenCalledTimes(1))
    expect(release.mock.calls).toEqual([
      [3, '193.141.60.121:8449'], [3, '193.141.60.129:8449'], [5, '193.141.60.129:8449'],
    ])
  })

  it('подтверждение «Вернуть все» само гаснет через несколько секунд', () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    const release = vi.spyOn(lossApi, 'releaseQuarantine').mockResolvedValue({ data: { released: true } } as never)
    show(overview([...on('193.141.60.121:8449', [BRIDGE_3]), ...on('193.141.60.129:8449', [BRIDGE_3])]))
    fireEvent.click(releaseAllButton('Тубо 2', 'Вернуть все 2'))
    expect(releaseAllButton('Тубо 2', 'Точно вернуть 2?')).toBeTruthy()

    act(() => { vi.advanceTimersByTime(5000) })

    fireEvent.click(releaseAllButton('Тубо 2', 'Вернуть все 2'))
    expect(release).not.toHaveBeenCalled()
  })

  it('подтверждение не переносится на другое число адресов', () => {
    const release = vi.spyOn(lossApi, 'releaseQuarantine').mockResolvedValue({ data: { released: true } } as never)
    const two = [...on('193.141.60.121:8449', [BRIDGE_3]), ...on('193.141.60.129:8449', [BRIDGE_3])]
    const { refresh } = show(overview(two))
    fireEvent.click(releaseAllButton('Тубо 2', 'Вернуть все 2'))

    refresh(overview([...two, ...on('193.141.60.243:8449', [BRIDGE_3])]))
    fireEvent.click(releaseAllButton('Тубо 2', 'Вернуть все 3'))

    expect(releaseAllButton('Тубо 2', 'Точно вернуть 3?')).toBeTruthy()
    expect(release).not.toHaveBeenCalled()
  })

  it('пока идёт возврат всех адресов, кнопка остаётся на месте и второй раз не запускается', async () => {
    let finish = () => {}
    const pending = new Promise(resolve => { finish = () => resolve({ data: { released: true } }) })
    const release = vi.spyOn(lossApi, 'releaseQuarantine').mockReturnValue(pending as never)
    const first = on('193.141.60.121:8449', [BRIDGE_3])
    const { refresh, onReleased } = show(overview([...first, ...on('193.141.60.129:8449', [BRIDGE_3])]))
    fireEvent.click(releaseAllButton('Тубо 2', 'Вернуть все 2'))
    fireEvent.click(releaseAllButton('Тубо 2', 'Точно вернуть 2?'))

    // Список обновился раньше, чем дошли все запросы: отключённым остался один адрес
    refresh(overview(first))

    expect(releaseAllButton('Тубо 2', 'Вернуть все 2').disabled).toBe(true)
    finish()
    await waitFor(() => expect(onReleased).toHaveBeenCalledTimes(1))
    expect(release).toHaveBeenCalledTimes(2)
    expect(within(card('Тубо 2')).queryByRole('button', { name: /Вернуть все/ })).toBeNull()
  })

  it('сбой возврата всех адресов называет, сколько возвратов не прошло', async () => {
    vi.spyOn(lossApi, 'releaseQuarantine').mockImplementation(((serverId: number) =>
      serverId === BRIDGE_5[0] ? Promise.reject(new Error('timeout')) : Promise.resolve({ data: { released: true } })) as never)
    const toastError = vi.spyOn(toast, 'error')
    const { onReleased } = show(overview([
      ...on('193.141.60.121:8449', [BRIDGE_3]),
      ...on('193.141.60.129:8449', [BRIDGE_3, BRIDGE_5]),
    ]))

    fireEvent.click(releaseAllButton('Тубо 2', 'Вернуть все 2'))
    fireEvent.click(releaseAllButton('Тубо 2', 'Точно вернуть 2?'))

    await waitFor(() => expect(onReleased).toHaveBeenCalledTimes(1))
    expect(toastError).toHaveBeenCalledWith('Вернуть не удалось в 1 из 3 случаев')
  })

  it('адрес, который не удалось отключить, стоит в карточке первым, а не прячется под кнопку', () => {
    const disabled = Array.from({ length: 12 }, (_, index) => item(`10.0.0.${index + 1}:443`, BRIDGE_3))
    show(overview([...disabled, ...on('10.0.9.9:443', [BRIDGE_3], refused)]))

    expect(rowsOf('Тубо 2')[0].textContent).toContain('10.0.9.9:443')
    expect(rowsOf('Тубо 2')[1].textContent).toContain('10.0.0.1:443')
  })

  it('отказ без названной причины и «ошибка HAProxy» идут одной строкой', () => {
    show(overview([
      item('193.141.60.242:8449', BRIDGE_3, { ...refused, blocked_reason: null }),
      item('193.141.60.242:8449', BRIDGE_5, { ...refused, blocked_reason: 'error' }),
    ]))

    expect(rowOf('193.141.60.242:8449').textContent).toContain('не отключён на 2 релеях: ошибка HAProxy')
  })

  it('в подробностях метка «N за сутки» расшифрована словами', () => {
    show(overview(on('193.141.60.129:8443', [BRIDGE_3]), {
      chronic: [{ target: '193.141.60.129:8443', ip: '193.141.60.129', port: 8443, owner: 'Тубо 2', strikes: 7 }],
    }))
    const row = rowOf('193.141.60.129:8443')
    expect(row.textContent).not.toContain('постоянные проблемы')

    expand('193.141.60.129:8443')

    expect(row.textContent).toContain('постоянные проблемы · 7 отключений за сутки')
  })

  it('адрес, который не работает на всех своих релеях, помечен «везде»', () => {
    show(overview(on('193.141.60.129:8443', [BRIDGE_3, BRIDGE_5])), {
      targets: [probed('193.141.60.129:8443', [measured(BRIDGE_3, 100, false), measured(BRIDGE_5, 100, false)])],
    })

    expect(rowOf('193.141.60.129:8443').textContent).toContain('везде · 2 из 2')
  })

  it('у адреса, который не работает на части релеев, видно, на скольких из скольких', () => {
    const healthy = relays(4).map(relay => measured(relay, 0, true))
    show(overview(on('176.97.210.169:8449', [BRIDGE_3, BRIDGE_5])), {
      targets: [probed('176.97.210.169:8449', [measured(BRIDGE_3, 100, false), measured(BRIDGE_5, 100, false), ...healthy])],
    })

    const row = rowOf('176.97.210.169:8449')
    expect(within(row).getByTitle('Не работает на 2 из 6 релеев, где стоит этот адрес').textContent).toBe('2 из 6')
    expect(row.textContent).not.toContain('везде')
  })

  it('релей, где адрес не отвечает, считается, даже если в списке отключённых его нет', () => {
    show(overview(on('176.97.210.169:8449', [BRIDGE_3])), {
      targets: [probed('176.97.210.169:8449', [
        measured(BRIDGE_3, 100, false), measured(BRIDGE_5, 100, false), measured([7, 'Ланд'], 0, true),
      ])],
    })

    expect(rowOf('176.97.210.169:8449').textContent).toContain('2 из 3')
  })

  it('потери не ниже порога отключения — тоже «не работает», меньшие — нет', () => {
    const readings = [measured(BRIDGE_3, 100), measured(BRIDGE_5, 60), measured([7, 'Ланд'], 49), measured([8, 'Ланд 2'], 0)]
    const view = overview(on('176.97.210.169:8449', [BRIDGE_3]))
    const { unmount } = show(view, { targets: [probed('176.97.210.169:8449', readings)] })
    expect(rowOf('176.97.210.169:8449').textContent).toContain('2 из 4')
    unmount()

    // Порог неизвестен, пока настройки не загружены: остаются только релеи из списка
    show(view, { targets: [probed('176.97.210.169:8449', readings)], lossThreshold: null })
    expect(rowOf('176.97.210.169:8449').textContent).toContain('1 из 4')
  })

  it('без различия портов релеи считаются по всем портам IP', () => {
    show(overview(on('193.141.60.129:8443', [BRIDGE_3])), {
      targets: [
        probed('193.141.60.129:8443', [measured(BRIDGE_3, 100, false)]),
        probed('193.141.60.129:8449', [measured(BRIDGE_5, 0, true), measured([7, 'Ланд'], 0, true)]),
      ],
    })
    expect(rowOf('193.141.60.129:8443').textContent).toContain('1 из 1')

    mergePorts()

    expect(rowOf('193.141.60.129').textContent).toContain('1 из 3')
  })

  it('метки нет, пока таблица потерь не загружена или адреса в ней нет', () => {
    const view = overview(on('193.141.60.129:8443', [BRIDGE_3, BRIDGE_5]))
    const { unmount } = show(view)
    expect(rowOf('193.141.60.129:8443').textContent).not.toContain(' из ')
    unmount()

    show(view, { targets: [probed('10.0.0.1:443', [measured(BRIDGE_3, 100, false)])] })
    expect(rowOf('193.141.60.129:8443').textContent).not.toContain(' из ')
  })

  it('у адреса, который сейчас работает на всех релеях, метки нет', () => {
    show(overview([], {
      chronic: [{ target: '194.107.126.137:8443', ip: '194.107.126.137', port: 8443, owner: 'Аворо нода', strikes: 5 }],
    }), { targets: [probed('194.107.126.137:8443', [measured(BRIDGE_3, 0, true), measured(BRIDGE_5, 0, true)])] })

    expect(rowOf('194.107.126.137:8443').textContent).not.toContain(' из ')
  })

  it('у адреса, который не отключён, вернуть нечего', () => {
    show(overview(on('193.141.60.242:8449', [BRIDGE_3], refused)))

    fireEvent.click(within(rowOf('193.141.60.242:8449')).getByRole('button', { name: 'Действия' }))

    expect(screen.queryByRole('menuitem', { name: 'Вернуть' })).toBeNull()
    expect(screen.getByRole('menuitem', { name: 'Не отключать' })).toBeTruthy()
    expect(within(card('Тубо 2')).queryByRole('button', { name: /Вернуть все/ })).toBeNull()
  })

  it('адрес с постоянными проблемами виден и когда он сейчас в работе', () => {
    show(overview([], {
      chronic: [{ target: '194.107.126.137:8443', ip: '194.107.126.137', port: 8443, owner: 'Аворо нода', strikes: 5 }],
    }))

    const row = rowOf('194.107.126.137:8443').textContent
    expect(row).toContain('5 за сутки')
    expect(row).toContain('сейчас в работе — отключался 5 раз за сутки')
    expect(card('Аворо нода').textContent).toContain('1 адрес')
  })

  it('причины «потери» и «падения» названы с цифрами', () => {
    show(overview([
      item('194.107.126.139:8443', BRIDGE_3, { reason: { kind: 'downs', loss_pct: 0, downs: 3 } }),
      item('194.107.126.139:8443', BRIDGE_5, { reason: { kind: 'loss', loss_pct: 72, downs: 0 }, until: NOW - MINUTE }),
    ]))
    const row = rowOf('194.107.126.139:8443')
    expect(row.textContent).toContain('упал 3 раза за 30 мин · потери 72 %')

    expand('194.107.126.139:8443')

    expect(row.textContent).toContain('потери 72 % дольше 3 мин')
    expect(row.textContent).toContain('вернётся, когда уйдут потери')
  })

  it('в режиме наблюдения адреса показаны как «отключил бы» и возвращать нечего', () => {
    show(overview(
      on('193.141.60.121:8449', [BRIDGE_3], { state: 'would_disable', until: null, disabled_at: NOW - 5 * MINUTE }),
      { mode: 'observe' },
    ))

    expect(screen.getByRole('heading', { name: 'Отключил бы (режим наблюдения)' })).toBeTruthy()
    expect(card('Тубо 2').textContent).toContain('1 адрес · отключил бы 1')
    const row = rowOf('193.141.60.121:8449')
    expect(within(row).getByText('отключил бы')).toBeTruthy()
    fireEvent.click(within(row).getByRole('button', { name: 'Действия' }))
    expect(screen.queryByRole('menuitem', { name: 'Вернуть' })).toBeNull()
  })

  it('приостановка автоотключения на релее остаётся отдельной строкой', () => {
    show(overview([], { paused_relays: [{ server_id: 9, name: 'Ланд', since: NOW - 12 * MINUTE }] }))

    expect(screen.getByText('Ланд: автоотключение приостановлено 12 мин назад — похоже, проблема самого релея')).toBeTruthy()
  })

  it('в карточке видны первые десять адресов, остальные — по кнопке', () => {
    const many = Array.from({ length: 13 }, (_, index) => item(`10.0.0.${index + 1}:443`, BRIDGE_3))
    show(overview(many))
    expect(rowsOf('Тубо 2')).toHaveLength(10)

    fireEvent.click(within(card('Тубо 2')).getByRole('button', { name: 'Показать все 13' }))

    expect(rowsOf('Тубо 2')).toHaveLength(13)
  })

  it('пустой список: подпись при включённом автоотключении и ничего — при выключенном', () => {
    const { unmount } = show(overview([]))
    expect(screen.getByText('Сейчас ни один адрес не отключён.')).toBeTruthy()
    expect(screen.queryByRole('checkbox')).toBeNull()
    unmount()

    const { container } = show(overview([], { mode: 'off' }))
    expect(container.textContent).toBe('')
  })
})

describe('Потери: отключённые адреса по релеям', () => {
  const GROUP_BY_KEY = 'loss_quarantine_group_by'
  const byRelays = () => fireEvent.click(screen.getByRole('radio', { name: 'По релеям' }))
  const cardNames = () => screen.getAllByRole('region').map(region => region.getAttribute('aria-label'))
  const rowIn = (relay: string, address: string) =>
    within(card(relay)).getByText(address).closest('li') as HTMLElement

  beforeEach(() => {
    localStorage.clear()
    vi.spyOn(Date, 'now').mockReturnValue(NOW * 1000)
  })

  it('по умолчанию карточки — ноды, по переключателю — релеи со своими адресами', () => {
    show(overview([
      ...on('176.97.210.169:8449', [BRIDGE_3, BRIDGE_5], { owner: 'тубо' }),
      ...on('193.141.60.121:8449', [BRIDGE_3]),
    ]))
    expect(cardNames()).toEqual(['тубо', 'Тубо 2'])

    byRelays()

    expect(cardNames()).toEqual(['Вк мост 3', 'Вк мост 5'])
    expect(rowsOf('Вк мост 3')).toHaveLength(2)
    expect(rowsOf('Вк мост 5')).toHaveLength(1)
    expect(card('Вк мост 3').textContent).toContain('2 адреса · отключено 2')
  })

  it('в строке адреса — чья это нода, а не список релеев', () => {
    show(overview(on('176.97.210.169:8449', [BRIDGE_3, BRIDGE_5], { owner: 'тубо', sessions: 7 })))
    byRelays()

    const row = rowIn('Вк мост 3', '176.97.210.169:8449').textContent
    expect(row).toContain('тубо · не отвечает как клиент · 7 соед.')
    expect(row).not.toContain('Вк мост 5')
  })

  it('адрес не ноды панели подписан как чужой', () => {
    show(overview(on('203.0.113.7:443', [BRIDGE_3], { owner: null })))
    byRelays()

    expect(rowIn('Вк мост 3', '203.0.113.7:443').textContent).toContain('не нода панели · не отвечает как клиент')
  })

  it('выбор запоминается', () => {
    const view = overview(on('193.141.60.121:8449', [BRIDGE_3]))
    const { unmount } = show(view)
    byRelays()
    expect(localStorage.getItem(GROUP_BY_KEY)).toBe('relay')
    unmount()

    show(view)
    expect(cardNames()).toEqual(['Вк мост 3'])
    fireEvent.click(screen.getByRole('radio', { name: 'По серверам' }))
    expect(cardNames()).toEqual(['Тубо 2'])
    expect(localStorage.getItem(GROUP_BY_KEY)).toBe('node')
  })

  it('выше — релей, где отключено больше адресов, при равенстве — по имени', () => {
    show(overview([
      ...on('193.141.60.121:8449', [BRIDGE_5, BRIDGE_3, [9, 'Ланд']]),
      ...on('193.141.60.129:8449', [BRIDGE_5]),
    ]))
    byRelays()

    expect(cardNames()).toEqual(['Вк мост 5', 'Вк мост 3', 'Ланд'])
  })

  it('число в заголовке — адреса, а не пары «релей — адрес»', () => {
    show(overview([
      ...on('193.141.60.121:8449', [BRIDGE_3, BRIDGE_5]),
      ...on('193.141.60.129:8449', [BRIDGE_3]),
    ]))
    const title = () => screen.getByRole('heading', { name: 'Отключённые адреса' }).parentElement?.textContent
    expect(title()).toContain('Отключённые адреса2')

    byRelays()

    expect(title()).toContain('Отключённые адреса2')
  })

  it('«Вернуть» возвращает адрес только на релее этой карточки', async () => {
    const release = vi.spyOn(lossApi, 'releaseQuarantine').mockResolvedValue({ data: { released: true } } as never)
    const { onReleased } = show(overview(on('193.141.60.129:8443', [BRIDGE_3, BRIDGE_5])))
    byRelays()

    fireEvent.click(within(rowIn('Вк мост 5', '193.141.60.129:8443')).getByRole('button', { name: 'Действия' }))
    fireEvent.click(screen.getByRole('menuitem', { name: 'Вернуть' }))

    await waitFor(() => expect(onReleased).toHaveBeenCalledTimes(1))
    expect(release.mock.calls).toEqual([[5, '193.141.60.129:8443']])
  })

  it('«Вернуть все» возвращает всё отключённое на этом релее', async () => {
    const release = vi.spyOn(lossApi, 'releaseQuarantine').mockResolvedValue({ data: { released: true } } as never)
    const { onReleased } = show(overview([
      ...on('193.141.60.121:8449', [BRIDGE_3, BRIDGE_5]),
      ...on('193.141.60.129:8449', [BRIDGE_3]),
    ]))
    byRelays()

    fireEvent.click(releaseAllButton('Вк мост 3', 'Вернуть все 2'))
    fireEvent.click(releaseAllButton('Вк мост 3', 'Точно вернуть 2?'))

    await waitFor(() => expect(onReleased).toHaveBeenCalledTimes(1))
    expect(release.mock.calls).toEqual([[3, '193.141.60.121:8449'], [3, '193.141.60.129:8449']])
  })

  it('«Не различать порты» склеивает порты адреса и в карточке релея', () => {
    show(overview([
      ...on('193.141.60.129:8443', [BRIDGE_3]),
      ...on('193.141.60.129:8449', [BRIDGE_3]),
    ]))
    byRelays()
    expect(rowsOf('Вк мост 3')).toHaveLength(2)

    mergePorts()

    expect(rowsOf('Вк мост 3')).toHaveLength(1)
    expect(rowIn('Вк мост 3', '193.141.60.129').textContent).toContain(':8443, :8449')
  })

  it('адрес, который сейчас в работе, в карточки релеев не попадает — об этом сказано', () => {
    show(overview([], {
      chronic: [{ target: '194.107.126.137:8443', ip: '194.107.126.137', port: 8443, owner: 'Аворо нода', strikes: 5 }],
    }))
    byRelays()

    expect(screen.queryAllByRole('region')).toHaveLength(0)
    expect(screen.getByText(/На релеях сейчас ничего не отключено/)).toBeTruthy()
  })

  it('карточек показано не больше тридцати, остальные — по кнопке', () => {
    show(overview(on('193.141.60.129:8443', relays(34))))
    byRelays()
    expect(screen.getAllByRole('region')).toHaveLength(30)

    fireEvent.click(screen.getByRole('button', { name: 'Показать все 34' }))

    expect(screen.getAllByRole('region')).toHaveLength(34)
  })
})

describe('Потери: насколько широко адрес не работает', () => {
  it('«везде» — только когда релеев больше одного и не работает на всех', () => {
    expect(spreadLevel({ failing: 12, total: 12 })).toBe('all')
    expect(spreadLevel({ failing: 2, total: 2 })).toBe('all')
    // Единственный релей — одна точка зрения: по ней о бане везде судить нельзя
    expect(spreadLevel({ failing: 1, total: 1 })).toBe('limited')
  })

  it('«большинство» — от половины релеев, но не один', () => {
    expect(spreadLevel({ failing: 6, total: 12 })).toBe('most')
    expect(spreadLevel({ failing: 11, total: 12 })).toBe('most')
    expect(spreadLevel({ failing: 5, total: 12 })).toBe('limited')
    expect(spreadLevel({ failing: 1, total: 2 })).toBe('limited')
  })
})
