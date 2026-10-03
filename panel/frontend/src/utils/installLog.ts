// Лог install.sh приходит с ANSI-кодами и \r-перерисовкой спиннеров —
// берём последний сегмент после \r и вырезаем управляющие последовательности
export const cleanInstallLogLine = (line: string): string => {
  const visible = line.split('\r').pop() ?? line
  // eslint-disable-next-line no-control-regex
  return visible.replace(/\x1b\[[0-9;]*[a-zA-Z]/g, '').replace(/\x1b/g, '').trimEnd()
}
