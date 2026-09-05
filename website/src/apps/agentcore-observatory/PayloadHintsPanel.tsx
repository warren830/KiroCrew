/**
 * What this runtime's own log group says about the input it accepts.
 *
 * The three findings are rendered as three DIFFERENT things on purpose, because
 * they carry different weight and collapsing them would be the fabrication
 * problem in a new costume:
 *
 * - **Required fields** are what the runtime said when it rejected a request.
 *   Facts. Offered as a one-click merge into the payload.
 * - **Rejected bodies** are what somebody actually sent. Shown as excerpts, not
 *   as payloads to submit — they are a Python repr, and one was rejected.
 * - **Observed fields** are keys of JSON objects the runtime logged. Weaker: a
 *   logged object may be an internal record, so each one shows the line it came
 *   from and the operator decides.
 *
 * An empty result is stated as "nothing observed", never filled in with a guess.
 */
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { AlertTriangle, Ban, ScanSearch } from 'lucide-react'
import { Btn } from '../../components/ui'
import { i18nT } from '../../i18n/t'
import { observatoryApi, ObservatoryError } from './api'

export default function PayloadHintsPanel({
  runtimeArn,
  onApplyFields,
}: {
  runtimeArn: string
  onApplyFields: (names: string[]) => void
}) {
  const [wanted, setWanted] = useState(false)
  const hints = useQuery({
    queryKey: ['agentcore-observatory', 'payload-hints', runtimeArn],
    queryFn: () => observatoryApi.getPayloadHints(runtimeArn),
    enabled: wanted,
    retry: false,
  })

  const thrownDenied =
    hints.isError && hints.error instanceof ObservatoryError && hints.error.status === 403
  const data = hints.data
  const nothing =
    data?.ok &&
    !data.requiredFields.length &&
    !data.rejectedExamples.length &&
    !data.observedFields.length

  return (
    <div className="mt-2">
      <Btn onClick={() => setWanted(true)} disabled={hints.isFetching}>
        <ScanSearch size={16} className="lucide-inline" />
        {i18nT('apps.agentcoreObservatory.page.derive_payload')}
      </Btn>

      {(thrownDenied || data?.denied) && (
        <div className="flex gap-2 items-start text-sm mt-2">
          <Ban size={16} className="lucide-inline mt-0.5 text-warn shrink-0" />
          <div className="min-w-0">
            <div className="font-medium">
              {i18nT('apps.agentcoreObservatory.page.denied_headline')}
            </div>
            {/* Names the permission, because this is the ONE call that needs a
                grant outside bedrock-agentcore and the operator cannot guess it. */}
            <div className="text-muted">
              {i18nT('apps.agentcoreObservatory.page.hints_needs_permission')}
            </div>
          </div>
        </div>
      )}

      {hints.isError && !thrownDenied && (
        <div className="flex gap-2 items-start text-sm mt-2">
          <AlertTriangle size={16} className="lucide-inline mt-0.5 text-warn shrink-0" />
          <div className="text-muted break-words">
            {hints.error instanceof ObservatoryError ? hints.error.message : String(hints.error)}
          </div>
        </div>
      )}

      {data && !data.ok && !data.denied && (
        <div className="flex gap-2 items-start text-sm mt-2">
          <AlertTriangle size={16} className="lucide-inline mt-0.5 text-warn shrink-0" />
          <div className="min-w-0">
            <div className="text-muted break-words">{data.error}</div>
          </div>
        </div>
      )}

      {data?.ok && (
        <div className="mt-2 text-sm">
          {!data.logGroup ? (
            // No log group at all: a fact about the runtime, like an unpublished
            // agent card. Not an error, and not filled in with a guess.
            <p className="text-muted">
              {i18nT('apps.agentcoreObservatory.page.hints_no_log_group')}
            </p>
          ) : nothing ? (
            <p className="text-muted">{i18nT('apps.agentcoreObservatory.page.hints_nothing')}</p>
          ) : (
            <div className="space-y-3">
              {data.requiredFields.length > 0 && (
                <div>
                  <div className="font-medium">
                    {i18nT('apps.agentcoreObservatory.page.hints_required')}
                  </div>
                  <div className="flex flex-wrap gap-2 items-center mt-1">
                    {data.requiredFields.map((name) => (
                      <code key={name} className="text-xs bg-bg rounded border border-border px-1.5 py-0.5">
                        {name}
                      </code>
                    ))}
                    <Btn onClick={() => onApplyFields(data.requiredFields)}>
                      {i18nT('apps.agentcoreObservatory.page.apply_fields')}
                    </Btn>
                  </div>
                </div>
              )}

              {data.rejectedExamples.length > 0 && (
                <div>
                  <div className="font-medium">
                    {i18nT('apps.agentcoreObservatory.page.hints_rejected')}
                  </div>
                  {data.rejectedExamples.map((example, i) => (
                    <pre
                      key={i}
                      className="text-xs bg-bg rounded border border-border p-2 mt-1 overflow-x-auto whitespace-pre-wrap"
                    >
                      {example}
                    </pre>
                  ))}
                </div>
              )}

              {data.observedFields.length > 0 && (
                <div>
                  <div className="font-medium">
                    {i18nT('apps.agentcoreObservatory.page.hints_observed')}
                  </div>
                  {/* Listed as information, with NO merge button. A live run
                      offered one here and the operator ended up with a telemetry
                      key in their payload — worse than the generic template it
                      replaced. Only evidence the runtime STATED (required fields,
                      or the fields a rejection named) is safe to one-click. */}
                  <p className="text-xs text-muted">
                    {i18nT('apps.agentcoreObservatory.page.hints_observed_weak')}
                  </p>
                  <ul className="mt-1 space-y-1">
                    {data.observedFields.map((f) => (
                      <li key={f.name} className="min-w-0">
                        <code className="text-xs">{f.name}</code>
                        {/* The excerpt is the whole point: it lets a human see
                            whether the key came from a request or from an
                            internal record. */}
                        <div className="text-xs text-muted truncate">{f.excerpt}</div>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          )}
          {/* Stated even on a good result: the sources cannot see an optional
              field that is never logged, so "nothing more here" does not mean
              "this is the whole contract". */}
          <p className="text-xs text-muted mt-2">
            {i18nT('apps.agentcoreObservatory.page.hints_limit')}
          </p>
        </div>
      )}
    </div>
  )
}
