/**
 * Render-level tests for the row list and the action panels.
 *
 * `labels.test.ts` already proves `rowKey` returns distinct keys for same-named
 * rows. That test passed for the whole life of a bug where the top-level list
 * still built its key as `rowName(row) || String(i)` — because a unit test on a
 * helper cannot see whether the call site uses it. The first test here mounts the
 * real list with two identically-named rows and asserts they expand
 * independently, which is the only shape of test that catches that class of
 * defect.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const getCatalog = vi.fn()
const getResource = vi.fn()
const getProfiles = vi.fn()
const invokeRuntime = vi.fn()
const getAgentCard = vi.fn()

vi.mock('../api', async () => {
  const actual = await vi.importActual<typeof import('../api')>('../api')
  return {
    ...actual,
    observatoryApi: {
      getCatalog: () => getCatalog(),
      getResource: (...a: unknown[]) => getResource(...a),
      getProfiles: () => getProfiles(),
      getConfig: () => Promise.resolve({ profile: '', region: 'us-east-2', configured: true }),
      saveConfig: () => Promise.resolve({ profile: '', region: 'us-east-2', configured: true }),
      getDetail: () => Promise.resolve({ type: 'x', detail: { ok: true, item: {}, error: '', denied: false } }),
      getAgentCard: (...a: unknown[]) => getAgentCard(...a),
      invokeRuntime: (...a: unknown[]) => invokeRuntime(...a),
      startBatchEvaluation: () => Promise.resolve({ ok: true, result: {}, body: '', body_truncated: false, error: '', denied: false }),
    },
  }
})

// Imported after the mock so the page picks up the stubbed client.
const { default: AgentcoreObservatoryPage } = await import('../AgentcoreObservatoryPage')

const RUNTIME_TYPE = {
  id: 'agent-runtimes',
  listable: true,
  idField: 'agentRuntimeId',
  children: [],
}

/** Two rows that a human cannot tell apart — the case that broke before. */
const SAME_NAME_ROWS = [
  {
    agentRuntimeName: 'orders_agent',
    agentRuntimeId: 'orders_agent-AbCdEf1234',
    agentRuntimeArn: 'arn:aws:bedrock-agentcore:us-east-2:111122223333:runtime/orders_agent-AbCdEf1234',
    status: 'READY',
    marker: 'first',
  },
  {
    agentRuntimeName: 'orders_agent',
    agentRuntimeId: 'orders_agent-AbCdEf1234',
    agentRuntimeArn: 'arn:aws:bedrock-agentcore:us-east-2:111122223333:runtime/orders_agent-AbCdEf1234',
    status: 'READY',
    marker: 'second',
  },
]

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  })
  return render(
    <QueryClientProvider client={client}>
      <AgentcoreObservatoryPage />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  getProfiles.mockResolvedValue({ profiles: [] })
  getCatalog.mockResolvedValue({
    config: { profile: '', region: 'us-east-2', configured: true },
    groups: [{ id: 'compute', types: [RUNTIME_TYPE] }],
  })
  getResource.mockResolvedValue({
    type: 'agent-runtimes',
    list: { ok: true, items: SAME_NAME_ROWS, error: '', denied: false, truncated: false },
  })
})

async function openRuntimeList(user: ReturnType<typeof userEvent.setup>) {
  const railItem = await screen.findByRole('button', { name: /runtime/i })
  await user.click(railItem)
  return await waitFor(() => screen.getAllByRole('button', { expanded: false }))
}

describe('row identity at the call site', () => {
  it('expands two identically-named rows independently', async () => {
    const user = userEvent.setup()
    renderPage()
    await openRuntimeList(user)

    // Both rows render, despite sharing name, id, ARN and status.
    const rows = await waitFor(() => {
      const found = screen.getAllByRole('button').filter((b) => b.textContent?.includes('orders_agent'))
      expect(found.length).toBe(2)
      return found
    })

    await user.click(rows[0])
    // Only the clicked row opened. A content-derived key would open both, which
    // is exactly the defect a helper-only test cannot see.
    await waitFor(() => {
      expect(rows[0]).toHaveAttribute('aria-expanded', 'true')
      expect(rows[1]).toHaveAttribute('aria-expanded', 'false')
    })

    // The open row shows its OWN payload, not the sibling's.
    const shown = await screen.findByText(/"marker": "first"/)
    expect(shown).toBeTruthy()
    expect(screen.queryByText(/"marker": "second"/)).toBeNull()
  })
})

describe('the runtime test panel', () => {
  it('needs a second, explicit confirmation before it invokes', async () => {
    const user = userEvent.setup()
    invokeRuntime.mockResolvedValue({
      ok: true,
      result: { statusCode: 200, traceId: '1-abc' },
      body: 'agent reply',
      body_truncated: false,
      derived_fields: [],
      error: '',
      denied: false,
    })
    renderPage()
    await openRuntimeList(user)

    const rows = await waitFor(() => {
      const found = screen.getAllByRole('button').filter((b) => b.textContent?.includes('orders_agent'))
      expect(found.length).toBe(2)
      return found
    })
    await user.click(rows[0])

    // First press only arms the confirmation — nothing is billed yet.
    const arm = await screen.findByRole('button', { name: /^Invoke$/ })
    await user.click(arm)
    expect(invokeRuntime).not.toHaveBeenCalled()

    const confirm = await screen.findByRole('button', { name: /Yes, invoke it/ })
    await user.click(confirm)
    await waitFor(() => expect(invokeRuntime).toHaveBeenCalledTimes(1))

    // The reply is shown verbatim, and the metadata a reader needs is beside it.
    expect(await screen.findByText('agent reply')).toBeTruthy()
    expect(screen.getByText('1-abc')).toBeTruthy()
  })

  it('labels the seeded payload as generic until a card proves otherwise', async () => {    const user = userEvent.setup()
    renderPage()
    await openRuntimeList(user)
    const rows = await waitFor(() => {
      const found = screen.getAllByRole('button').filter((b) => b.textContent?.includes('orders_agent'))
      expect(found.length).toBe(2)
      return found
    })
    await user.click(rows[0])

    // Before any card lookup the UI must not imply this shape came from the
    // runtime — a fabricated schema that returns 200 is the failure mode here.
    expect(await screen.findByText(/generic template/i)).toBeTruthy()
    expect(getAgentCard).not.toHaveBeenCalled()
  })
})
