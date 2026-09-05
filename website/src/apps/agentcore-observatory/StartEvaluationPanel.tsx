/**
 * Start a batch evaluation over session spans AWS reads from CloudWatch Logs.
 *
 * This is the most expensive thing the app can do and the only action whose cost
 * is invisible afterwards: evaluator token spend appears in no log or span field
 * this app can read, so a duplicate job cannot be detected later — only
 * prevented. Hence a two-step confirmation, and a `clientToken` the backend
 * always sends so that retrying THIS submission is safe.
 *
 * The form mirrors the API's real bounds rather than a friendlier guess, because
 * every one of them is enforced server-side after the user has already
 * confirmed a paid action:
 *
 * - the name pattern is `[a-zA-Z][a-zA-Z0-9_]{0,47}` — **no hyphens**, which is
 *   what the obvious `nightly-regression` uses;
 * - `serviceNames` is min 1 **max 1**, so this is one field and not a list;
 * - at most 5 log groups and at most 10 evaluators;
 * - a time range needs both bounds, since one alone silently widens the scope.
 *
 * Spans are pulled by AWS from the named log groups, which is why this app needs
 * no Logs Insights query and never handles span data itself.
 */
import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Ban, FlaskConical } from 'lucide-react'
import { Btn, Input } from '../../components/ui'
import { i18nT } from '../../i18n/t'
import { observatoryApi, ObservatoryError, type ActionResult } from './api'

/** Mirrors the service's `batchEvaluationName` pattern exactly. */
export const EVAL_NAME_RE = /^[a-zA-Z][a-zA-Z0-9_]{0,47}$/

export const MAX_LOG_GROUPS = 5
export const MAX_EVALUATORS = 10

/** Split a comma/newline separated field into trimmed, non-empty entries. */
export function splitList(text: string): string[] {
  return text
    .split(/[\n,]/)
    .map((item) => item.trim())
    .filter(Boolean)
}

/**
 * Why this submission cannot be sent yet, as a catalog key, or '' when it can.
 *
 * Returning the reason rather than a boolean is what lets the UI say which bound
 * was missed instead of greying a button out with no explanation.
 */
export function submitProblem(fields: {
  name: string
  evaluators: string[]
  serviceName: string
  logGroups: string[]
  startTime: string
  endTime: string
}): string {
  if (!EVAL_NAME_RE.test(fields.name)) return 'apps.agentcoreObservatory.page.eval_name_invalid'
  if (fields.evaluators.length === 0) return 'apps.agentcoreObservatory.page.eval_need_evaluator'
  if (fields.evaluators.length > MAX_EVALUATORS)
    return 'apps.agentcoreObservatory.page.eval_too_many_evaluators'
  if (!fields.serviceName.trim()) return 'apps.agentcoreObservatory.page.eval_need_service'
  if (fields.logGroups.length === 0) return 'apps.agentcoreObservatory.page.eval_need_log_group'
  if (fields.logGroups.length > MAX_LOG_GROUPS)
    return 'apps.agentcoreObservatory.page.eval_too_many_log_groups'
  // One bound alone would widen a paid job's scope without saying so.
  if (Boolean(fields.startTime.trim()) !== Boolean(fields.endTime.trim()))
    return 'apps.agentcoreObservatory.page.eval_need_both_bounds'
  return ''
}

const FIELD = 'bg-bg-elevated border border-border rounded-md px-3 py-2 text-sm font-body w-full'

export default function StartEvaluationPanel() {
  const queryClient = useQueryClient()
  const [name, setName] = useState('')
  const [evaluatorText, setEvaluatorText] = useState('Builtin.Correctness\nBuiltin.Helpfulness')
  const [serviceName, setServiceName] = useState('')
  const [logGroupText, setLogGroupText] = useState('')
  const [startTime, setStartTime] = useState('')
  const [endTime, setEndTime] = useState('')
  const [confirming, setConfirming] = useState(false)

  const evaluators = splitList(evaluatorText)
  const logGroups = splitList(logGroupText)
  const problem = submitProblem({
    name,
    evaluators,
    serviceName,
    logGroups,
    startTime,
    endTime,
  })

  const start = useMutation({
    mutationFn: () =>
      observatoryApi.startBatchEvaluation({
        name,
        evaluatorIds: evaluators,
        serviceName: serviceName.trim(),
        logGroupNames: logGroups,
        startTime: startTime.trim(),
        endTime: endTime.trim(),
      }),
    onSuccess: () => {
      // The new job belongs in the list right above this form.
      void queryClient.invalidateQueries({
        queryKey: ['agentcore-observatory', 'resource', 'batch-evaluations'],
      })
    },
    onSettled: () => setConfirming(false),
  })

  const result: ActionResult | undefined = start.data
  const thrown = start.isError
    ? {
        denied: start.error instanceof ObservatoryError && start.error.status === 403,
        error: start.error instanceof ObservatoryError ? start.error.message : String(start.error),
      }
    : undefined
  const failure = thrown ?? (result && !result.ok ? result : undefined)

  return (
    <section className="mt-4 rounded border border-border p-3">
      <div className="text-sm font-medium mb-1">
        {i18nT('apps.agentcoreObservatory.page.start_evaluation')}
      </div>
      <p className="text-sm text-muted mb-3">
        {i18nT('apps.agentcoreObservatory.page.eval_cost_caveat')}
      </p>

      <div className="grid gap-2 md:grid-cols-2">
        <div>
          <label className="block text-xs text-muted mb-1" htmlFor="eval-name">
            {i18nT('apps.agentcoreObservatory.page.eval_name_label')}
          </label>
          <Input
            id="eval-name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder={i18nT('apps.agentcoreObservatory.page.eval_name_placeholder')}
          />
        </div>
        <div>
          <label className="block text-xs text-muted mb-1" htmlFor="eval-service">
            {i18nT('apps.agentcoreObservatory.page.eval_service_label')}
          </label>
          <Input
            id="eval-service"
            value={serviceName}
            onChange={(e) => setServiceName(e.target.value)}
            placeholder={i18nT('apps.agentcoreObservatory.page.eval_service_placeholder')}
          />
        </div>
      </div>

      <div className="grid gap-2 md:grid-cols-2 mt-2">
        <div>
          <label className="block text-xs text-muted mb-1" htmlFor="eval-evaluators">
            {i18nT('apps.agentcoreObservatory.page.eval_evaluators_label')}
          </label>
          <textarea
            id="eval-evaluators"
            className={`${FIELD} resize-y font-mono`}
            rows={3}
            value={evaluatorText}
            onChange={(e) => setEvaluatorText(e.target.value)}
            spellCheck={false}
          />
        </div>
        <div>
          <label className="block text-xs text-muted mb-1" htmlFor="eval-log-groups">
            {i18nT('apps.agentcoreObservatory.page.eval_log_groups_label')}
          </label>
          <textarea
            id="eval-log-groups"
            className={`${FIELD} resize-y font-mono`}
            rows={3}
            value={logGroupText}
            onChange={(e) => setLogGroupText(e.target.value)}
            spellCheck={false}
            placeholder="/aws/bedrock-agentcore/runtimes/..."
          />
        </div>
      </div>

      <div className="grid gap-2 md:grid-cols-2 mt-2">
        <div>
          <label className="block text-xs text-muted mb-1" htmlFor="eval-start">
            {i18nT('apps.agentcoreObservatory.page.eval_start_label')}
          </label>
          <Input
            id="eval-start"
            value={startTime}
            onChange={(e) => setStartTime(e.target.value)}
            placeholder="2026-09-01T00:00:00Z"
          />
        </div>
        <div>
          <label className="block text-xs text-muted mb-1" htmlFor="eval-end">
            {i18nT('apps.agentcoreObservatory.page.eval_end_label')}
          </label>
          <Input
            id="eval-end"
            value={endTime}
            onChange={(e) => setEndTime(e.target.value)}
            placeholder="2026-09-02T00:00:00Z"
          />
        </div>
      </div>

      <div className="flex flex-wrap gap-2 items-center mt-3">
        {confirming ? (
          <>
            <span className="text-sm text-warn">
              {i18nT('apps.agentcoreObservatory.page.eval_confirm_question')}
            </span>
            <Btn onClick={() => start.mutate()} disabled={start.isPending}>
              <FlaskConical size={16} className="lucide-inline" />
              {i18nT('apps.agentcoreObservatory.page.eval_confirm')}
            </Btn>
            <Btn onClick={() => setConfirming(false)} disabled={start.isPending}>
              {i18nT('apps.agentcoreObservatory.page.cancel')}
            </Btn>
          </>
        ) : (
          <Btn onClick={() => setConfirming(true)} disabled={!!problem || start.isPending}>
            <FlaskConical size={16} className="lucide-inline" />
            {i18nT('apps.agentcoreObservatory.page.start_evaluation')}
          </Btn>
        )}
        {/* The reason is shown rather than only disabling the button: a greyed
            control with no explanation is how a user concludes the app is broken. */}
        {!!problem && !confirming && <span className="text-sm text-muted">{i18nT(problem)}</span>}
      </div>

      {failure && (
        <div className="flex gap-2 items-start text-sm mt-2">
          {failure.denied ? (
            <Ban size={16} className="lucide-inline mt-0.5 text-warn shrink-0" />
          ) : (
            <AlertTriangle size={16} className="lucide-inline mt-0.5 text-warn shrink-0" />
          )}
          <div className="min-w-0">
            <div className="font-medium">
              {failure.denied
                ? i18nT('apps.agentcoreObservatory.page.denied_headline')
                : i18nT('apps.agentcoreObservatory.page.eval_failed')}
            </div>
            <div className="text-muted break-words">{failure.error}</div>
          </div>
        </div>
      )}
      {result?.ok && (
        <p className="text-sm mt-2">
          {i18nT('apps.agentcoreObservatory.page.eval_started')}{' '}
          <span className="font-mono text-xs break-all">
            {String(result.result.batchEvaluationId ?? '')}
          </span>
        </p>
      )}
    </section>
  )
}
