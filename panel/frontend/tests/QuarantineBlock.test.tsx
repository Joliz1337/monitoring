import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import QuarantineBlock from '../src/components/loss/QuarantineBlock'
import { lossApi, type QuarantineItem, type QuarantineOverview } from '../src/api/client'

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

function show(view: QuarantineOverview) {
  const handlers = { onReleased: vi.fn(), onExempt: vi.fn(), onEdit: vi.fn() }
  const rendered = render(<QuarantineBlock view={view} sustainedSeconds={180} canExempt {...handlers} />)
  return { ...handlers, ...rendered }
}

const card = (owner: string) => screen.getByRole('region', { name: owner })
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
