import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useTranslation } from 'react-i18next'
import type {
  ExpressionSpecification, FilterSpecification, GeoJSONSource, MapGeoJSONFeature, Popup,
} from 'maplibre-gl'
import { AlertTriangle, Loader2, MousePointerClick, Users } from 'lucide-react'
import { remnawaveApi, type RemnawaveGeoIpRow, type RemnawaveGeoMap } from '../../api/client'
import { getFlag } from '../../utils/format'
import {
  COMPARE_COLORS, COMPARE_DROP_RATIO, COMPARE_MIN_PREV, COMPARE_WARN_RATIO, countryName, formatAgo,
} from './geo'
import { LABEL_FONT } from './mapLoader'
import { ProviderBadge } from './ProviderBadge'
import { useMaplibreMap, useScrollZoomAfterClick, type MapHandle } from './useMaplibreMap'

// До этого зума — круги по странам, дальше — города с кластеризацией
const COUNTRY_MAX_ZOOM = 3
const INITIAL_MAX_ZOOM = 4
const SPIDER_LIMIT = 60
// Мельче этого зума «ромашка» расползается на полконтинента — прячем, пока не приблизят снова
const SPIDER_MIN_ZOOM = COUNTRY_MAX_ZOOM + 1
const SPIDER_RING_MAX = 8
const SPIDER_RING_PX = 34
const SPIDER_SPIRAL_START_PX = 22
const SPIDER_SPIRAL_GROWTH_PX = 2.6
const SPIDER_SPIRAL_ANGLE = 0.8
const BOUNDS_DEBOUNCE_MS = 300
const RADIUS_MIN_PX = 8
const RADIUS_MAX_PX = 40

const SRC = { countries: 'rw-countries', places: 'rw-places', spider: 'rw-spider' } as const
const LAYER = {
  countryCircle: 'rw-country-circle',
  countryLabel: 'rw-country-label',
  placeCircle: 'rw-place-circle',
  placeLabel: 'rw-place-label',
  placeName: 'rw-place-name',
  placeSelected: 'rw-place-selected',
  spiderLegs: 'rw-spider-legs',
  spiderPoints: 'rw-spider-points',
} as const

const CLICKABLE_LAYERS = [LAYER.spiderPoints, LAYER.placeCircle, LAYER.countryCircle]
const EMPTY: GeoJSON.FeatureCollection = { type: 'FeatureCollection', features: [] }
const COMPARE_STORAGE_KEY = 'remnawave.map.compare'

interface GeoMapProps {
  data: RemnawaveGeoMap | null
  node: string | null
  selectedPlace: string | null
  onSelectPlace: (key: string | null) => void
  onBoundsChange: (bbox: string | null) => void
  onOpenUser: (userId: number) => void
}

interface Spider {
  center: [number, number]
  rows: RemnawaveGeoIpRow[]
  total: number
}

interface HoverInfo {
  id: string
  lngLat: [number, number]
  title: string
  countryCode: string | null
  ips: number
  prev: number | null
  users: number | null
}

function compareProps(ips: number, prev: number | null) {
  return {
    ips,
    cmp_ips: prev !== null ? ips : 0,
    cmp_prev: prev ?? 0,
    ghost_prev: ips === 0 ? prev ?? 0 : 0,
    prev: prev ?? -1,
  }
}

function countriesCollection(data: RemnawaveGeoMap): GeoJSON.FeatureCollection {
  return {
    type: 'FeatureCollection',
    features: data.countries.map(c => ({
      type: 'Feature',
      geometry: { type: 'Point', coordinates: [c.lon, c.lat] },
      properties: { ...compareProps(c.ips, c.prev), country_code: c.country_code },
    })),
  }
}

function placesCollection(data: RemnawaveGeoMap): GeoJSON.FeatureCollection {
  return {
    type: 'FeatureCollection',
    features: data.locations.map(l => ({
      type: 'Feature',
      geometry: { type: 'Point', coordinates: [l.lon, l.lat] },
      properties: {
        ...compareProps(l.ips, l.prev), key: l.key, place: l.place, country_code: l.country_code, users: l.users,
      },
    })),
  }
}

function colorExpression(compare: boolean): ExpressionSpecification | string {
  if (!compare) return COMPARE_COLORS.normal
  const ratio: ExpressionSpecification = ['/', ['get', 'cmp_ips'], ['max', ['get', 'cmp_prev'], 1]]
  return [
    'case',
    ['<', ['get', 'cmp_prev'], COMPARE_MIN_PREV], COMPARE_COLORS.normal,
    ['<=', ratio, COMPARE_DROP_RATIO], COMPARE_COLORS.drop,
    ['<=', ratio, COMPARE_WARN_RATIO], COMPARE_COLORS.warn,
    COMPARE_COLORS.normal,
  ]
}

function radiusExpression(compare: boolean, maxValue: number): ExpressionSpecification {
  const value: ExpressionSpecification = compare ? ['max', ['get', 'ips'], ['get', 'ghost_prev']] : ['get', 'ips']
  return ['interpolate', ['linear'], ['sqrt', value], 0, RADIUS_MIN_PX, Math.max(Math.sqrt(maxValue), 1), RADIUS_MAX_PX]
}

// В режиме «только количество» пропавшие места (0 сейчас) не рисуем — им нечего показать.
// В режиме сравнения отдельное пропавшее место рисуем, только если вчера там было
// достаточно клиентов; мелкие всё равно попадают в суммы кластеров и стран.
function visibleFilter(compare: boolean): FilterSpecification {
  if (!compare) return ['>', ['get', 'ips'], 0]
  return ['any', ['has', 'point_count'], ['>', ['get', 'ips'], 0], ['>=', ['get', 'cmp_prev'], COMPARE_MIN_PREV]]
}

const COUNT_LABEL: ExpressionSpecification = [
  'case',
  ['>=', ['get', 'ips'], 1000],
  ['concat', ['number-format', ['/', ['get', 'ips'], 1000], { 'max-fraction-digits': 1 }], 'k'],
  ['to-string', ['get', 'ips']],
]

function readCompareSetting(): boolean {
  try {
    return window.localStorage.getItem(COMPARE_STORAGE_KEY) !== 'off'
  } catch {
    return true
  }
}

function writeCompareSetting(value: boolean) {
  try {
    window.localStorage.setItem(COMPARE_STORAGE_KEY, value ? 'on' : 'off')
  } catch { /* приватный режим — настройка просто не запомнится */ }
}

function addDataLayers({ map }: MapHandle) {
  map.addSource(SRC.countries, { type: 'geojson', data: EMPTY })
  map.addSource(SRC.places, {
    type: 'geojson',
    data: EMPTY,
    cluster: true,
    clusterRadius: 45,
    clusterMaxZoom: 10,
    clusterProperties: {
      ips: ['+', ['get', 'ips']],
      cmp_ips: ['+', ['get', 'cmp_ips']],
      cmp_prev: ['+', ['get', 'cmp_prev']],
      ghost_prev: ['+', ['get', 'ghost_prev']],
    },
  })
  map.addSource(SRC.spider, { type: 'geojson', data: EMPTY })

  const circlePaint = {
    'circle-radius': RADIUS_MIN_PX,
    'circle-color': COMPARE_COLORS.normal,
    'circle-opacity': ['case', ['==', ['get', 'ips'], 0], 0, 0.28] as ExpressionSpecification,
    'circle-stroke-color': COMPARE_COLORS.normal,
    'circle-stroke-width': 1.6,
    'circle-stroke-opacity': 0.9,
  }
  const labelLayout = {
    'text-field': COUNT_LABEL,
    'text-font': LABEL_FONT,
    'text-size': 11,
    'text-allow-overlap': true,
    'text-ignore-placement': true,
  }
  const labelPaint = { 'text-color': '#ecfeff', 'text-halo-color': '#0b0b0d', 'text-halo-width': 1 }

  map.addLayer({ id: LAYER.countryCircle, type: 'circle', source: SRC.countries, maxzoom: COUNTRY_MAX_ZOOM, paint: circlePaint })
  map.addLayer({ id: LAYER.countryLabel, type: 'symbol', source: SRC.countries, maxzoom: COUNTRY_MAX_ZOOM, layout: labelLayout, paint: labelPaint })
  map.addLayer({ id: LAYER.placeCircle, type: 'circle', source: SRC.places, minzoom: COUNTRY_MAX_ZOOM, paint: circlePaint })
  map.addLayer({
    id: LAYER.placeSelected, type: 'circle', source: SRC.places, minzoom: COUNTRY_MAX_ZOOM,
    filter: ['==', ['get', 'key'], ''],
    paint: { 'circle-radius': RADIUS_MIN_PX + 4, 'circle-opacity': 0, 'circle-stroke-color': '#ffffff', 'circle-stroke-width': 2 },
  })
  map.addLayer({ id: LAYER.placeLabel, type: 'symbol', source: SRC.places, minzoom: COUNTRY_MAX_ZOOM, layout: labelLayout, paint: labelPaint })
  map.addLayer({
    id: LAYER.placeName, type: 'symbol', source: SRC.places, minzoom: 5,
    filter: ['!', ['has', 'point_count']],
    layout: {
      'text-field': ['get', 'place'],
      'text-font': LABEL_FONT,
      'text-size': 10,
      'text-anchor': 'top',
      'text-offset': [0, 1.5],
    },
    paint: { 'text-color': '#a1a1aa', 'text-halo-color': '#0b0b0d', 'text-halo-width': 1 },
  })
  map.addLayer({
    id: LAYER.spiderLegs, type: 'line', source: SRC.spider,
    filter: ['==', ['geometry-type'], 'LineString'],
    paint: { 'line-color': COMPARE_COLORS.normal, 'line-opacity': 0.45, 'line-width': 1 },
  })
  map.addLayer({
    id: LAYER.spiderPoints, type: 'circle', source: SRC.spider,
    filter: ['==', ['geometry-type'], 'Point'],
    paint: {
      'circle-radius': 5, 'circle-color': COMPARE_COLORS.normal,
      'circle-stroke-color': '#ffffff', 'circle-stroke-width': 1,
    },
  })
}

function featureCoords(feature: MapGeoJSONFeature): [number, number] {
  const [lng, lat] = (feature.geometry as GeoJSON.Point).coordinates
  return [lng, lat]
}

export default function GeoMap({ data, node, selectedPlace, onSelectPlace, onBoundsChange, onOpenUser }: GeoMapProps) {
  const { t, i18n } = useTranslation()
  const containerRef = useRef<HTMLDivElement>(null)
  const [compare, setCompare] = useState(readCompareSetting)
  const [ipPopup, setIpPopup] = useState<{ row: RemnawaveGeoIpRow; lngLat: [number, number]; more: number } | null>(null)
  const [hover, setHover] = useState<HoverInfo | null>(null)
  const spiderRef = useRef<Spider | null>(null)
  const fittedRef = useRef(false)
  const callbacks = useRef({ onSelectPlace, onBoundsChange, onOpenUser, node })
  callbacks.current = { onSelectPlace, onBoundsChange, onOpenUser, node }

  const mapOptions = useMemo(() => ({
    center: [45, 52] as [number, number],
    zoom: 2,
    minZoom: 1,
    maxZoom: 13,
    dragRotate: false,
    pitchWithRotate: false,
    touchPitch: false,
  }), [])

  const { handle, styleVersion, isFallback, failed } = useMaplibreMap(containerRef, mapOptions, addDataLayers)
  const scrollEnabled = useScrollZoomAfterClick(handle)

  // Без очистки: map.remove() при размонтировании сам снимает элементы управления,
  // а повторный removeControl на уже удалённой карте падает
  useEffect(() => {
    if (!handle) return
    handle.map.addControl(new handle.ml.NavigationControl({ showCompass: false }), 'top-right')
  }, [handle])

  const compareActive = compare && !!data?.compare_available
  const maxValue = useMemo(
    () => Math.max(1, ...(data?.countries ?? []).map(c => Math.max(c.ips, c.prev ?? 0))),
    [data],
  )

  const renderSpider = useCallback(() => {
    const map = handle?.map
    const source = map?.getSource<GeoJSONSource>(SRC.spider)
    if (!map || !source) return
    const spider = spiderRef.current
    if (!spider || map.getZoom() < SPIDER_MIN_ZOOM) {
      source.setData(EMPTY)
      return
    }
    const center = map.project(spider.center)
    const count = spider.rows.length
    const features: GeoJSON.Feature[] = []
    spider.rows.forEach((_, i) => {
      const ring = count <= SPIDER_RING_MAX
      const angle = ring ? (2 * Math.PI * i) / count - Math.PI / 2 : i * SPIDER_SPIRAL_ANGLE
      const radius = ring ? SPIDER_RING_PX : SPIDER_SPIRAL_START_PX + i * SPIDER_SPIRAL_GROWTH_PX
      const point = map.unproject([center.x + radius * Math.cos(angle), center.y + radius * Math.sin(angle)])
      const coords: [number, number] = [point.lng, point.lat]
      features.push({ type: 'Feature', geometry: { type: 'Point', coordinates: coords }, properties: { idx: i } })
      features.push({ type: 'Feature', geometry: { type: 'LineString', coordinates: [spider.center, coords] }, properties: {} })
    })
    source.setData({ type: 'FeatureCollection', features })
  }, [handle])

  const clearSpider = useCallback(() => {
    spiderRef.current = null
    setIpPopup(null)
    renderSpider()
  }, [renderSpider])

  // Данные карты
  useEffect(() => {
    const map = handle?.map
    if (!map || !styleVersion || !data) return
    map.getSource<GeoJSONSource>(SRC.countries)?.setData(countriesCollection(data))
    map.getSource<GeoJSONSource>(SRC.places)?.setData(placesCollection(data))

    if (!fittedRef.current && data.locations.length > 0) {
      fittedRef.current = true
      const lons = data.locations.map(l => l.lon)
      const lats = data.locations.map(l => l.lat)
      const bounds: [[number, number], [number, number]] = [
        [Math.min(...lons), Math.min(...lats)], [Math.max(...lons), Math.max(...lats)],
      ]
      map.fitBounds(bounds, { padding: { top: 64, bottom: 40, left: 40, right: 64 }, maxZoom: INITIAL_MAX_ZOOM, duration: 0 })
    }
  }, [handle, styleVersion, data])

  // Раскраска и размеры зависят от режима сравнения и масштаба данных
  useEffect(() => {
    const map = handle?.map
    if (!map || !styleVersion) return
    const color = colorExpression(compareActive)
    const radius = radiusExpression(compareActive, maxValue)
    const filter = visibleFilter(compareActive)
    for (const id of [LAYER.countryCircle, LAYER.placeCircle]) {
      map.setPaintProperty(id, 'circle-color', color)
      map.setPaintProperty(id, 'circle-stroke-color', color)
      map.setPaintProperty(id, 'circle-radius', radius)
      map.setFilter(id, filter)
    }
    for (const id of [LAYER.countryLabel, LAYER.placeLabel]) map.setFilter(id, filter)
    map.setPaintProperty(LAYER.placeSelected, 'circle-radius', ['+', radius, 4])
  }, [handle, styleVersion, compareActive, maxValue])

  useEffect(() => {
    const map = handle?.map
    if (!map || !styleVersion) return
    map.setFilter(LAYER.placeSelected, ['==', ['get', 'key'], selectedPlace ?? ''])
    if (!selectedPlace) clearSpider()
  }, [handle, styleVersion, selectedPlace, clearSpider])

  useEffect(() => { clearSpider() }, [node, clearSpider])

  // События карты
  useEffect(() => {
    if (!handle) return
    const { map } = handle
    let boundsTimer: number | undefined

    const openSpider = async (feature: MapGeoJSONFeature) => {
      const key = String(feature.properties.key)
      const center = featureCoords(feature)
      try {
        const res = await remnawaveApi.getGeoIps({
          place: key, node: callbacks.current.node ?? undefined, limit: SPIDER_LIMIT, active_only: true,
        })
        const { rows, total } = res.data
        if (rows.length === 1) {
          spiderRef.current = null
          renderSpider()
          setIpPopup({ row: rows[0], lngLat: center, more: 0 })
          return
        }
        spiderRef.current = { center, rows, total }
        setIpPopup(null)
        renderSpider()
      } catch { /* сеть моргнула — клик можно повторить */ }
    }

    const onClick = (e: { point: { x: number; y: number } }) => {
      const layers = CLICKABLE_LAYERS.filter(id => map.getLayer(id))
      const feature = map.queryRenderedFeatures([e.point.x, e.point.y], { layers })[0]
      if (!feature) {
        callbacks.current.onSelectPlace(null)
        return
      }
      if (feature.layer.id === LAYER.spiderPoints) {
        const spider = spiderRef.current
        const row = spider?.rows[Number(feature.properties.idx)]
        if (spider && row) setIpPopup({ row, lngLat: featureCoords(feature), more: spider.total - spider.rows.length })
        return
      }
      if (feature.layer.id === LAYER.countryCircle) {
        map.easeTo({ center: featureCoords(feature), zoom: COUNTRY_MAX_ZOOM + 1.5 })
        return
      }
      if (feature.properties.point_count) {
        const source = map.getSource<GeoJSONSource>(SRC.places)
        source?.getClusterExpansionZoom(Number(feature.properties.cluster_id))
          .then(zoom => map.easeTo({ center: featureCoords(feature), zoom: zoom + 0.2 }))
          .catch(() => undefined)
        return
      }
      callbacks.current.onSelectPlace(String(feature.properties.key))
      void openSpider(feature)
    }

    const onMouseMove = (e: { point: { x: number; y: number } }) => {
      const layers = CLICKABLE_LAYERS.filter(id => map.getLayer(id))
      const feature = map.queryRenderedFeatures([e.point.x, e.point.y], { layers })[0]
      map.getCanvas().style.cursor = feature ? 'pointer' : ''
      if (!feature || feature.layer.id === LAYER.spiderPoints) {
        setHover(null)
        return
      }
      const p = feature.properties
      const isCluster = !!p.point_count
      const isCountry = feature.layer.id === LAYER.countryCircle
      const id = isCluster ? `c${p.cluster_id}` : isCountry ? `k${p.country_code}` : `p${p.key}`
      setHover(current => current?.id === id ? current : {
        id,
        lngLat: featureCoords(feature),
        title: isCluster ? '' : isCountry ? String(p.country_code) : String(p.place),
        countryCode: isCluster ? null : String(p.country_code),
        ips: Number(p.ips),
        prev: Number(p.cmp_prev) >= COMPARE_MIN_PREV ? Number(p.cmp_prev) : null,
        users: isCluster || isCountry ? null : Number(p.users),
      })
    }

    const onMoveEnd = () => {
      window.clearTimeout(boundsTimer)
      boundsTimer = window.setTimeout(() => {
        // На уровне «весь мир» таблица показывает всё, включая клиентов через релеи и без места
        if (map.getZoom() < COUNTRY_MAX_ZOOM) {
          callbacks.current.onBoundsChange(null)
          return
        }
        const b = map.getBounds()
        callbacks.current.onBoundsChange(
          [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()].map(v => v.toFixed(4)).join(','),
        )
      }, BOUNDS_DEBOUNCE_MS)
    }

    const onMouseLeave = () => setHover(null)

    map.on('click', onClick)
    map.on('mousemove', onMouseMove)
    map.on('mouseout', onMouseLeave)
    map.on('moveend', onMoveEnd)
    map.on('zoom', renderSpider)
    return () => {
      window.clearTimeout(boundsTimer)
      map.off('click', onClick)
      map.off('mousemove', onMouseMove)
      map.off('mouseout', onMouseLeave)
      map.off('moveend', onMoveEnd)
      map.off('zoom', renderSpider)
    }
  }, [handle, renderSpider])

  // Попапы MapLibre с React-содержимым через портал
  const ipPopupNode = useMemo(() => document.createElement('div'), [])
  const hoverNode = useMemo(() => document.createElement('div'), [])
  const ipPopupRef = useRef<Popup | null>(null)

  useEffect(() => {
    if (!handle || !ipPopup) return
    const popup = new handle.ml.Popup({ closeButton: true, maxWidth: '300px', offset: 10, className: 'rw-map-popup' })
      .setLngLat(ipPopup.lngLat)
      .setDOMContent(ipPopupNode)
      .addTo(handle.map)
    ipPopupRef.current = popup
    popup.on('close', () => setIpPopup(current => (ipPopupRef.current === popup ? null : current)))
    return () => {
      ipPopupRef.current = null
      popup.remove()
    }
  }, [handle, ipPopup, ipPopupNode])

  useEffect(() => {
    if (!handle || !hover) return
    const popup = new handle.ml.Popup({
      closeButton: false, closeOnClick: false, offset: 14, className: 'rw-map-popup rw-map-hover',
    })
      .setLngLat(hover.lngLat)
      .setDOMContent(hoverNode)
      .addTo(handle.map)
    return () => { popup.remove() }
  }, [handle, hover, hoverNode])

  const toggleCompare = (value: boolean) => {
    setCompare(value)
    writeCompareSetting(value)
  }

  const lang = i18n.language
  const notes: string[] = []
  if (data && data.relay_users > 0) notes.push(t('remnawave.geo.relay_note', { count: data.relay_users }))
  if (data && data.unlocated_ips > 0) notes.push(t('remnawave.geo.unlocated_note', { count: data.unlocated_ips }))

  return (
    <div className="space-y-2">
      <div className="relative rw-map rounded-xl overflow-hidden border border-dark-800/60 bg-[#0b0b0d]">
        <div ref={containerRef} className="h-[340px] sm:h-[460px] w-full" />

        {!handle && !failed && (
          <div className="absolute inset-0 flex items-center justify-center">
            <Loader2 className="w-6 h-6 text-dark-500 animate-spin" />
          </div>
        )}
        {failed && (
          <div className="absolute inset-0 flex items-center justify-center text-sm text-dark-400">
            <AlertTriangle className="w-4 h-4 mr-2 text-warning" />
            {t('remnawave.geo.map_failed')}
          </div>
        )}

        <div className="absolute top-3 left-3 flex flex-col gap-2 items-start pointer-events-none">
          <div className="pointer-events-auto flex p-0.5 rounded-lg bg-dark-950/85 border border-dark-800/80 text-xs">
            {([true, false] as const).map(value => (
              <button
                key={String(value)}
                type="button"
                onClick={() => toggleCompare(value)}
                disabled={value && !data?.compare_available}
                className={`px-2.5 py-1 rounded-md transition-colors disabled:opacity-40 disabled:cursor-not-allowed ${
                  compare === value ? 'bg-accent-500/20 text-accent-300' : 'text-dark-400 hover:text-dark-200'
                }`}
              >
                {value ? t('remnawave.geo.compare_yesterday') : t('remnawave.geo.compare_count_only')}
              </button>
            ))}
          </div>
          {compare && data && !data.compare_available && (
            <div className="text-[11px] text-dark-400 bg-dark-950/85 border border-dark-800/80 rounded-lg px-2 py-1">
              {node ? t('remnawave.geo.compare_unavailable_node') : t('remnawave.geo.compare_unavailable_nodata')}
            </div>
          )}
          {data && !data.geo_ready && (
            <div className="text-[11px] text-warning bg-dark-950/85 border border-dark-800/80 rounded-lg px-2 py-1 max-w-[260px]">
              {data.geo_error
                ? t('remnawave.geo.geo_failed', { error: data.geo_error })
                : t('remnawave.geo.geo_loading')}
            </div>
          )}
          {isFallback && (
            <div className="text-[11px] text-dark-400 bg-dark-950/85 border border-dark-800/80 rounded-lg px-2 py-1">
              {t('remnawave.geo.fallback_basemap')}
            </div>
          )}
        </div>

        {handle && !scrollEnabled && (
          <div className="absolute top-3 left-1/2 -translate-x-1/2 hidden sm:flex items-center gap-1.5 text-[11px] text-dark-400 bg-dark-950/80 border border-dark-800/80 rounded-lg px-2 py-1 pointer-events-none">
            <MousePointerClick className="w-3.5 h-3.5" />
            {t('remnawave.geo.scroll_hint')}
          </div>
        )}

        {ipPopup && createPortal(
          <IpPopupContent
            row={ipPopup.row}
            more={ipPopup.more}
            lang={lang}
            onOpenUser={id => callbacks.current.onOpenUser(id)}
          />,
          ipPopupNode,
        )}
        {hover && createPortal(<HoverContent info={hover} lang={lang} />, hoverNode)}
      </div>

      {(compareActive || notes.length > 0) && (
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 px-1 text-[11px] text-dark-400">
          {compareActive && (
            <>
              <LegendDot color={COMPARE_COLORS.normal} label={t('remnawave.geo.legend_normal')} />
              <LegendDot color={COMPARE_COLORS.warn} label={t('remnawave.geo.legend_warn')} />
              <LegendDot color={COMPARE_COLORS.drop} label={t('remnawave.geo.legend_drop')} />
              <LegendDot color={COMPARE_COLORS.drop} label={t('remnawave.geo.legend_vanished')} hollow />
            </>
          )}
          <div className="flex-1" />
          {notes.map(note => <span key={note}>{note}</span>)}
        </div>
      )}
    </div>
  )
}

function LegendDot({ color, label, hollow = false }: { color: string; label: string; hollow?: boolean }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span
        className="w-2.5 h-2.5 rounded-full"
        style={hollow ? { border: `1.5px solid ${color}` } : { background: color }}
      />
      {label}
    </span>
  )
}

function HoverContent({ info, lang }: { info: HoverInfo; lang: string }) {
  const { t } = useTranslation()
  const isCountryOnly = info.countryCode && info.title === info.countryCode
  const title = isCountryOnly ? countryName(info.countryCode, lang) : info.title
  return (
    <div className="text-xs space-y-1 min-w-[140px]">
      {title && (
        <div className="font-medium text-dark-100 flex items-center gap-1.5">
          {info.countryCode && <span>{getFlag(info.countryCode)}</span>}
          {title}
          {!isCountryOnly && info.countryCode && (
            <span className="text-dark-500 font-normal">{countryName(info.countryCode, lang)}</span>
          )}
        </div>
      )}
      <div className="flex items-center gap-3 text-dark-300">
        <span><b className="text-dark-100">{info.ips}</b> {t('remnawave.geo.place_now')}</span>
        {info.prev !== null && (
          <span className="text-dark-500"><b className="text-dark-300">{info.prev}</b> {t('remnawave.geo.place_yesterday')}</span>
        )}
      </div>
      {info.users !== null && (
        <div className="flex items-center gap-1 text-dark-400">
          <Users className="w-3 h-3" /> {info.users}
        </div>
      )}
    </div>
  )
}

function IpPopupContent({ row, more, lang, onOpenUser }: {
  row: RemnawaveGeoIpRow
  more: number
  lang: string
  onOpenUser: (id: number) => void
}) {
  const { t } = useTranslation()
  return (
    <div className="text-xs space-y-2 min-w-[200px]">
      <div className="flex items-center justify-between gap-3">
        <span className="font-mono text-dark-50 text-sm">{row.ip}</span>
        <span className="text-dark-500">{formatAgo(row.last_seen, lang)}</span>
      </div>
      {row.place && (
        <div className="text-dark-300">
          {getFlag(row.country_code)} {row.place}
        </div>
      )}
      <ProviderBadge asOrg={row.as_org} asn={row.asn} />
      {row.nodes.length > 0 && (
        <div className="text-dark-400">
          {row.nodes.map(n => `${getFlag(n.country_code)} ${n.name ?? n.uuid.slice(0, 8)}`).join(', ')}
        </div>
      )}
      <div className="flex flex-col gap-1 pt-1 border-t border-dark-800">
        {row.users.map(user => (
          <button
            key={user.id}
            type="button"
            onClick={() => onOpenUser(user.id)}
            className="text-left text-accent-400 hover:text-accent-300 transition-colors"
          >
            {t('remnawave.geo.popup_open_user')}: {user.username ?? `#${user.id}`}
          </button>
        ))}
        {row.users_total > row.users.length && (
          <span className="text-dark-500">+{row.users_total - row.users.length}</span>
        )}
      </div>
      {more > 0 && <div className="text-dark-500">{t('remnawave.geo.popup_more_in_table', { count: more })}</div>}
    </div>
  )
}
