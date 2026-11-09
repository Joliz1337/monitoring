import type { StyleSpecification } from 'maplibre-gl'
import countriesUrl from './countries.geo.json?url'
import maplibreWorkerUrl from 'maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url'

// MapLibre — самый тяжёлый модуль панели (~1 МБ), поэтому грузится отдельным
// чанком только при открытии карты, а не в общем бандле.
export type MapLibreModule = typeof import('maplibre-gl')

let maplibrePromise: Promise<MapLibreModule> | null = null

export function loadMaplibre(): Promise<MapLibreModule> {
  if (!maplibrePromise) {
    maplibrePromise = Promise.all([
      import('maplibre-gl'),
      import('maplibre-gl/dist/maplibre-gl.css'),
    ]).then(([module]) => {
      // Свой воркер MapLibre ищет рядом с собственным файлом, а после сборки Vite
      // его там нет — путь к собранному воркеру задаём явно
      module.setWorkerUrl(maplibreWorkerUrl)
      return module
    })
    maplibrePromise.catch(() => { maplibrePromise = null })
  }
  return maplibrePromise
}

const OFM_STYLE_URL = 'https://tiles.openfreemap.org/styles/dark'
export const BASEMAP_SOURCE = 'openmaptiles'
// Запасная подложка не должна зависеть от OpenFreeMap и в шрифтах: латиница
// и цифры подписей лежат у панели (Noto Sans Bold, OFL), остальное MapLibre рисует сам
const LOCAL_GLYPHS_PATH = '/assets/map-glyphs/{fontstack}/{range}.pbf'
export const LABEL_FONT = ['Noto Sans Bold']
const STYLE_TIMEOUT_MS = 8000

export const DBIP_ATTRIBUTION = '<a href="https://db-ip.com" target="_blank" rel="noopener">IP Geolocation by DB-IP</a>'

export interface MapStyleResult {
  style: StyleSpecification
  isFallback: boolean
}

function localizeLabels(style: StyleSpecification, lang: string): void {
  const localized = ['coalesce', ['get', `name:${lang}`], ['get', 'name:latin'], ['get', 'name']]
  for (const layer of style.layers) {
    if (layer.type !== 'symbol' || !layer.layout?.['text-field']) continue
    // Номера дорог и прочие не-имена оставляем как в стиле
    if (!JSON.stringify(layer.layout['text-field']).includes('name')) continue
    layer.layout['text-field'] = localized as never
  }
}

export function fallbackStyle(): StyleSpecification {
  return {
    version: 8,
    glyphs: `${window.location.origin}${LOCAL_GLYPHS_PATH}`,
    sources: { 'fallback-countries': { type: 'geojson', data: countriesUrl } },
    layers: [
      { id: 'fallback-bg', type: 'background', paint: { 'background-color': '#0b0b0d' } },
      { id: 'fallback-land', type: 'fill', source: 'fallback-countries', paint: { 'fill-color': '#1a1a1f' } },
      {
        id: 'fallback-borders', type: 'line', source: 'fallback-countries',
        paint: { 'line-color': '#34343d', 'line-width': 0.6 },
      },
    ],
  }
}

async function buildMapStyle(lang: string): Promise<MapStyleResult> {
  const controller = new AbortController()
  const timer = window.setTimeout(() => controller.abort(), STYLE_TIMEOUT_MS)
  try {
    const response = await fetch(OFM_STYLE_URL, { signal: controller.signal })
    if (!response.ok) return { style: fallbackStyle(), isFallback: true }
    const style = (await response.json()) as StyleSpecification
    localizeLabels(style, lang)
    return { style, isFallback: false }
  } catch {
    return { style: fallbackStyle(), isFallback: true }
  } finally {
    window.clearTimeout(timer)
  }
}

const styleCache = new Map<string, Promise<MapStyleResult>>()

// Стиль качается один раз на язык; каждой карте — своя копия, MapLibre может её менять
export function getMapStyle(lang: string): Promise<MapStyleResult> {
  const key = lang.startsWith('ru') ? 'ru' : 'en'
  let pending = styleCache.get(key)
  if (!pending) {
    pending = buildMapStyle(key)
    styleCache.set(key, pending)
  }
  return pending.then(result => {
    if (result.isFallback) styleCache.delete(key)
    return { style: structuredClone(result.style), isFallback: result.isFallback }
  })
}
