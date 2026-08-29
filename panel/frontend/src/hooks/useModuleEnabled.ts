import { useSettingsStore } from '../stores/settingsStore'

/** Включён ли раздел панели — для ссылок на него со страниц других разделов */
export function useModuleEnabled(moduleId: string): boolean {
  return useSettingsStore(s => !s.hiddenModules.includes(moduleId))
}
