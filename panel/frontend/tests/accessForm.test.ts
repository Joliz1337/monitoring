import { describe, expect, it } from 'vitest'
import {
  ACCESS_FORM_BLANK, isAccessFormDirty, toAccessChange, type AccessFormValue,
} from '../src/components/sshAccess/accessForm'

const form = (patch: Partial<AccessFormValue['creds']> = {}, rest: Partial<AccessFormValue> = {}): AccessFormValue => ({
  ...ACCESS_FORM_BLANK,
  ...rest,
  creds: { ...ACCESS_FORM_BLANK.creds, ...patch },
})

describe('форма SSH-доступа: что уходит на сохранение', () => {
  it('шлёт только заполненные поля — пустое не меняет заданное', () => {
    expect(toAccessChange(form({ password: 'pw' }))).toEqual({ ssh_password: 'pw' })
    expect(toAccessChange(form({ port: '2222' }))).toEqual({ ssh_port: 2222 })
    expect(toAccessChange(ACCESS_FORM_BLANK)).toEqual({})
  })

  it('пароль уходит как введён, пользователь — без пробелов по краям', () => {
    expect(toAccessChange(form({ user: ' root ', password: '  p:a@ss  ' }))).toEqual({
      ssh_user: 'root', ssh_password: '  p:a@ss  ',
    })
  })

  it('при входе по ключу пароль из соседнего поля не уходит', () => {
    const change = toAccessChange(form({ authMethod: 'key', password: 'stale', privateKey: 'KEY', passphrase: 'pp' }))

    expect(change).toEqual({ ssh_private_key: 'KEY', ssh_passphrase: 'pp' })
  })

  it('адрес прокси уходит вместе с режимом', () => {
    expect(toAccessChange(form({}, { proxy: ' 1.2.3.4:1080 ', proxyMode: 'always' }))).toEqual({
      ssh_proxy: '1.2.3.4:1080', ssh_proxy_mode: 'always',
    })
  })

  it('«без прокси» уходит режимом без адреса', () => {
    expect(toAccessChange(form({}, { proxy: '1.2.3.4:1080', proxyMode: 'off' }))).toEqual({ ssh_proxy_mode: 'off' })
  })

  it('режим без адреса ничего не меняет', () => {
    expect(toAccessChange(form({}, { proxyMode: 'always' }))).toEqual({})
  })

  it('ключ из одних пробелов и переводов строк не уходит', () => {
    expect(toAccessChange(form({ authMethod: 'key', privateKey: '  \n ' }))).toEqual({})
  })

  it('нетронутая форма не считается изменённой', () => {
    expect(isAccessFormDirty(ACCESS_FORM_BLANK)).toBe(false)
    expect(isAccessFormDirty(form({}, { proxyMode: 'always' }))).toBe(false)
    expect(isAccessFormDirty(form({ user: 'root' }))).toBe(true)
    expect(isAccessFormDirty(form({}, { proxyMode: 'off' }))).toBe(true)
  })
})
