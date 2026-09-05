/**
 * Turning field names into a payload.
 *
 * The load-bearing property is what this does NOT do: it never supplies a value.
 * A payload the app filled in would look authoritative while being invented, and
 * an agent answering 200 to it would make the invention look confirmed — the
 * exact failure the evidence-based design exists to prevent.
 */
import { describe, expect, it } from 'vitest'
import { mergeFields } from '../payload'

describe('mergeFields', () => {
  it('adds a named field with an EMPTY value, never a plausible one', () => {
    const out = JSON.parse(mergeFields('{}', ['chatId']))
    expect(out).toEqual({ chatId: '' })
  })

  it('keeps what the operator already typed', () => {
    const out = JSON.parse(mergeFields('{"prompt": "hello"}', ['chatId']))
    expect(out).toEqual({ prompt: 'hello', chatId: '' })
  })

  it('does not clobber a value already supplied for a named field', () => {
    const out = JSON.parse(mergeFields('{"chatId": "abc"}', ['chatId']))
    expect(out.chatId).toBe('abc')
  })

  it('builds the nesting a dotted path implies', () => {
    const out = JSON.parse(mergeFields('{}', ['businessData.sourceApp']))
    expect(out).toEqual({ businessData: { sourceApp: '' } })
  })

  it('merges into an existing nested object instead of replacing it', () => {
    const out = JSON.parse(
      mergeFields('{"businessData": {"other": 1}}', ['businessData.sourceApp']),
    )
    expect(out).toEqual({ businessData: { other: 1, sourceApp: '' } })
  })

  it('drops the index of a list path rather than inventing a length', () => {
    // `messages.0.content` says a list element was wrong. Creating a
    // one-element array would assert a length the runtime never stated, so only
    // the list itself is named and the operator fills in the shape.
    const out = JSON.parse(mergeFields('{}', ['messages.0.content']))
    expect(out).toEqual({ messages: '' })
  })

  it('replaces an unparseable payload rather than silently dropping the fields', () => {
    // A button that appears to do nothing is worse than a replaced draft.
    const out = JSON.parse(mergeFields('not json at all', ['chatId']))
    expect(out).toEqual({ chatId: '' })
  })

  it('ignores a JSON array, which cannot carry named fields', () => {
    const out = JSON.parse(mergeFields('[1,2]', ['chatId']))
    expect(out).toEqual({ chatId: '' })
  })

  it('returns indented JSON so the result is editable in the textarea', () => {
    expect(mergeFields('{}', ['a', 'b'])).toBe('{\n  "a": "",\n  "b": ""\n}')
  })
})
