/**
 * SSE reply rendering.
 *
 * Built from a real invocation: a runtime answered
 * `contentType: text/event-stream` with a single event carrying an
 * application-level error under HTTP 200. The parsing here is grounded in the
 * SSE wire format ONLY — blank-line-separated events, `data:`-prefixed payload
 * lines. It deliberately does not know what `{"type":"ERROR"}` means: that is
 * one agent's convention, and treating it as a contract would repeat the
 * fabricated-schema mistake this panel exists to avoid.
 */
import { describe, expect, it } from 'vitest'
import { isEventStream, sseEvents } from '../RuntimeTestPanel'

// Verbatim from the live call, minus the identifiers.
const REAL_BODY =
  'data: {"type": "ERROR", "content_chunk": "\u670d\u52d9\u66ab\u6642\u4e0d\u53ef\u7528\uff0c\u8acb\u7a0d\u5f8c\u518d\u8a66\u3002"}'

describe('isEventStream', () => {
  it('recognises the content type AgentCore actually returned', () => {
    expect(isEventStream('text/event-stream; charset=utf-8')).toBe(true)
    expect(isEventStream('TEXT/EVENT-STREAM')).toBe(true)
  })

  it('does not claim a JSON reply is a stream', () => {
    expect(isEventStream('application/json')).toBe(false)
    expect(isEventStream(undefined)).toBe(false)
    expect(isEventStream(200)).toBe(false)
  })
})

describe('sseEvents', () => {
  it('extracts the payload of the single real event', () => {
    const events = sseEvents(REAL_BODY)
    expect(events).toHaveLength(1)
    // The payload is preserved exactly — not parsed, not classified.
    expect(events[0]).toBe(
      '{"type": "ERROR", "content_chunk": "\u670d\u52d9\u66ab\u6642\u4e0d\u53ef\u7528\uff0c\u8acb\u7a0d\u5f8c\u518d\u8a66\u3002"}',
    )
  })

  it('splits a multi-event stream on blank lines', () => {
    const events = sseEvents('data: one\n\ndata: two\n\ndata: three\n\n')
    expect(events).toEqual(['one', 'two', 'three'])
  })

  it('joins multi-line data within one event', () => {
    // The SSE spec allows several data lines per event; they are one payload.
    expect(sseEvents('data: {"a":1,\ndata: "b":2}\n\n')).toEqual(['{"a":1,\n"b":2}'])
  })

  it('ignores non-data fields rather than showing them as content', () => {
    expect(sseEvents('event: message\nid: 7\nretry: 100\ndata: payload\n\n')).toEqual(['payload'])
  })

  it('tolerates CRLF, which a proxy may introduce', () => {
    expect(sseEvents('data: one\r\n\r\ndata: two\r\n\r\n')).toEqual(['one', 'two'])
  })

  it('returns nothing for a body that is not a stream, so the caller shows it raw', () => {
    // Falling back to verbatim is what keeps a plain-text or JSON answer visible
    // instead of rendering an empty list.
    expect(sseEvents('{"ok": false}')).toEqual([])
    expect(sseEvents('')).toEqual([])
  })
})
