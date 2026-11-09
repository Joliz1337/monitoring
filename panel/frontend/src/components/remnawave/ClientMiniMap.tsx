import { useEffect, useMemo, useRef } from 'react'
import { useTranslation } from 'react-i18next'
import type { GeoJSONSource } from 'maplibre-gl'
import { Loader2, MapPinOff } from 'lucide-react'
import type { RemnawaveUserHistoryIp, RemnawaveUserNode } from '../../api/client'
import { COMPARE_COLORS } from './geo'
import { LABEL_FONT } from './mapLoader'
import { useCollapsedAttribution, useMaplibreMap, useScrollZoomAfterClick, type MapHandle } from './useMaplibreMap'

const NODE_COLOR = '#a78bfa'
const PAST_COLOR = '#64748b'
const ARC_SEGMENTS = 24
const ARC_BEND = 0.2
const FIT_PADDING = 30
const FIT_MAX_ZOOM = 6
const SRC = { ips: 'rw-user-ips', nodes: 'rw-user-nodes', arcs: 'rw-user-arcs' } as const

type LngLat = [number, number]

interface ClientMiniMapProps {
  history: RemnawaveUserHistoryIp[]
  nodes: RemnawaveUserNode[]
}

// Дуга вместо прямой: линии от нескольких IP к одной ноде иначе слипаются в одну
function arc(from: LngLat, to: LngLat): LngLat[] {
  const [x1, y1] = from
  const [x2, y2] = to
  const control: LngLat = [(x1 + x2) / 2 - (y2 - y1) * ARC_BEND, (y1 + y2) / 2 + (x2 - x1) * ARC_BEND]
  const points: LngLat[] = []
  for (let i = 0; i <= ARC_SEGMENTS; i++) {
    const s = i / ARC_SEGMENTS
    const a = (1 - s) * (1 - s)
    const b = 2 * (1 - s) * s
    const c = s * s
    points.push([a * x1 + b * control[0] + c * x2, a * y1 + b * control[1] + c * y2])
  }
  return points
}

function buildCollections(history: RemnawaveUserHistoryIp[], nodes: RemnawaveUserNode[]) {
  const located = history.filter(h => h.lat !== null && h.lon !== null)
  const nodeCoords = new Map<string, LngLat>(
    nodes.filter(n => n.lat !== null && n.lon !== null).map(n => [n.uuid, [n.lon as number, n.lat as number]]),
  )
  const ips: GeoJSON.FeatureCollection = {
    type: 'FeatureCollection',
    features: located.map(h => ({
      type: 'Feature',
      geometry: { type: 'Point', coordinates: [h.lon as number, h.lat as number] },
      properties: { online: h.online, ip: h.source_ip },
    })),
  }
  const nodeFeatures: GeoJSON.FeatureCollection = {
    type: 'FeatureCollection',
    features: nodes.filter(n => nodeCoords.has(n.uuid)).map(n => ({
      type: 'Feature',
      geometry: { type: 'Point', coordinates: nodeCoords.get(n.uuid) as LngLat },
      properties: { name: n.name ?? n.uuid.slice(0, 8) },
    })),
  }
  const arcs: GeoJSON.FeatureCollection = {
    type: 'FeatureCollection',
    features: located.flatMap(h => h.nodes
      .filter(uuid => nodeCoords.has(uuid))
      .map(uuid => ({
        type: 'Feature' as const,
        geometry: {
          type: 'LineString' as const,
          coordinates: arc([h.lon as number, h.lat as number], nodeCoords.get(uuid) as LngLat),
        },
        properties: { online: h.online },
      }))),
  }
  const allCoords: LngLat[] = [
    ...located.map(h => [h.lon as number, h.lat as number] as LngLat),
    ...nodeCoords.values(),
  ]
  return { ips, nodes: nodeFeatures, arcs, allCoords, hasLocation: located.length > 0 }
}

function addLayers({ map }: MapHandle) {
  map.addSource(SRC.arcs, { type: 'geojson', data: { type: 'FeatureCollection', features: [] } })
  map.addSource(SRC.ips, { type: 'geojson', data: { type: 'FeatureCollection', features: [] } })
  map.addSource(SRC.nodes, { type: 'geojson', data: { type: 'FeatureCollection', features: [] } })
  map.addLayer({
    id: 'rw-user-arcs', type: 'line', source: SRC.arcs,
    paint: {
      'line-color': NODE_COLOR,
      'line-width': 1.2,
      'line-opacity': ['case', ['get', 'online'], 0.75, 0.3],
    },
  })
  map.addLayer({
    id: 'rw-user-ips', type: 'circle', source: SRC.ips,
    paint: {
      'circle-radius': ['case', ['get', 'online'], 6, 4.5],
      'circle-color': ['case', ['get', 'online'], COMPARE_COLORS.normal, PAST_COLOR],
      'circle-stroke-color': '#0b0b0d',
      'circle-stroke-width': 1.5,
    },
  })
  map.addLayer({
    id: 'rw-user-nodes', type: 'circle', source: SRC.nodes,
    paint: {
      'circle-radius': 5.5,
      'circle-color': NODE_COLOR,
      'circle-stroke-color': '#ffffff',
      'circle-stroke-width': 1,
    },
  })
  map.addLayer({
    id: 'rw-user-node-names', type: 'symbol', source: SRC.nodes,
    layout: {
      'text-field': ['get', 'name'],
      'text-font': LABEL_FONT,
      'text-size': 10,
      'text-anchor': 'left',
      'text-offset': [0.8, 0],
    },
    paint: { 'text-color': '#c4b5fd', 'text-halo-color': '#0b0b0d', 'text-halo-width': 1 },
  })
}

export default function ClientMiniMap({ history, nodes }: ClientMiniMapProps) {
  const { t } = useTranslation()
  const containerRef = useRef<HTMLDivElement>(null)
  const collections = useMemo(() => buildCollections(history, nodes), [history, nodes])

  const mapOptions = useMemo(() => ({
    center: [45, 52] as LngLat,
    zoom: 2,
    minZoom: 1,
    maxZoom: 10,
    dragRotate: false,
    pitchWithRotate: false,
    touchPitch: false,
  }), [])

  const { handle, styleVersion } = useMaplibreMap(containerRef, mapOptions, addLayers)
  useScrollZoomAfterClick(handle)
  useCollapsedAttribution(handle, styleVersion)

  useEffect(() => {
    const map = handle?.map
    if (!map || !styleVersion) return
    map.getSource<GeoJSONSource>(SRC.ips)?.setData(collections.ips)
    map.getSource<GeoJSONSource>(SRC.nodes)?.setData(collections.nodes)
    map.getSource<GeoJSONSource>(SRC.arcs)?.setData(collections.arcs)
    const coords = collections.allCoords
    if (coords.length === 0) return
    const lons = coords.map(c => c[0])
    const lats = coords.map(c => c[1])
    map.fitBounds(
      [[Math.min(...lons), Math.min(...lats)], [Math.max(...lons), Math.max(...lats)]],
      { padding: FIT_PADDING, maxZoom: FIT_MAX_ZOOM, duration: 0 },
    )
  }, [handle, styleVersion, collections])

  if (!collections.hasLocation) {
    return (
      <div className="h-[220px] rounded-xl border border-dark-800/60 bg-dark-900/40 flex flex-col items-center justify-center gap-2 text-xs text-dark-500 text-center px-4">
        <MapPinOff className="w-5 h-5" />
        {t('remnawave.geo.user_no_location')}
      </div>
    )
  }

  return (
    <div className="space-y-2">
      <div className="relative rw-map rounded-xl overflow-hidden border border-dark-800/60 bg-[#0b0b0d]">
        <div ref={containerRef} className="h-[220px] w-full" />
        {!handle && (
          <div className="absolute inset-0 flex items-center justify-center">
            <Loader2 className="w-5 h-5 text-dark-500 animate-spin" />
          </div>
        )}
      </div>
      <div className="flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-dark-400">
        <Legend color={COMPARE_COLORS.normal} label={t('remnawave.geo.legend_ip_now')} />
        <Legend color={PAST_COLOR} label={t('remnawave.geo.legend_ip_past')} />
        <Legend color={NODE_COLOR} label={t('remnawave.geo.legend_node')} />
      </div>
    </div>
  )
}

function Legend({ color, label }: { color: string; label: string }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className="w-2.5 h-2.5 rounded-full" style={{ background: color }} />
      {label}
    </span>
  )
}
