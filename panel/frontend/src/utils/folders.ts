/** Порядок папок как на дашборде: сохранённый пользователем, остальные — по алфавиту */
export function orderFolders(names: string[]): string[] {
  try {
    const saved: string[] = JSON.parse(localStorage.getItem('dashboard_folder_order') || '[]')
    const ordered = saved.filter(f => names.includes(f))
    const rest = names.filter(f => !saved.includes(f)).sort()
    return [...ordered, ...rest]
  } catch {
    return [...names].sort()
  }
}
