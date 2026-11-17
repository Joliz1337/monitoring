import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import QuarantineSettings, { isQuarantineValid } from '../src/components/loss/QuarantineSettings'
import type { QuarantineSettingsData } from '../src/api/client'

const SETTINGS: QuarantineSettingsData = {
  mode: 'enforce', loss_threshold: 50, sustained_seconds: 180, downs_threshold: 3, calm_seconds: 600,
  chronic_strikes: 4, max_share_pct: 50, drop_sessions: true, notify: true, notify_scope: 'all',
  notify_dead_share_pct: 70, client_probe: false, exempt_server_ids: [], exempt_targets: [],
}
const ALL = 'Все события'
const DEAD = 'Только умершие адреса'
const OFF = 'Выключены'
const SHARE = 'Адрес умер — отключён на такой доле релеев, %'

function show(over: Partial<QuarantineSettingsData> = {}) {
  const onChange = vi.fn<(next: QuarantineSettingsData) => void>()
  render(<QuarantineSettings servers={[]} value={{ ...SETTINGS, ...over }} onChange={onChange} />)
  return onChange
}

const pressed = (name: string) => screen.getByRole('button', { name }).getAttribute('aria-pressed')
const press = (name: string) => fireEvent.click(screen.getByRole('button', { name }))
const lastChange = (onChange: ReturnType<typeof show>) => onChange.mock.calls[onChange.mock.calls.length - 1][0]

describe('настройки автоотключения: о чём писать в Telegram', () => {
  it('из трёх режимов нажат сохранённый', () => {
    show({ notify_scope: 'dead' })

    expect([pressed(ALL), pressed(DEAD), pressed(OFF)]).toEqual(['false', 'true', 'false'])
    expect((screen.getByLabelText(SHARE) as HTMLInputElement).value).toBe('70')
  })

  it('выключенные уведомления — нажато «Выключены», порога не видно', () => {
    show({ notify: false, notify_scope: 'dead' })

    expect([pressed(ALL), pressed(DEAD), pressed(OFF)]).toEqual(['false', 'false', 'true'])
    expect(screen.queryByLabelText(SHARE)).toBeNull()
  })

  it('в режиме «Все события» порога не видно', () => {
    show()

    expect(pressed(ALL)).toBe('true')
    expect(screen.queryByLabelText(SHARE)).toBeNull()
  })

  it('«Только умершие адреса» включает уведомления вместе с режимом', () => {
    const onChange = show({ notify: false })

    press(DEAD)

    expect(lastChange(onChange)).toMatchObject({ notify: true, notify_scope: 'dead' })
  })

  it('«Все события» возвращает прежние сводки', () => {
    const onChange = show({ notify_scope: 'dead' })

    press(ALL)

    expect(lastChange(onChange)).toMatchObject({ notify: true, notify_scope: 'all' })
  })

  it('«Выключены» режим не трогает — при включении он вернётся', () => {
    const onChange = show({ notify_scope: 'dead' })

    press(OFF)

    expect(lastChange(onChange)).toMatchObject({ notify: false, notify_scope: 'dead' })
  })

  it('порог уходит числом, пустое поле — не числом', () => {
    const onChange = show({ notify_scope: 'dead' })

    fireEvent.change(screen.getByLabelText(SHARE), { target: { value: '85' } })
    expect(lastChange(onChange).notify_dead_share_pct).toBe(85)

    fireEvent.change(screen.getByLabelText(SHARE), { target: { value: '' } })
    expect(lastChange(onChange).notify_dead_share_pct).toBeNaN()
  })

  it('порог вне 10–100 сохранить нельзя', () => {
    const dead = (share: number) => isQuarantineValid({ ...SETTINGS, notify_scope: 'dead', notify_dead_share_pct: share })

    expect([10, 70, 100].map(dead)).toEqual([true, true, true])
    expect([9, 101, 70.5, NaN].map(dead)).toEqual([false, false, false, false])
  })

  it('порог, которого не видно, сохранению не мешает', () => {
    expect(isQuarantineValid({ ...SETTINGS, notify_dead_share_pct: NaN })).toBe(true)
    expect(isQuarantineValid({ ...SETTINGS, notify: false, notify_scope: 'dead', notify_dead_share_pct: NaN })).toBe(true)
  })
})
