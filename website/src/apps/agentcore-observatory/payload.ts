/**
 * Turning field NAMES into an editable payload.
 *
 * Both evidence paths — the log scan and a rejected invoke — return field names,
 * not values. This is what makes a name actionable: it adds the key to whatever
 * the operator has already typed, leaving the value blank for them to fill.
 *
 * It never invents a VALUE. A plausible-looking value is the fabrication risk
 * this whole feature is built to avoid: a payload the app filled in would look
 * authoritative while being made up, and an agent answering 200 to it would make
 * the invention look confirmed.
 */

/** A dotted path like `businessData.sourceApp` becomes a nested object. */
function setPath(target: Record<string, unknown>, path: string[]): void {
  const [head, ...rest] = path
  if (!head) return
  if (!rest.length) {
    // Only fill a gap: an existing value the operator typed is never clobbered.
    if (target[head] === undefined) target[head] = ''
    return
  }
  const next = target[head]
  const child = next && typeof next === 'object' && !Array.isArray(next)
    ? (next as Record<string, unknown>)
    : {}
  target[head] = child
  setPath(child, rest)
}

/**
 * Add each named field to `payload`, preserving what is already there.
 *
 * A payload that is not valid JSON is replaced rather than merged into — there
 * is nothing to preserve, and silently discarding the names would leave the
 * operator with a button that appears to do nothing.
 */
export function mergeFields(payload: string, names: readonly string[]): string {
  let base: Record<string, unknown> = {}
  try {
    const parsed = JSON.parse(payload) as unknown
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      base = parsed as Record<string, unknown>
    }
  } catch {
    // Unparseable: start from an empty object rather than dropping the names.
  }
  for (const name of names) {
    const path = name.split('.').filter(Boolean)
    // An indexed path (`messages.0.content`) names a list element. The index is
    // dropped rather than guessed at, because creating a one-element array would
    // assert a length the runtime never stated.
    if (path.some((seg) => /^\d+$/.test(seg))) {
      setPath(base, [path[0]])
      continue
    }
    setPath(base, path)
  }
  return JSON.stringify(base, null, 2)
}
