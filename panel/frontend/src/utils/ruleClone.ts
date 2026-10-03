const RULE_NAME_MAX_LENGTH = 64
const COPY_SUFFIX_RE = /-copy(-\d+)?$/

/** Свободное имя для копии правила: «vless» → «vless-copy», дальше «vless-copy-2», «vless-copy-3»… */
export function uniqueCopyName(name: string, takenNames: string[]): string {
  const taken = new Set(takenNames)
  const base = name.replace(COPY_SUFFIX_RE, '')
  for (let attempt = 1; ; attempt++) {
    const suffix = attempt === 1 ? '-copy' : `-copy-${attempt}`
    const candidate = base.slice(0, RULE_NAME_MAX_LENGTH - suffix.length) + suffix
    if (!taken.has(candidate)) return candidate
  }
}
