import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import LinkServerPicker from '../src/components/haproxy/LinkServerPicker'
import type { HAProxyAvailableServer } from '../src/api/client'

const THIS_PROFILE = 1
const OTHER_PROFILE = 2
const PROFILE_NAMES = new Map([[THIS_PROFILE, 'Европа'], [OTHER_PROFILE, 'Азия']])

const SHOW_LINKED = /Показывать уже привязанные/
const WARNING = /уйдёт из своего профиля/

function server(id: number, name: string, activeProfileId: number | null = null, folder: string | null = null): HAProxyAvailableServer {
  return { id, name, url: `https://10.0.0.${id}:9100`, active_profile_id: activeProfileId, sync_status: null, folder }
}

const FREE = server(1, 'свободный')
const TAKEN = server(2, 'занятый', OTHER_PROFILE)
const OWN = server(3, 'свой', THIS_PROFILE)

function show(servers: HAProxyAvailableServer[] = [FREE, TAKEN, OWN], profileNames = PROFILE_NAMES) {
  const onLink = vi.fn<(serverId: number) => void>()
  render(<LinkServerPicker servers={servers} profileId={THIS_PROFILE} profileNames={profileNames} onLink={onLink} />)
  return onLink
}

const showLinked = () => screen.getByRole<HTMLInputElement>('checkbox', { name: SHOW_LINKED })
const row = (name: string) => screen.queryByRole('button', { name: new RegExp(name) })
const pick = (name: string) => fireEvent.click(row(name)!)
const answerConfirm = (answer: boolean) => vi.spyOn(window, 'confirm').mockReturnValue(answer)

describe('выбор сервера для привязки к профилю HAProxy', () => {
  it('сначала показывает только свободные серверы', () => {
    show()

    expect(showLinked().checked).toBe(false)
    expect(row('свободный')).not.toBeNull()
    expect(row('занятый')).toBeNull()
    expect(row('свой')).toBeNull()
  })

  it('галочка добавляет серверы других профилей с названием профиля, свои остаются скрыты', () => {
    show()

    fireEvent.click(showLinked())

    expect(row('свободный')).not.toBeNull()
    expect(row('занятый')!.textContent).toContain('в профиле «Азия»')
    expect(row('свой')).toBeNull()
  })

  it('рядом с галочкой стоит число скрытых серверов', () => {
    show([FREE, TAKEN, server(4, 'занятый-2', OTHER_PROFILE), OWN])

    expect(showLinked().closest('label')!.textContent).toContain('2')
  })

  it('предупреждение о переносе видно только при включённой галочке', () => {
    show()
    expect(screen.queryByText(WARNING)).toBeNull()

    fireEvent.click(showLinked())

    expect(screen.queryByText(WARNING)).not.toBeNull()
  })

  it('свободный сервер привязывается без вопросов', () => {
    const confirm = answerConfirm(true)
    const onLink = show()

    pick('свободный')

    expect(confirm).not.toHaveBeenCalled()
    expect(onLink).toHaveBeenCalledWith(FREE.id)
  })

  it('перед переносом занятого сервера спрашивает, называя сервер и его профиль', () => {
    const confirm = answerConfirm(true)
    const onLink = show()
    fireEvent.click(showLinked())

    pick('занятый')

    const question = String(confirm.mock.calls[0][0])
    expect(question).toContain('«занятый»')
    expect(question).toContain('«Азия»')
    expect(onLink).toHaveBeenCalledWith(TAKEN.id)
  })

  it('отказ в вопросе оставляет сервер в прежнем профиле', () => {
    answerConfirm(false)
    const onLink = show()
    fireEvent.click(showLinked())

    pick('занятый')

    expect(onLink).not.toHaveBeenCalled()
  })

  it('профиль, которого нет в списке, назван номером', () => {
    show([TAKEN], new Map())

    fireEvent.click(showLinked())

    expect(row('занятый')!.textContent).toContain('в профиле «#2»')
  })

  it('без серверов в других профилях галочку нажать нельзя', () => {
    show([FREE, OWN])

    expect(showLinked().disabled).toBe(true)
  })

  it('когда свободных нет, подсказывает включить галочку', () => {
    show([TAKEN, OWN])

    expect(screen.queryByText(/Свободных серверов нет/)).not.toBeNull()

    fireEvent.click(showLinked())

    expect(screen.queryByText(/Свободных серверов нет/)).toBeNull()
    expect(row('занятый')).not.toBeNull()
  })

  it('занятый сервер в папке виден после раскрытия папки', () => {
    show([server(5, 'в-папке', OTHER_PROFILE, 'Мосты')])
    fireEvent.click(showLinked())

    fireEvent.click(screen.getByText('Мосты'))

    expect(row('в-папке')!.textContent).toContain('в профиле «Азия»')
  })
})
