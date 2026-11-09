import { useEffect, useMemo, useRef } from 'react'
import { createPortal } from 'react-dom'
import { useTranslation } from 'react-i18next'
import type { ExpressionSpecification, GeoJSONSource, LngLatBoundsLike } from 'maplibre-gl'
import { Loader2 } from 'lucide-react'
import type { ClientRoute, ClientRouteLeg, ClientRouteStop } from '../../api/client'
import { getFlag } from '../../utils/format'
import { ROUTE_LEG_COLORS, ROUTE_STOP_COLORS } from './geo'
import { useCollapsedAttribution, useMaplibreMap, type MapHandle } from './useMaplibreMap'

const SRC = 'rw-route-legs'
const LAYER = {
  glow: 'rw-route-glow',
  core: 'rw-route-core',
  lastMile: 'rw-route-last-mile',
  flow: 'rw-route-flow',
  hit: 'rw-route-hit',
} as const
const GROW_MS = 1400
const FLOW_FRAME_MS = 55
// Справа шире: подписи остановок стоят правее точки
const FIT_PADDING = { top: 48, bottom: 48, left: 48, right: 170 }
const FIT_MAX_ZOOM = 6
const FOCUS_MAX_ZOOM = 8
// Штрих «бежит» по линии от клиента к ноде: сдвиг рисунка пунктира по кругу
const DASH_STEPS: number[][] = [
  [0, 4, 3], [0.5, 4, 2.5], [1, 4, 2], [1.5, 4, 1.5], [2, 4, 1], [2.5, 4, 0.5], [3, 4, 0],
  [0, 0.5, 3, 3.5], [0, 1, 3, 3], [0, 1.5, 3, 2.5], [0, 2, 3, 2], [0, 2.5, 3, 1.5], [0, 3, 3, 1], [0, 3.5, 3, 0.5],
]

type LngLat = [number, number]

const EMPTY: GeoJSON.FeatureCollection = { type: 'FeatureCollection', features: [] }

const legColor: ExpressionSpecification = [
  'match', ['get', 'status'],
  'ok', ROUTE_LEG_COLORS.ok,
  'warn', ROUTE_LEG_COLORS.warn,
  'bad', ROUTE_LEG_COLORS.bad,
  ROUTE_LEG_COLORS.unknown,
]
const hovered: ExpressionSpecification = ['boolean', ['feature-state', 'hover'], false]

function addLayers({ map }: MapHandle) {
  map.addSource(SRC, { type: 'geojson', data: EMPTY })
  const round = { 'line-cap': 'round', 'line-join': 'round' } as const
  map.addLayer({
    id: LAYER.glow, type: 'line', source: SRC, layout: round,
    paint: {
      'line-color': legColor,
      'line-width': ['interpolate', ['linear'], ['zoom'], 2, 8, 8, 18],
      'line-blur': 8,
      'line-opacity': ['case', hovered, 0.75, 0.4],
    },
  })
  map.addLayer({
    id: LAYER.core, type: 'line', source: SRC, layout: round,
    filter: ['!', ['get', 'last_mile']],
    paint: {
      'line-color': legColor,
      'line-width': ['case', hovered, 4.5, ['interpolate', ['linear'], ['zoom'], 2, 2, 8, 3.5]],
      'line-opacity': 0.95,
    },
  })
  map.addLayer({
    id: LAYER.lastMile, type: 'line', source: SRC, layout: { 'line-join': 'round' },
    filter: ['get', 'last_mile'],
    paint: { 'line-color': legColor, 'line-width': 2, 'line-opacity': 0.8, 'line-dasharray': [1.5, 2] },
  })
  map.addLayer({
    id: LAYER.flow, type: 'line', source: SRC, layout: { 'line-join': 'round' },
    filter: ['!', ['get', 'last_mile']],
    paint: {
      'line-color': '#ffffff',
      'line-width': ['interpolate', ['linear'], ['zoom'], 2, 1.2, 8, 2.2],
      'line-opacity': 0.8,
      'line-dasharray': DASH_STEPS[0],
    },
  })
  map.addLayer({ id: LAYER.hit, type: 'line', source: SRC, paint: { 'line-color': '#000000', 'line-width': 18, 'line-opacity': 0 } })
}

// Длина в «градусах на месте»: для доли отрезка при росте линии точности хватает
function segmentLength(a: LngLat, b: LngLat): number {
  const dx = (b[0] - a[0]) * Math.cos(((a[1] + b[1]) / 2) * (Math.PI / 180))
  return Math.hypot(dx, b[1] - a[1])
}

function slicePolyline(coords: LngLat[], fraction: number): LngLat[] {
  if (fraction >= 1 || coords.length < 2) return coords
  const lengths = coords.slice(1).map((point, i) => segmentLength(coords[i], point))
  let remaining = lengths.reduce((sum, value) => sum + value, 0) * fraction
  const result: LngLat[] = [coords[0]]
  for (let i = 0; i < lengths.length; i++) {
    if (lengths[i] <= remaining) {
      result.push(coords[i + 1])
      remaining -= lengths[i]
      continue
    }
    const t = lengths[i] ? remaining / lengths[i] : 0
    result.push([coords[i][0] + (coords[i + 1][0] - coords[i][0]) * t, coords[i][1] + (coords[i + 1][1] - coords[i][1]) * t])
    break
  }
  return result
}

// Маршрут «прорастает» от клиента к ноде: доля общей длины, а не по участку за раз
function legsCollection(legs: ClientRouteLeg[], fraction: number): GeoJSON.FeatureCollection {
  const total = legs.reduce((sum, leg) => sum + leg.km, 0)
  let start = 0
  const features: GeoJSON.Feature[] = []
  legs.forEach((leg, index) => {
    const shown = total ? (total * fraction - start) / (leg.km || 1) : 1
    start += leg.km
    if (shown <= 0) return
    features.push({
      type: 'Feature',
      id: index,
      geometry: { type: 'LineString', coordinates: slicePolyline(leg.coords, Math.min(1, shown)) },
      properties: { index, status: leg.status ?? 'unknown', last_mile: leg.last_mile },
    })
  })
  return { type: 'FeatureCollection', features }
}

function boundsOf(coords: LngLat[]): LngLatBoundsLike | null {
  if (coords.length === 0) return null
  const lons = coords.map(c => c[0])
  const lats = coords.map(c => c[1])
  return [[Math.min(...lons), Math.min(...lats)], [Math.max(...lons), Math.max(...lats)]]
}

interface RouteMapProps {
  route: ClientRoute | null
  nodeName: string
  focus: { leg: number; nonce: number } | null
  hoveredLeg: number | null
  onHoverLeg: (index: number | null) => void
}

export default function RouteMap({ route, nodeName, focus, hoveredLeg, onHoverLeg }: RouteMapProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const mapOptions = useMemo(() => ({
    center: [30, 50] as LngLat,
    zoom: 2.5,
    minZoom: 1,
    maxZoom: 12,
    dragRotate: false,
    pitchWithRotate: false,
    touchPitch: false,
  }), [])
  const { handle, styleVersion } = useMaplibreMap(containerRef, mapOptions, addLayers)
  useCollapsedAttribution(handle, styleVersion)
  const legs = useMemo(() => route?.legs ?? [], [route])
  const stops = route?.stops ?? []
  const legsRef = useRef(legs)
  legsRef.current = legs
  // Ref, а не состояние: эффект данных в том же коммите уже должен видеть, что идёт рост,
  // иначе полный маршрут мелькнул бы на кадр до анимации
  const growing = useRef(false)
  const grownFor = useRef<number | null>(null)
  const userMoved = useRef(false)
  const fittedStops = useRef(0)

  useEffect(() => {
    if (!handle) return
    const onMoveStart = (event: { originalEvent?: unknown }) => {
      if (event.originalEvent) userMoved.current = true
    }
    handle.map.on('movestart', onMoveStart)
    return () => { handle.map.off('movestart', onMoveStart) }
  }, [handle])

  // Новая трасса — карта снова сама показывает весь путь и снова «прорастает»
  const hasLegs = legs.length > 0
  useEffect(() => {
    if (route) return
    userMoved.current = false
    fittedStops.current = 0
  }, [route])

  // Первый маршрут прорастает анимацией, дальше опросы просто обновляют линии
  useEffect(() => {
    const map = handle?.map
    if (!hasLegs) {
      grownFor.current = null
      return
    }
    if (!map || !styleVersion || grownFor.current === styleVersion) return
    grownFor.current = styleVersion
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return
    growing.current = true
    const started = performance.now()
    let frame = 0
    // Каждый кадр берёт свежие участки: опрос может принести новые посреди роста
    const step = (now: number) => {
      const fraction = Math.min(1, (now - started) / GROW_MS)
      map.getSource<GeoJSONSource>(SRC)?.setData(legsCollection(legsRef.current, fraction))
      if (fraction < 1) frame = requestAnimationFrame(step)
      else growing.current = false
    }
    frame = requestAnimationFrame(step)
    return () => {
      cancelAnimationFrame(frame)
      growing.current = false
    }
  }, [handle, styleVersion, hasLegs])

  useEffect(() => {
    const map = handle?.map
    if (!map || !styleVersion || growing.current) return
    map.getSource<GeoJSONSource>(SRC)?.setData(legsCollection(legs, 1))
  }, [handle, styleVersion, legs])

  useEffect(() => {
    const map = handle?.map
    if (!map || !styleVersion || window.matchMedia('(prefers-reduced-motion: reduce)').matches) return
    let frame = 0
    let shown = -1
    const tick = (now: number) => {
      const step = Math.floor(now / FLOW_FRAME_MS) % DASH_STEPS.length
      if (step !== shown && map.getLayer(LAYER.flow)) {
        map.setPaintProperty(LAYER.flow, 'line-dasharray', DASH_STEPS[step])
        shown = step
      }
      frame = requestAnimationFrame(tick)
    }
    frame = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(frame)
  }, [handle, styleVersion])

  // Новые города трассы — карта показывает весь путь, пока человек сам её не двигал
  useEffect(() => {
    const map = handle?.map
    if (!map || !styleVersion || stops.length < 2 || stops.length === fittedStops.current || userMoved.current) return
    const bounds = boundsOf([...legs.flatMap(leg => leg.coords), ...stops.map(s => [s.lon, s.lat] as LngLat)])
    if (!bounds) return
    map.fitBounds(bounds, { padding: FIT_PADDING, maxZoom: FIT_MAX_ZOOM, duration: fittedStops.current ? 600 : 0 })
    fittedStops.current = stops.length
  }, [handle, styleVersion, stops, legs])

  useEffect(() => {
    const map = handle?.map
    const leg = focus ? legsRef.current[focus.leg] : undefined
    const bounds = leg ? boundsOf(leg.coords) : null
    if (!map || !bounds) return
    userMoved.current = true
    map.fitBounds(bounds, { padding: FIT_PADDING, maxZoom: FOCUS_MAX_ZOOM })
  }, [handle, focus])

  useEffect(() => {
    const map = handle?.map
    if (!map || !styleVersion) return
    const onMove = (event: { features?: { id?: string | number }[] }) => {
      const id = event.features?.[0]?.id
      onHoverLeg(typeof id === 'number' ? id : null)
      map.getCanvas().style.cursor = 'pointer'
    }
    const onLeave = () => {
      onHoverLeg(null)
      map.getCanvas().style.cursor = ''
    }
    map.on('mousemove', LAYER.hit, onMove)
    map.on('mouseleave', LAYER.hit, onLeave)
    return () => {
      map.off('mousemove', LAYER.hit, onMove)
      map.off('mouseleave', LAYER.hit, onLeave)
    }
  }, [handle, styleVersion, onHoverLeg])

  useEffect(() => {
    const map = handle?.map
    if (!map || !styleVersion || hoveredLeg === null || !map.getSource(SRC)) return
    map.setFeatureState({ source: SRC, id: hoveredLeg }, { hover: true })
    return () => {
      if (map.getSource(SRC)) map.setFeatureState({ source: SRC, id: hoveredLeg }, { hover: false })
    }
  }, [handle, styleVersion, hoveredLeg, legs])

  return (
    <div className="relative rw-map h-full w-full bg-[#0b0b0d]">
      <div ref={containerRef} className="h-full w-full" />
      {!handle && (
        <div className="absolute inset-0 flex items-center justify-center">
          <Loader2 className="w-6 h-6 text-dark-500 animate-spin" />
        </div>
      )}
      {handle && styleVersion > 0 && stops.map((stop, index) => (
        <StopMarker key={`${index}-${stop.lon}-${stop.lat}`} handle={handle} stop={stop} nodeName={nodeName} />
      ))}
    </div>
  )
}

function StopMarker({ handle, stop, nodeName }: { handle: MapHandle; stop: ClientRouteStop; nodeName: string }) {
  const { t } = useTranslation()
  const element = useMemo(() => document.createElement('div'), [])

  useEffect(() => {
    const marker = new handle.ml.Marker({ element, anchor: 'left', offset: [-7, 0] })
      .setLngLat([stop.lon, stop.lat])
      .addTo(handle.map)
    return () => { marker.remove() }
  }, [handle, element, stop.lon, stop.lat])

  const color = ROUTE_STOP_COLORS[stop.kind]
  const size = stop.kind === 'router' ? 10 : 14
  const title = stop.kind === 'node' ? nodeName : stop.place ?? stop.country_code ?? '—'
  return createPortal(
    <div className="flex items-center gap-1.5 pointer-events-none">
      <span
        className="block rounded-full bg-[#0b0b0d] shrink-0"
        style={{ width: size, height: size, border: `2px solid ${color}`, boxShadow: `0 0 10px ${color}` }}
      />
      <span className="rounded-md bg-[#16161b]/90 border border-[#2a2a32] px-1.5 py-0.5 text-[11px] text-dark-100 whitespace-nowrap">
        {getFlag(stop.country_code)} {title}
        {stop.kind !== 'node' && stop.rtt_ms !== null && (
          <span className="text-dark-400"> · {t('remnawave.geo.route_ms', { value: Math.round(stop.rtt_ms) })}</span>
        )}
      </span>
    </div>,
    element,
  )
}
