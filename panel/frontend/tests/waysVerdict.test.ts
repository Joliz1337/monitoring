import { describe, expect, it } from 'vitest'
import { waysVerdict } from '../src/components/loss/waysVerdict'
import type { LossWay } from '../src/api/client'

const way = (over: Partial<LossWay>): LossWay => ({
  local_ip: '146.185.243.157', used: true, loss_pct: 0, rtt_ms: 40, samples: 60, answers: null, span_sec: 120, ...over,
})
const spare = (over: Partial<LossWay> = {}) => way({ local_ip: '10.0.0.97', used: false, samples: 30, ...over })

describe('вывод из сравнения адресов релея', () => {
  it('с выхода правила потери, с другого адреса чисто — дело, скорее всего, в выходе', () => {
    const lossy = way({ loss_pct: 60 })
    const clean = spare()

    expect(waysVerdict([lossy, clean])).toEqual({ kind: 'exit', silent: false, worse: lossy, better: clean })
  })

  it('с выхода сервер молчит, с другого адреса отвечает — то же, но про ответ', () => {
    const silent = way({ answers: false })
    const answering = spare({ answers: true })

    expect(waysVerdict([silent, answering])).toEqual({ kind: 'exit', silent: true, worse: silent, better: answering })
  })

  it('с другого адреса сервер отвечает, пусть и с небольшими потерями — это всё равно лучше молчания', () => {
    const answering = spare({ answers: true, loss_pct: 3.3 })

    expect(waysVerdict([way({ answers: false }), answering])).toMatchObject({ kind: 'exit', silent: true, better: answering })
    // Отвечает, но теряет почти половину — это уже не «лучше», а «плохо со всех адресов»
    expect(waysVerdict([way({ answers: false }), spare({ answers: true, loss_pct: 40 })])).toEqual({ kind: 'everywhere' })
  })

  it('потери с выхода против небольших потерь с другого адреса — не довод', () => {
    expect(waysVerdict([way({ loss_pct: 60 }), spare({ loss_pct: 3.3 })])).toBeNull()
  })

  it('с выхода молчит, а отвечает ли с другого — неизвестно: вывода нет', () => {
    expect(waysVerdict([way({ answers: false }), spare({ answers: null })])).toBeNull()
  })

  it('плохо со всех адресов — дело в сервере или дороге целиком', () => {
    expect(waysVerdict([way({ loss_pct: 80 }), spare({ loss_pct: 70 })])).toEqual({ kind: 'everywhere' })
    expect(waysVerdict([way({ answers: false }), spare({ answers: false })])).toEqual({ kind: 'everywhere' })
  })

  it('из нескольких выходов худший назван, из запасных — первый чистый', () => {
    const worst = way({ local_ip: '1.1.1.1', loss_pct: 90 })
    const verdict = waysVerdict([way({ local_ip: '1.1.1.2', loss_pct: 20 }), worst, spare({ loss_pct: 50 }), spare({ local_ip: '10.0.0.98' })])

    expect(verdict).toMatchObject({ kind: 'exit', worse: worst, better: { local_ip: '10.0.0.98' } })
  })

  it('с выхода всё хорошо — вывода нет, даже если запасной адрес плох', () => {
    expect(waysVerdict([way({}), spare({ loss_pct: 100 })])).toBeNull()
  })

  it('сравнивать не с чем или попыток мало — вывода нет', () => {
    expect(waysVerdict([way({ loss_pct: 60 })])).toBeNull()
    expect(waysVerdict([way({ loss_pct: 60 }), spare({ samples: 3 })])).toBeNull()
    expect(waysVerdict([way({ loss_pct: 60, samples: 4 }), spare()])).toBeNull()
    expect(waysVerdict([])).toBeNull()
  })

  it('небольшие потери с выхода поводом не считаются', () => {
    expect(waysVerdict([way({ loss_pct: 8 }), spare()])).toBeNull()
  })
})
