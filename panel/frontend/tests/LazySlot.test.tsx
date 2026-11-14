import { describe, expect, it, vi } from 'vitest'
import { act, render, screen } from '@testing-library/react'
import { LazySlot, type CardPlaceholder } from '../src/components/Dashboard/LazyServerCard'
import { FakeIntersectionObserver } from './helpers'

vi.stubGlobal('IntersectionObserver', FakeIntersectionObserver)

const placeholder: CardPlaceholder = { layoutKey: 'test', height: 120 }

function renderSlot(props: { holdMounted?: boolean; dragged?: boolean } = {}) {
  const tree = (holdMounted: boolean) => (
    <LazySlot placeholder={placeholder} holdMounted={holdMounted} dragged={props.dragged ?? false}>
      <span>card</span>
    </LazySlot>
  )
  const view = render(tree(props.holdMounted ?? false))
  const slot = view.container.firstElementChild as HTMLElement
  const move = (isNear: boolean) => act(() => FakeIntersectionObserver.moveSlots([slot], isNear))
  return { slot, move, setHoldMounted: (holdMounted: boolean) => view.rerender(tree(holdMounted)) }
}

describe('LazySlot', () => {
  it('вдали от экрана держит место заглушкой, рядом — монтирует содержимое', () => {
    const { slot, move } = renderSlot()
    expect(screen.queryByText('card')).toBeNull()
    expect(slot.style.height).toBe('120px')

    move(true)
    expect(screen.getByText('card')).toBeTruthy()
    expect(slot.style.height).toBe('')

    move(false)
    expect(screen.queryByText('card')).toBeNull()
  })

  it('пока идёт перетаскивание, смонтированное не убирает', () => {
    const { move, setHoldMounted } = renderSlot({ holdMounted: true })
    move(true)

    move(false)
    expect(screen.getByText('card')).toBeTruthy()

    setHoldMounted(false)
    move(false)
    expect(screen.queryByText('card')).toBeNull()
  })

  it('перетаскиваемую карточку монтирует сразу, не дожидаясь наблюдателя', () => {
    renderSlot({ dragged: true })

    expect(screen.getByText('card')).toBeTruthy()
  })
})
