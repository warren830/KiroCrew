/**
 * Test one agent runtime: build a payload, confirm, invoke, read the reply.
 *
 * Three things this component refuses to do, each because the alternative
 * produces a confident wrong answer:
 *
 * 1. **It never invents a payload that looks like this runtime's schema.**
 *    `get-agent-card` is the only honest source of a runtime's input shape and
 *    it answers only for A2A agents; every other runtime returns
 *    `published: false`. So the textarea is seeded with a GENERIC template that
 *    says so in the UI, and the card is fetched on request and shown verbatim
 *    when it exists. A fabricated schema that returns HTTP 200 is worse than an
 *    empty box, because the 200 makes the guess look correct.
 * 2. **It never invokes on one click.** Invoking runs the agent and bills the
 *    account, and `invoke-agent-runtime` has no idempotency key — reusing a
 *    session id continues a conversation rather than de-duplicating a call — so
 *    the confirmation strip is the only thing preventing a double charge.
 * 3. **It does not prefill the qualifier from a version row.** The qualifier is
 *    an ENDPOINT NAME, not a version number; the endpoint names for this runtime
 *    are listed in the same expanded row, a few lines below this panel.
 */
import { useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { AlertTriangle, Ban, FileQuestion, Play, RotateCcw } from 'lucide-react'
import { Btn, Input } from '../../components/ui'
import { i18nT } from '../../i18n/t'
import { observatoryApi, ObservatoryError, type ActionResult } from './api'
import { mergeFields } from './payload'
import PayloadHintsPanel from './PayloadHintsPanel'

/**
 * The seed payload. Deliberately minimal and obviously generic: most AgentCore
 * samples accept a `prompt` string, but that is a convention rather than a
 * contract, which is exactly why the UI labels it as a template to replace.
 */
export const GENERIC_PAYLOAD = '{\n  "prompt": ""\n}'

const FIELD = 'bg-bg-elevated border border-border rounded-md px-3 py-2 text-sm font-body w-full'

/** Metadata worth surfacing above the reply body, in the order a reader wants it. */
const META_FIELDS: readonly string[] = ['statusCode', 'runtimeSessionId', 'traceId', 'contentType']

/**
 * Split a `text/event-stream` reply into its `data:` payloads.
 *
 * Grounded in the SSE wire format only — events separated by a blank line,
 * payload lines prefixed `data:` — and deliberately NOT in any agent's payload
 * schema. An agent that answers `{"type":"ERROR"}` is using its own convention;
 * sniffing for that would be the same mistake as fabricating an input schema.
 * All this does is stop a multi-event stream rendering as one unbroken line.
 *
 * Returns `[]` when the body carries no `data:` line, so the caller falls back
 * to showing it verbatim rather than showing nothing.
 */
export function sseEvents(body: string): string[] {
  const out: string[] = []
  for (const block of body.split(/\r?\n\r?\n/)) {
    const data = block
      .split(/\r?\n/)
      .filter((line) => line.startsWith('data:'))
      .map((line) => line.slice(5).trimStart())
    if (data.length) out.push(data.join('\n'))
  }
  return out
}

/** Whether the declared content type is a server-sent-event stream. */
export function isEventStream(contentType: unknown): boolean {
  return typeof contentType === 'string' && contentType.toLowerCase().includes('text/event-stream')
}

function Failure({ result }: { result: { denied: boolean; error: string } }) {
  const Icon = result.denied ? Ban : AlertTriangle
  return (
    <div className="flex gap-2 items-start text-sm mt-2">
      <Icon size={16} className="lucide-inline mt-0.5 text-warn shrink-0" />
      <div className="min-w-0">
        <div className="font-medium">
          {result.denied
            ? i18nT('apps.agentcoreObservatory.page.denied_headline')
            : i18nT('apps.agentcoreObservatory.page.invoke_failed')}
        </div>
        <div className="text-muted break-words">{result.error}</div>
      </div>
    </div>
  )
}

export default function RuntimeTestPanel({ runtimeArn }: { runtimeArn: string }) {
  const [payload, setPayload] = useState(GENERIC_PAYLOAD)
  const [qualifier, setQualifier] = useState('')
  const [sessionId, setSessionId] = useState('')
  const [confirming, setConfirming] = useState(false)
  const [wantCard, setWantCard] = useState(false)

  // Fetched only on request: it is one API call per runtime and most runtimes
  // answer "not an A2A agent", so doing it eagerly would spend a call per row to
  // usually learn nothing.
  const card = useQuery({
    queryKey: ['agentcore-observatory', 'agent-card', runtimeArn, qualifier],
    queryFn: () => observatoryApi.getAgentCard(runtimeArn, qualifier),
    enabled: wantCard,
    retry: false,
  })

  const invoke = useMutation({
    mutationFn: () =>
      observatoryApi.invokeRuntime({
        runtimeArn,
        payload,
        qualifier: qualifier.trim(),
        sessionId: sessionId.trim(),
      }),
    onSettled: () => setConfirming(false),
  })

  const result: ActionResult | undefined = invoke.data
  const thrown = invoke.isError
    ? {
        denied: invoke.error instanceof ObservatoryError && invoke.error.status === 403,
        error: invoke.error instanceof ObservatoryError ? invoke.error.message : String(invoke.error),
      }
    : undefined

  const published = card.data?.ok === true && card.data.result?.published === true
  const cardChecked = card.data?.ok === true

  return (
    <section className="mt-3 rounded border border-border p-3">
      <div className="text-sm font-medium mb-1">
        {i18nT('apps.agentcoreObservatory.page.test_runtime')}
      </div>
      {/* Stated up front, not behind the confirm button: the reader decides
          whether to start at all, and a cost disclosed only at the last click is
          disclosed too late. */}
      <p className="text-sm text-muted mb-2">
        {i18nT('apps.agentcoreObservatory.page.invoke_cost_caveat')}
      </p>

      <div className="flex flex-wrap gap-2 items-center mb-2">
        <Btn onClick={() => setWantCard(true)} disabled={card.isFetching}>
          <FileQuestion size={16} className="lucide-inline" />
          {i18nT('apps.agentcoreObservatory.page.check_agent_card')}
        </Btn>
        {cardChecked && !published && (
          <span className="text-sm text-muted">
            {i18nT('apps.agentcoreObservatory.page.no_card_published')}
          </span>
        )}
      </div>
      {card.data && !card.data.ok && <Failure result={card.data} />}
      {published && (
        <pre className="text-xs bg-bg rounded border border-border p-3 overflow-x-auto max-h-60 mb-2">
          {JSON.stringify(card.data?.result?.card ?? {}, null, 2)}
        </pre>
      )}

      {/* The second evidence source, offered beside the first: when no card is
          published the runtime's own log group is the next best witness to the
          input it accepts. */}
      <PayloadHintsPanel
        runtimeArn={runtimeArn}
        onApplyFields={(names) => setPayload((current) => mergeFields(current, names))}
      />

      <label className="block text-sm mb-1" htmlFor={`payload-${runtimeArn}`}>
        {i18nT('apps.agentcoreObservatory.page.payload_label')}
      </label>
      {/* The label says "template" whenever no card was found, so the reader is
          never left believing this shape came from the runtime. */}
      <p className="text-xs text-muted mb-1">
        {published
          ? i18nT('apps.agentcoreObservatory.page.payload_from_card')
          : i18nT('apps.agentcoreObservatory.page.payload_is_generic')}
      </p>
      <textarea
        id={`payload-${runtimeArn}`}
        className={`${FIELD} resize-y font-mono`}
        rows={5}
        value={payload}
        onChange={(e) => setPayload(e.target.value)}
        spellCheck={false}
      />

      <div className="flex flex-wrap gap-2 items-end mt-2">
        <div className="min-w-0 flex-1">
          <label className="block text-xs text-muted mb-1" htmlFor={`qualifier-${runtimeArn}`}>
            {i18nT('apps.agentcoreObservatory.page.qualifier_label')}
          </label>
          <Input
            id={`qualifier-${runtimeArn}`}
            value={qualifier}
            onChange={(e) => setQualifier(e.target.value)}
            placeholder={i18nT('apps.agentcoreObservatory.page.qualifier_placeholder')}
          />
        </div>
        <div className="min-w-0 flex-1">
          <label className="block text-xs text-muted mb-1" htmlFor={`session-${runtimeArn}`}>
            {i18nT('apps.agentcoreObservatory.page.session_label')}
          </label>
          <Input
            id={`session-${runtimeArn}`}
            value={sessionId}
            onChange={(e) => setSessionId(e.target.value)}
            placeholder={i18nT('apps.agentcoreObservatory.page.session_placeholder')}
          />
        </div>
      </div>

      <div className="flex flex-wrap gap-2 items-center mt-3">
        {confirming ? (
          <>
            <span className="text-sm text-warn">
              {i18nT('apps.agentcoreObservatory.page.invoke_confirm_question')}
            </span>
            <Btn onClick={() => invoke.mutate()} disabled={invoke.isPending}>
              <Play size={16} className="lucide-inline" />
              {i18nT('apps.agentcoreObservatory.page.invoke_confirm')}
            </Btn>
            <Btn onClick={() => setConfirming(false)} disabled={invoke.isPending}>
              {i18nT('apps.agentcoreObservatory.page.cancel')}
            </Btn>
          </>
        ) : (
          <Btn onClick={() => setConfirming(true)} disabled={!payload.trim() || invoke.isPending}>
            <Play size={16} className="lucide-inline" />
            {i18nT('apps.agentcoreObservatory.page.invoke_action')}
          </Btn>
        )}
        {result?.ok && (
          <Btn
            onClick={() => {
              invoke.reset()
              setConfirming(false)
            }}
          >
            <RotateCcw size={16} className="lucide-inline" />
            {i18nT('apps.agentcoreObservatory.page.clear_result')}
          </Btn>
        )}
      </div>

      {thrown && <Failure result={thrown} />}
      {result && !result.ok && <Failure result={result} />}
      {result?.ok && (
        <div className="mt-3">
          <dl className="text-sm grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 mb-1">
            {META_FIELDS.filter((f) => result.result[f] !== undefined).map((field) => (
              <div key={field} className="contents">
                <dt className="text-muted">{field}</dt>
                <dd className="font-mono text-xs break-all">{String(result.result[field])}</dd>
              </div>
            ))}
          </dl>
          {/* A 200 here means AgentCore accepted and routed the call. The agent's
              own outcome is in the body, and it can perfectly well be a failure
              under a 200 — so the number is labelled rather than left to read as
              "it worked". */}
          <p className="text-xs text-muted mb-2">
            {i18nT('apps.agentcoreObservatory.page.status_is_transport')}
          </p>
          {/* The runtime rejected the payload and named what it wanted. One click
              turns that into the corrected payload, so the failed call is the
              answer rather than a trip to the runtime's source.

              Read defensively: this value crosses an HTTP boundary, and a
              gateway a version behind would omit it. The cost of assuming it is
              present is a blank panel, which is far worse than no hint. */}
          {(result.derived_fields ?? []).length > 0 && (
            <div className="mb-2 rounded border border-border p-2">
              <div className="text-sm font-medium">
                {i18nT('apps.agentcoreObservatory.page.reply_named_fields')}
              </div>
              <div className="flex flex-wrap gap-2 items-center mt-1">
                {(result.derived_fields ?? []).map((name) => (
                  <code
                    key={name}
                    className="text-xs bg-bg rounded border border-border px-1.5 py-0.5"
                  >
                    {name}
                  </code>
                ))}
                <Btn
                  onClick={() => setPayload((c) => mergeFields(c, result.derived_fields ?? []))}
                >
                  {i18nT('apps.agentcoreObservatory.page.apply_fields')}
                </Btn>
              </div>
            </div>
          )}
          {result.body_truncated && (
            <div className="text-sm text-warn mb-1">
              {i18nT('apps.agentcoreObservatory.page.reply_truncated')}
            </div>
          )}
          {/* Rendered verbatim as text: the agent chose the format, and parsing it
              as JSON would turn a plain-text or SSE answer into a false error. An
              event stream is split per event for readability only — the payload
              inside each event is still shown exactly as it arrived. */}
          {(() => {
            const events = isEventStream(result.result.contentType)
              ? sseEvents(result.body)
              : []
            if (events.length) {
              return (
                <ol className="text-xs bg-bg rounded border border-border p-3 overflow-x-auto max-h-80 space-y-1">
                  {events.map((event, i) => (
                    <li key={i} className="font-mono whitespace-pre-wrap break-all">
                      {event}
                    </li>
                  ))}
                </ol>
              )
            }
            return (
              <pre className="text-xs bg-bg rounded border border-border p-3 overflow-x-auto max-h-80 whitespace-pre-wrap">
                {result.body || i18nT('apps.agentcoreObservatory.page.empty_reply')}
              </pre>
            )
          })()}
        </div>
      )}
    </section>
  )
}
