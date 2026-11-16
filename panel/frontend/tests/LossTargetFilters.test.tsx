import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'
import Loss from '../src/pages/Loss'
import { lossApi, serversApi, type LossRelay, type LossTarget } from '../src/api/client'

vi.mock('../src/components/loss/LossSettings', () => ({ default: () => null }))
vi.mock('../src/components/ssh/ServerSelector', () => ({ ServerSelector: () => null }))
// Вместо окна правки — список адресов, с которыми страница его открыла
vi.mock('../src/components/loss/BatchEditModal', () => ({
  default: ({ targets }: { targets: LossTarget[] }) => (
    <ul aria-label="в правку">{targets.map(target => <li key={target.target}>{target.target}</li>)}</ul>
  ),
}))

// Сервер принимает подключение, а на проверку «как клиент» не отвечает
const SILENT = 'молчит'
type Reading = number | typeof SILENT

function reading(loss: Reading, index: number): LossRelay {
  const silent = loss === SILENT
  return {
    server_id: index + 1, name: `релей ${index + 1}`, rtt_ms: 40, samples: 60,
    loss_pct: silent ? 100 : loss, answers: silent ? false : null,
  }
}

// Строка таблицы: что видит каждый релей, где стоит адрес; худший — первым, как отдаёт панель
function address(target: string, owner: string | null, losses: Reading[], over: Partial<LossTarget> = {}): LossTarget {
  const [ip, port] = target.split(':')
  const relays = losses.map(reading).sort((left, right) => right.loss_pct - left.loss_pct)
  return {
    target, ip, port: Number(port), owner, total_only: false, relays,
    worst_loss: relays[0].loss_pct, episode: null, quarantined: [], ...over,
  }
}

const FLEET = [
  address('10.0.0.1:443', 'тубо', [SILENT, SILENT, 0, 0, 0, 0]),
  address('10.0.0.2:443', 'тубо', [SILENT, SILENT, SILENT, SILENT, 0, 0], { quarantined: [1, 2] }),
  address('10.0.0.3:443', 'Тубо 2', [SILENT, SILENT, SILENT, SILENT, SILENT, SILENT], { quarantined: [1, 2, 3] }),
  address('10.0.0.4:443', 'Тубо 2', [30, 30, 30, 30, 30, 30]),
  address('10.0.0.5:443', 'Тубо 2', [12, 0, 0, 0, 0, 0]),
  address('10.0.0.6:443', null, [0, 0, 0, 0, 0, 0]),
]

// Тесту нужны только data: остальные поля ответа axios страница не читает
const respond = <T,>(data: T) => Promise.resolve({ data }) as never

async function open(targets: LossTarget[] = FLEET) {
  vi.spyOn(lossApi, 'overview').mockImplementation(() => respond({ targets }))
  vi.spyOn(lossApi, 'quarantine').mockImplementation(() => respond({ mode: 'off', items: [], chronic: [], paused_relays: [] }))
  vi.spyOn(lossApi, 'settings').mockImplementation(() => new Promise(() => {}) as never)
  vi.spyOn(lossApi, 'jobs').mockImplementation(() => respond({ jobs: [] }))
  vi.spyOn(serversApi, 'list').mockImplementation(() => respond({ servers: [] }))
  render(<Loss />)
  await screen.findByRole('table')
}

const ADDRESS = /^\d+\.\d+\.\d+\.\d+:\d+$/
const addresses = () => screen.queryAllByText(ADDRESS).map(cell => cell.textContent)

const STATE = 'Худшие потери'
const SPREAD = 'Релеи с потерями'
const QUARANTINE = 'Автоотключение'
const NODE = 'Нода'

const group = (name: string) => screen.getByRole('group', { name })
const chipsOf = (name: string) => within(group(name)).getAllByRole('button') as HTMLButtonElement[]
// Подпись кнопки — название и число адресов: «Тубо 2 3»
const chip = (groupName: string, label: string) =>
  within(group(groupName)).getByRole('button', { name: new RegExp(`^${label} \\d+$`) }) as HTMLButtonElement
const countOn = (button: HTMLElement) => Number(button.textContent?.match(/(\d+)$/)?.[1])
const press = (groupName: string, label: string) => fireEvent.click(chip(groupName, label))
const showAllAddresses = () => fireEvent.click(screen.getByLabelText(/Показать все адреса/))
const tick = (target: string) =>
  fireEvent.click(within(screen.getByText(target).closest('tr') as HTMLElement).getByRole('checkbox'))
const tickAll = () => fireEvent.click(within(screen.getAllByRole('row')[0]).getByRole('checkbox'))
const sentToEdit = () =>
  within(screen.getByRole('list', { name: 'в правку' })).getAllByRole('listitem').map(entry => entry.textContent)

// Парк, где нод-владельцев больше, чем помещается в ряд кнопок
const crowdOwners = Array.from({ length: 11 }, (_, index) => `нода ${String(index + 1).padStart(2, '0')}`)
const crowd = () => crowdOwners.map((owner, index) => address(`10.0.1.${index + 1}:443`, owner, [SILENT, 0]))

describe('Потери: фильтры таблицы адресов', () => {
  it('кнопка ноды оставляет только её адреса, повторное нажатие возвращает все', async () => {
    await open()
    expect(addresses()).toEqual(['10.0.0.1:443', '10.0.0.2:443', '10.0.0.3:443', '10.0.0.4:443', '10.0.0.5:443'])

    press(NODE, 'Тубо 2')
    expect(addresses()).toEqual(['10.0.0.3:443', '10.0.0.4:443', '10.0.0.5:443'])
    expect(chip(NODE, 'Тубо 2').getAttribute('aria-pressed')).toBe('true')

    press(NODE, 'Тубо 2')
    expect(addresses()).toHaveLength(5)
    expect(chip(NODE, 'Тубо 2').getAttribute('aria-pressed')).toBe('false')
  })

  it('фильтры разных групп сужают список, кнопки одной группы — расширяют', async () => {
    await open()

    press(NODE, 'Тубо 2')
    press(SPREAD, 'везде')
    expect(addresses()).toEqual(['10.0.0.3:443', '10.0.0.4:443'])

    press(STATE, 'потери')
    expect(addresses()).toEqual(['10.0.0.4:443'])

    press(STATE, 'не отвечает')
    expect(addresses()).toEqual(['10.0.0.3:443', '10.0.0.4:443'])
  })

  it('по ширине: везде, на многих релеях и на некоторых', async () => {
    await open()

    press(SPREAD, 'на многих релеях')
    expect(addresses()).toEqual(['10.0.0.2:443'])

    press(SPREAD, 'на многих релеях')
    press(SPREAD, 'на некоторых релеях')
    expect(addresses()).toEqual(['10.0.0.1:443', '10.0.0.5:443'])
  })

  it('адрес на единственном релее в «везде» не попадает — это одна точка зрения', async () => {
    await open([
      address('10.0.0.1:443', 'тубо', [SILENT]),
      address('10.0.0.2:443', 'тубо', [SILENT, SILENT, SILENT]),
    ])

    press(SPREAD, 'везде')

    expect(addresses()).toEqual(['10.0.0.2:443'])
  })

  it('отключённые автоотключением и не отключённые', async () => {
    await open()

    press(QUARANTINE, 'отключён')
    expect(addresses()).toEqual(['10.0.0.2:443', '10.0.0.3:443'])

    press(QUARANTINE, 'отключён')
    press(QUARANTINE, 'не отключён')
    expect(addresses()).toEqual(['10.0.0.1:443', '10.0.0.4:443', '10.0.0.5:443'])
  })

  it('число на кнопке — сколько адресов она даст при остальных фильтрах', async () => {
    await open()
    expect(countOn(chip(SPREAD, 'везде'))).toBe(2)
    expect(countOn(chip(NODE, 'тубо'))).toBe(2)
    expect(countOn(chip(NODE, 'Тубо 2'))).toBe(3)

    press(NODE, 'тубо')

    expect(countOn(chip(SPREAD, 'на многих релеях'))).toBe(1)
    expect(countOn(chip(SPREAD, 'на некоторых релеях'))).toBe(1)
    // Соседняя нода остаётся выбором, а не обнуляется: кнопки одной группы складываются
    expect(countOn(chip(NODE, 'Тубо 2'))).toBe(3)
  })

  it('кнопка, которая ничего не даст, остаётся на месте, но не нажимается', async () => {
    await open()

    press(NODE, 'тубо')

    const everywhere = chip(SPREAD, 'везде')
    expect(countOn(everywhere)).toBe(0)
    expect(everywhere.disabled).toBe(true)
  })

  it('группа из одного значения не показывается', async () => {
    await open([
      address('10.0.0.1:443', 'тубо', [SILENT, SILENT, 0, 0, 0, 0]),
      address('10.0.0.2:443', 'тубо', [SILENT, SILENT, SILENT, SILENT, SILENT, SILENT]),
    ])

    expect(chipsOf(SPREAD)).toHaveLength(2)
    expect(screen.queryByRole('group', { name: STATE })).toBeNull()
    expect(screen.queryByRole('group', { name: QUARANTINE })).toBeNull()
    expect(screen.queryByRole('group', { name: NODE })).toBeNull()
  })

  it('выбирать не из чего — полосы фильтров нет', async () => {
    await open([
      address('10.0.0.1:443', 'тубо', [SILENT, SILENT, 0, 0, 0, 0]),
      address('10.0.0.2:443', 'тубо', [SILENT, SILENT, 0, 0, 0, 0]),
    ])

    expect(screen.queryByRole('group', { name: 'Фильтры' })).toBeNull()
  })

  it('«выбрать все» отмечает только отобранные адреса', async () => {
    await open()
    press(NODE, 'Тубо 2')

    tickAll()

    expect(screen.getByText('Выбрано адресов: 3')).toBeTruthy()
  })

  it('отмеченный адрес, скрытый фильтром, в правку не уходит', async () => {
    await open()
    tick('10.0.0.1:443')
    tick('10.0.0.3:443')
    expect(screen.getByText('Выбрано адресов: 2')).toBeTruthy()

    press(NODE, 'Тубо 2')
    expect(screen.getByText('Выбрано адресов: 1')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Удалить бэкенды' }))
    expect(sentToEdit()).toEqual(['10.0.0.3:443'])
  })

  it('«выбрать все» под фильтром не трогает отмеченное вне его', async () => {
    await open()
    tick('10.0.0.1:443')
    press(NODE, 'Тубо 2')
    expect(screen.queryByText(/^Выбрано адресов/)).toBeNull()

    tickAll()
    expect(screen.getByText('Выбрано адресов: 3')).toBeTruthy()
    tickAll()
    expect(screen.queryByText(/^Выбрано адресов/)).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Сбросить' }))
    expect(screen.getByText('Выбрано адресов: 1')).toBeTruthy()
  })

  it('рядом со сбросом — сколько адресов показано; сброс снимает все фильтры', async () => {
    await open()
    press(NODE, 'Тубо 2')
    press(SPREAD, 'везде')
    expect(screen.getByText('Показано 2 из 5')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Сбросить' }))

    expect(addresses()).toHaveLength(5)
    expect(screen.queryByRole('button', { pressed: true })).toBeNull()
    expect(screen.queryByText(/^Показано/)).toBeNull()
  })

  it('фильтр, под который адресов не осталось, виден и снимается', async () => {
    await open()
    showAllAddresses()
    press(STATE, 'без потерь')
    expect(addresses()).toEqual(['10.0.0.6:443'])

    showAllAddresses()

    expect(addresses()).toEqual([])
    expect(screen.getByText('Под фильтры не подходит ни один адрес')).toBeTruthy()
    expect(chip(STATE, 'без потерь').getAttribute('aria-pressed')).toBe('true')

    press(STATE, 'без потерь')
    expect(addresses()).toHaveLength(5)
    expect(within(group(STATE)).queryByRole('button', { name: /^без потерь/ })).toBeNull()
  })

  it('адрес без ноды панели отбирается своей кнопкой', async () => {
    await open()
    showAllAddresses()

    press(NODE, 'не нода панели')

    expect(addresses()).toEqual(['10.0.0.6:443'])
  })

  it('нод много — в ряду первые восемь, остальные под «ещё N»; выбранная не прячется', async () => {
    await open(crowd())
    expect(chipsOf(NODE).map(button => button.textContent)).toEqual([
      ...crowdOwners.slice(0, 8).map(owner => `${owner} 1`), 'ещё 3',
    ])

    fireEvent.click(within(group(NODE)).getByRole('button', { name: 'ещё 3' }))
    expect(chipsOf(NODE)).toHaveLength(12)

    press(NODE, 'нода 11')
    fireEvent.click(within(group(NODE)).getByRole('button', { name: 'Свернуть' }))

    expect(chipsOf(NODE).map(button => button.textContent)).toEqual([
      ...crowdOwners.slice(0, 8).map(owner => `${owner} 1`), 'нода 11 1', 'ещё 2',
    ])
  })

  it('раскрытие ряда нод не уводит фокус с кнопки — с клавиатуры ряд можно свернуть обратно', async () => {
    await open(crowd())
    const more = within(group(NODE)).getByRole('button', { name: 'ещё 3' })
    more.focus()

    fireEvent.click(more)

    expect(document.activeElement?.textContent).toBe('Свернуть')
  })
})
