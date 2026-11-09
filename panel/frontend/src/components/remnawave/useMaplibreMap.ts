import { useEffect, useRef, useState, type RefObject } from 'react'
import type { Map as MapLibreMap, MapOptions } from 'maplibre-gl'
import { useTranslation } from 'react-i18next'
import {
  BASEMAP_SOURCE, DBIP_ATTRIBUTION, fallbackStyle, getMapStyle, loadMaplibre, type MapLibreModule,
} from './mapLoader'

export interface MapHandle {
  map: MapLibreMap
  ml: MapLibreModule
}

export function useMaplibreMap(
  containerRef: RefObject<HTMLDivElement>,
  options: Omit<MapOptions, 'container' | 'style'>,
  onStyleLoad: (handle: MapHandle) => void,
) {
  const { i18n } = useTranslation()
  const [handle, setHandle] = useState<MapHandle | null>(null)
  // 0 — стиль ещё не готов: менять слои и данные карты до его загрузки нельзя
  const [styleVersion, setStyleVersion] = useState(0)
  const [isFallback, setIsFallback] = useState(false)
  const [failed, setFailed] = useState(false)
  const onStyleLoadRef = useRef(onStyleLoad)
  onStyleLoadRef.current = onStyleLoad
  const optionsRef = useRef(options)

  useEffect(() => {
    let cancelled = false
    let map: MapLibreMap | null = null

    Promise.all([loadMaplibre(), getMapStyle(i18n.language)])
      .then(([ml, { style, isFallback: styleIsFallback }]) => {
        if (cancelled || !containerRef.current) return
        setIsFallback(styleIsFallback)
        const created = new ml.Map({
          container: containerRef.current,
          style,
          attributionControl: false,
          ...optionsRef.current,
        })
        map = created
        created.addControl(new ml.AttributionControl({ compact: true, customAttribution: DBIP_ATTRIBUTION }))

        // Стиль загрузился, а тайлы — нет (блокировка CDN): переходим на контуры стран,
        // чтобы карта не осталась чёрной. Если подложка уже показала хоть один тайл,
        // отдельные сбои потом не повод её менять
        let basemapLoaded = false
        let switched = styleIsFallback
        created.on('sourcedata', event => {
          if (event.sourceId === BASEMAP_SOURCE && event.tile) basemapLoaded = true
        })
        created.on('error', event => {
          if (switched || basemapLoaded || (event as { sourceId?: string }).sourceId !== BASEMAP_SOURCE) return
          switched = true
          setIsFallback(true)
          setStyleVersion(0)
          created.setStyle(fallbackStyle(), { diff: false })
        })

        const mapHandle = { map: created, ml }
        created.on('style.load', () => {
          onStyleLoadRef.current(mapHandle)
          setStyleVersion(v => v + 1)
        })
        setHandle(mapHandle)
      })
      .catch(() => {
        if (!cancelled) setFailed(true)
      })

    return () => {
      cancelled = true
      map?.remove()
      setHandle(null)
      setStyleVersion(0)
    }
  }, [containerRef, i18n.language])

  return { handle, styleVersion, isFallback, failed }
}

// Развёрнутая подпись источников закрывает полкарты небольшого размера; свёрнутая — кнопка «i»
export function useCollapsedAttribution(handle: MapHandle | null, styleVersion: number) {
  useEffect(() => {
    handle?.map.getContainer().querySelector('.maplibregl-ctrl-attrib')?.classList.remove('maplibregl-compact-show')
  }, [handle, styleVersion])
}

// Колесо мыши зумит карту только после клика по ней — иначе страница с картой
// перестаёт прокручиваться, как только курсор оказывается над картой
export function useScrollZoomAfterClick(handle: MapHandle | null) {
  const [enabled, setEnabled] = useState(false)
  useEffect(() => {
    if (!handle) return
    const { map } = handle
    map.scrollZoom.disable()
    setEnabled(false)
    const enable = () => {
      map.scrollZoom.enable()
      setEnabled(true)
    }
    map.once('mousedown', enable)
    map.once('touchstart', enable)
    return () => {
      map.off('mousedown', enable)
      map.off('touchstart', enable)
    }
  }, [handle])
  return enabled
}
