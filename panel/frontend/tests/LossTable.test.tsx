import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
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
const header = (name: string) => screen.getByRole('columnheader', { name })
const sortBy = (name: string) => fireEvent.click(screen.getByRole('button', { name }))

describe('Потери: таблица адресов упорядочивается нажатием на заголовок столбца', () => {
  it('порядок по умолчанию — как отдала панель, столбец не выбран', async () => {
    await open()

    expect(addresses()).toEqual(['10.0.0.1:443', '10.0.0.2:443', '10.0.0.3:443', '10.0.0.4:443', '10.0.0.5:443'])
    expect(header('Адрес').getAttribute('aria-sort')).toBe('none')
  })

  it('«Релеи с потерями» — сверху адрес, который теряют больше релеев; повторное нажатие — наоборот', async () => {
    await open()

    sortBy('Релеи с потерями')
    expect(addresses()).toEqual(['10.0.0.3:443', '10.0.0.4:443', '10.0.0.2:443', '10.0.0.1:443', '10.0.0.5:443'])
    expect(header('Релеи с потерями').getAttribute('aria-sort')).toBe('descending')

    sortBy('Релеи с потерями')
    expect(addresses()).toEqual(['10.0.0.5:443', '10.0.0.1:443', '10.0.0.2:443', '10.0.0.3:443', '10.0.0.4:443'])
    expect(header('Релеи с потерями').getAttribute('aria-sort')).toBe('ascending')
  })

  it('«Адрес» — по возрастанию, затем по убыванию; прежний столбец отпускается', async () => {
    await open()
    sortBy('Худшие потери')

    sortBy('Адрес')
    expect(addresses()).toEqual(['10.0.0.1:443', '10.0.0.2:443', '10.0.0.3:443', '10.0.0.4:443', '10.0.0.5:443'])
    expect([header('Адрес').getAttribute('aria-sort'), header('Худшие потери').getAttribute('aria-sort')]).toEqual(['ascending', 'none'])

    sortBy('Адрес')
    expect(addresses()[0]).toBe('10.0.0.5:443')
  })

  it('отдельных кнопок-фильтров над таблицей нет', async () => {
    await open()

    expect(screen.queryByRole('button', { name: /^не отвечает/ })).toBeNull()
    expect(screen.queryByText('Сбросить')).toBeNull()
  })

  it('в раскрытом адресе у релея есть разбивка по его IP', async () => {
    await open()
    expect(screen.queryByText('по IP релея')).toBeNull()

    fireEvent.click(screen.getByText('10.0.0.1:443'))

    expect(screen.getAllByText('по IP релея').length).toBeGreaterThan(0)
  })
})
