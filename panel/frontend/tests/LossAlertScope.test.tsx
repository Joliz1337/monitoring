import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import LossScopeChoice from '../src/components/loss/LossScopeChoice'
import type { PacketLossScope } from '../src/api/client'

const ALL = 'Все события'
const WIDE = 'Без одиночных'
const DEAD = 'Только умершие адреса'
const SHARE_SLIDER = 'ползунок доли'

function show(scope: PacketLossScope = 'all', share = 50) {
  const onChange = vi.fn<(next: PacketLossScope) => void>()
  render(
    <LossScopeChoice scope={scope} share={share} onChange={onChange}>
      <div>{SHARE_SLIDER}</div>
    </LossScopeChoice>,
  )
  return onChange
}

const checked = (name: string) => screen.getByRole('radio', { name }).getAttribute('aria-checked')
const choose = (name: string) => fireEvent.click(screen.getByRole('radio', { name }))

describe('алерт «Потери с релеев»: уровень сообщений', () => {
  it('из трёх уровней отмечен сохранённый', () => {
    show('wide')

    expect([checked(ALL), checked(WIDE), checked(DEAD)]).toEqual(['false', 'true', 'false'])
  })

  it('нажатый уровень уходит на сохранение', () => {
    const onChange = show()

    choose(DEAD)

    expect(onChange).toHaveBeenCalledWith('dead')
  })

  it('повторное нажатие выбранного уровня ничего не сохраняет', () => {
    const onChange = show('wide')

    choose(WIDE)

    expect(onChange).not.toHaveBeenCalled()
  })

  it('на уровне «Все события» доля ничего не решает — ползунка нет', () => {
    show('all')

    expect(screen.queryByText(SHARE_SLIDER)).toBeNull()
    expect(screen.getByText(/даже если его теряет один релей/)).toBeTruthy()
  })

  it('«Без одиночных»: виден ползунок, подсказка называет долю', () => {
    show('wide', 70)

    expect(screen.getByText(SHARE_SLIDER)).toBeTruthy()
    expect(screen.getByText(/не меньше 70% его релеев/)).toBeTruthy()
  })

  it('«Только умершие адреса»: виден ползунок, подсказка говорит про почти не отвечающий адрес', () => {
    show('dead', 60)

    expect(screen.getByText(SHARE_SLIDER)).toBeTruthy()
    expect(screen.getByText(/почти не отвечает.*60% своих релеев/)).toBeTruthy()
  })
})
