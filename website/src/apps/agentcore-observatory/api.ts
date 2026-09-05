/**
 * AgentCore Observatory API client — a thin same-origin fetch wrapper.
 *
 * Mirrors the aws-control client: it prefers the response body's machine
 * readable `code` over the untranslated English `error` prose, so the UI has a
 * stable token to localise. Every AWS-facing endpoint is a read; the one write
 * saves a profile NAME and a region to the app's own data dir.
 *
 * `getCatalog` makes no AWS call at all — it answers from the backend's
 * in-process resource table. That is what keeps first paint instant with 27
 * resource types: a type is fetched only when its rail item is opened.
 */

const BASE = '/api/apps/agentcore-observatory'

/** Error carrying the backend's machine-readable `code` (e.g. `app_disabled`). */
export class ObservatoryError extends Error {
  readonly status: number
  constructor(code: string, status: number) {
    super(code)
    this.name = 'ObservatoryError'
    this.status = status
  }
}

/** The saved connection. `configured` is false until a region is set. */
export interface ObservatoryConfig {
  profile: string
  region: string
  configured: boolean
}

/**
 * One paginated read.
 *
 * `ok: true` with an empty `items` is an authorized account with nothing
 * deployed — a distinct state from `ok: false`, and the UI must not collapse
 * them. `truncated` means `items` is a partial page and must never be presented
 * as a total.
 */
export interface ListResult {
  ok: boolean
  items: Record<string, unknown>[]
  error: string
  denied: boolean
  truncated: boolean
}

/** One `get-*` read: a single object rather than a list. */
export interface ObjectResult {
  ok: boolean
  item: Record<string, unknown>
  error: string
  denied: boolean
}

/** A child type and the flags its query needs, as the catalog declares them. */
export interface ChildType {
  id: string
  /** CLI flags, e.g. `['--gateway-identifier']`. */
  parentParams: string[]
  /** Parent response fields supplying them, positionally paired. */
  parentFields: string[]
}

export interface RootType {
  id: string
  /** False for a get-only singleton such as `token-vault`. */
  listable: boolean
  /** Response field holding a row's own identifier; '' for a singleton. */
  idField: string
  children: ChildType[]
}

export interface CatalogGroup {
  id: string
  types: RootType[]
}

export interface Catalog {
  config: ObservatoryConfig
  groups: CatalogGroup[]
}

/** A listable type answers with `list`; a singleton answers with `singleton`. */
export interface ResourceResponse {
  type: string
  list?: ListResult
  singleton?: ObjectResult
}

export interface DetailResponse {
  type: string
  detail: ObjectResult
}

/**
 * The outcome of one action.
 *
 * `body` is the agent's own reply. It arrives in a file on the backend rather
 * than on stdout and may not be JSON at all — an agent may answer plain text or
 * a server-sent-event stream — so it is carried as text for the UI to show
 * verbatim. `bodyTruncated` means the reply was longer than the backend will
 * hand a browser, and the text must not be presented as complete.
 *
 * `result` is the metadata AWS returned: `runtimeSessionId`, `traceId` and
 * `statusCode` for an invoke, `batchEvaluationId` for a started evaluation, or
 * `{ published: false }` for a runtime that publishes no agent card.
 */
export interface ActionResult {
  ok: boolean
  result: Record<string, unknown>
  body: string
  body_truncated: boolean
  /**
   * Field names the reply itself said were required and missing.
   *
   * Non-empty only when the runtime rejected the payload with a structured
   * validation error. Empty means the reply named no fields — never that the
   * payload was accepted.
   */
  derived_fields: string[]
  error: string
  denied: boolean
}

/** One candidate field name, with the log line it was observed in. */
export interface FieldEvidence {
  name: string
  excerpt: string
}

/**
 * What a runtime's own log group reveals about the input it accepts.
 *
 * Ranked by trust, and the UI must keep them distinguishable:
 * `requiredFields` are facts the runtime stated when it rejected a request;
 * `rejectedExamples` are bodies somebody actually sent (a Python repr, shown as
 * an excerpt rather than as a payload to submit); `observedFields` are weaker —
 * keys of JSON objects the runtime logged, each with its `excerpt` so a human
 * can judge whether it was a request at all.
 *
 * `ok: true` with everything empty is a successful scan that found nothing, and
 * an empty `logGroup` means the runtime has no log group. Both are facts about
 * the runtime, not failures.
 */
export interface PayloadHints {
  ok: boolean
  logGroup: string
  requiredFields: string[]
  rejectedExamples: string[]
  observedFields: FieldEvidence[]
  scannedEvents: number
  error: string
  denied: boolean
}

/** What an invoke needs. `payload` is always the user's own text. */
export interface InvokeRequest {
  runtimeArn: string
  payload: string
  qualifier?: string
  sessionId?: string
}

/** What starting a batch evaluation needs. `serviceName` is exactly one. */
export interface BatchEvaluationRequest {
  name: string
  evaluatorIds: string[]
  serviceName: string
  logGroupNames: string[]
  startTime?: string
  endTime?: string
  sessionIds?: string[]
  description?: string
  clientToken?: string
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  })
  if (!res.ok) {
    // A non-2xx body is expected to carry `code`; fall back to the status when a
    // proxy or crash produced a body this contract does not cover.
    let code = `http_${res.status}`
    try {
      const body = (await res.json()) as { code?: string }
      if (body?.code) code = body.code
    } catch {
      // Body was not JSON — the status-derived code above is the best available.
    }
    throw new ObservatoryError(code, res.status)
  }
  return (await res.json()) as T
}

/** Build `?flag=value` pairs from catalog flags (`--x` becomes `x`). */
function parentQuery(parentIds: Record<string, string>): string {
  const pairs = Object.entries(parentIds)
    .filter(([, value]) => value)
    .map(([flag, value]) => `${encodeURIComponent(flag.replace(/^--/, ''))}=${encodeURIComponent(value)}`)
  return pairs.length ? `?${pairs.join('&')}` : ''
}

export const observatoryApi = {
  getConfig: () => request<ObservatoryConfig>('/config'),

  saveConfig: (profile: string, region: string) =>
    request<ObservatoryConfig>('/config', {
      method: 'PUT',
      body: JSON.stringify({ profile, region }),
    }),

  getProfiles: () => request<{ profiles: { name: string; region: string }[] }>('/profiles'),

  getCatalog: () => request<Catalog>('/catalog'),

  getResource: (typeId: string, parentIds: Record<string, string> = {}) =>
    request<ResourceResponse>(
      `/resource/${encodeURIComponent(typeId)}${parentQuery(parentIds)}`,
    ),

  getDetail: (typeId: string, idArgs: Record<string, string>) =>
    request<DetailResponse>(
      `/resource/${encodeURIComponent(typeId)}/detail${parentQuery(idArgs)}`,
    ),

  /**
   * A runtime's published agent card, or `{ published: false }`.
   *
   * A free read, so no confirmation. When `published` is false the caller must
   * label whatever example payload it shows as a generic template — this API is
   * the only honest source of a runtime's real input schema, and most runtimes
   * do not publish one.
   */
  getAgentCard: (runtimeArn: string, qualifier = '') =>
    request<ActionResult>(
      `/agent-card?runtimeArn=${encodeURIComponent(runtimeArn)}${
        qualifier ? `&qualifier=${encodeURIComponent(qualifier)}` : ''
      }`,
    ),

  /**
   * Scan the runtime's OWN log group for what input it accepts.
   *
   * A read, and the only call in this client that reaches CloudWatch Logs rather
   * than AgentCore — so a denial here names a permission the operator has to
   * grant separately, and the UI says which one.
   */
  getPayloadHints: (runtimeArn: string, lookbackHours?: number) =>
    request<PayloadHints>(
      `/payload-hints?runtimeArn=${encodeURIComponent(runtimeArn)}${
        lookbackHours ? `&lookbackHours=${lookbackHours}` : ''
      }`,
    ),

  /**
   * Invoke a runtime. Costs money and runs the agent for real.
   *
   * `confirm: true` is not decoration: the backend refuses the request without
   * it, because nothing below the route can distinguish a deliberate click from
   * a retry loop. There is no idempotency key to offer — `invoke-agent-runtime`
   * has none, and reusing a session id continues a conversation rather than
   * de-duplicating a call — so the caller must not send this automatically.
   */
  invokeRuntime: (req: InvokeRequest) =>
    request<ActionResult>('/action/invoke', {
      method: 'POST',
      body: JSON.stringify({ ...req, confirm: true }),
    }),

  /**
   * Start a batch evaluation over spans AWS reads from CloudWatch Logs.
   *
   * The most expensive action here and the only one whose cost is invisible
   * afterwards: evaluator token spend appears in no log or span field, so a
   * duplicate job cannot be detected, only prevented. The backend always sends a
   * `clientToken`; pass one through to make a retry of the SAME job safe.
   */
  startBatchEvaluation: (req: BatchEvaluationRequest) =>
    request<ActionResult>('/action/batch-evaluation', {
      method: 'POST',
      body: JSON.stringify({ ...req, confirm: true }),
    }),
}
