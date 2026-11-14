import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { BackupCard } from '../src/components/settings/BackupCard'
import { backupApi, type BackupInfo } from '../src/api/client'

const PLAIN_WARNING = /Локальные бэкапы не шифруются/
const ENCRYPTED_NOTE = /Новые бэкапы шифруются паролем архива/
const CONFIRM_QUESTION = /Все текущие данные будут заменены/

// Тесту нужны только data: остальные поля ответа axios карточка не читает
const respond = <T,>(data: T) => Promise.resolve({ data }) as never

const backup = (filename: string, encrypted: boolean): BackupInfo => ({
  filename, encrypted, size: 2048, created_at: '2026-11-14T04:00:00Z', version: '10.80.0',
})

// Зашифрованный бэкап начинается с сигнатуры контейнера, открытый — с заголовка .env или дампа
const encryptedFile = (name: string) => new File(['MTGB1\n', 'salt-nonce-ciphertext'], name)
const plainFile = (name: string) => new File(['MONBKP2\n', '34\nPANEL_ENC_KEY=…PGDMP'], name)

function mockApi(backups: BackupInfo[] = []) {
  vi.spyOn(backupApi, 'list').mockImplementation(() => respond({ backups, count: backups.length }))
  vi.spyOn(backupApi, 'getStatus').mockImplementation(() => respond({
    state: 'idle', operation: null, filename: null, error: null, started_at: null, completed_at: null,
  }))
  return vi.spyOn(backupApi, 'restore').mockImplementation(() => respond({ success: true, message: 'Restore started' }))
}

async function renderCard(archivePasswordSet: boolean | null) {
  const view = render(<BackupCard archivePasswordSet={archivePasswordSet} />)
  await waitFor(() => expect(backupApi.list).toHaveBeenCalled())
  return view
}

async function chooseForRestore(container: HTMLElement, file: File) {
  fireEvent.change(container.querySelector('input[type="file"]') as HTMLInputElement, { target: { files: [file] } })
  await screen.findByText(CONFIRM_QUESTION)
}

const restoreButton = () => screen.getByRole('button', { name: 'Восстановить' }) as HTMLButtonElement
const passwordField = () => screen.queryByPlaceholderText('Пароль архива') as HTMLInputElement | null

describe('BackupCard: шифрование локальных бэкапов', () => {
  beforeEach(() => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
  })

  it('без пароля архива прямо говорит, что бэкапы не шифруются', async () => {
    mockApi()
    await renderCard(false)

    expect(screen.getByText(PLAIN_WARNING)).toBeTruthy()
    expect(screen.queryByText(ENCRYPTED_NOTE)).toBeNull()
  })

  it('с паролем архива говорит, что бэкапы шифруются и пароль надо хранить', async () => {
    mockApi()
    await renderCard(true)

    expect(screen.getByText(ENCRYPTED_NOTE)).toBeTruthy()
    expect(screen.queryByText(PLAIN_WARNING)).toBeNull()
  })

  it('пока настройки не пришли, о шифровании не говорит ничего', async () => {
    mockApi()
    await renderCard(null)

    expect(screen.queryByText(PLAIN_WARNING)).toBeNull()
    expect(screen.queryByText(ENCRYPTED_NOTE)).toBeNull()
  })

  it('в списке помечает, какой бэкап зашифрован, а какой нет', async () => {
    mockApi([backup('backup_2.dump.enc', true), backup('backup_1.dump', false)])
    await renderCard(true)

    const rowOf = async (filename: string) => (await screen.findByText(filename)).closest('.rounded-xl') as HTMLElement
    expect((await rowOf('backup_2.dump.enc')).textContent).toContain('v10.80.0зашифрован')
    expect((await rowOf('backup_1.dump')).textContent).toContain('не зашифрован')
  })
})

describe('BackupCard: восстановление из зашифрованного бэкапа', () => {
  beforeEach(() => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
  })

  it('пустое поле пароля допустимо, когда пароль архива сохранён в панели', async () => {
    const restore = mockApi()
    const { container } = await renderCard(true)
    const file = encryptedFile('backup_20261114_040000.dump.enc')

    await chooseForRestore(container, file)
    expect(passwordField()).not.toBeNull()
    expect(screen.getByText(/панель подставит сохранённый пароль архива/)).toBeTruthy()
    fireEvent.click(restoreButton())

    await waitFor(() => expect(restore).toHaveBeenCalledTimes(1))
    expect(restore.mock.calls[0].slice(0, 2)).toEqual([[file], ''])
  })

  it('без сохранённого пароля восстановление ждёт, пока пароль введут', async () => {
    const restore = mockApi()
    const { container } = await renderCard(false)
    const file = encryptedFile('backup_20261114_040000.dump.enc')

    await chooseForRestore(container, file)
    expect(restoreButton().disabled).toBe(true)
    fireEvent.change(passwordField() as HTMLInputElement, { target: { value: 'archive-pw' } })
    expect(restoreButton().disabled).toBe(false)
    fireEvent.click(restoreButton())

    await waitFor(() => expect(restore).toHaveBeenCalledTimes(1))
    expect(restore.mock.calls[0].slice(0, 2)).toEqual([[file], 'archive-pw'])
  })

  it('зашифрованный бэкап узнаётся по содержимому, даже если файл переименован', async () => {
    mockApi()
    const { container } = await renderCard(false)

    await chooseForRestore(container, encryptedFile('renamed.bin'))

    expect(passwordField()).not.toBeNull()
  })

  it('открытый .dump пароля не спрашивает', async () => {
    const restore = mockApi()
    const { container } = await renderCard(false)
    const file = plainFile('backup_20261114_040000.dump')

    await chooseForRestore(container, file)
    expect(passwordField()).toBeNull()
    fireEvent.click(restoreButton())

    await waitFor(() => expect(restore).toHaveBeenCalledTimes(1))
    expect(restore.mock.calls[0].slice(0, 2)).toEqual([[file], ''])
  })
})
