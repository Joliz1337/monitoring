import { afterEach, beforeAll } from 'vitest'
import { cleanup } from '@testing-library/react'
import i18n from '../src/i18n'

// Язык фиксирован: jsdom выдаёт себя за en-US, а проверки читают русские подписи
beforeAll(async () => {
  await i18n.changeLanguage('ru')
})

afterEach(cleanup)
