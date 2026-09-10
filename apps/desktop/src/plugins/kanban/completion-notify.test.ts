/**
 * Focused tests for the completion-notify module.
 *
 * Each test starts from a FRESH module instance (vi.resetModules + dynamic
 * import) so the in-memory per-board cursor and rest binding match a fresh
 * renderer process. The @hermes/plugin-sdk host is mocked: notify/navigate
 * calls are asserted, never really executed.
 */

import type { PluginRestOptions } from '@hermes/plugin-sdk'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { CompletionEvent } from './completion-notify'

/** Mirrors the module's public surface (no `typeof import()` — repo eslint
 *  bans import() in type annotations). */
type Rest = <T>(path: string, opts?: PluginRestOptions) => Promise<T>
type Translate = (key: string, ...args: unknown[]) => string
interface OsDoor {
  notify: (input: { title: string; body?: string; silent?: boolean }) => void
}
interface Mod {
  bindCompletionNotify(r: Rest, t?: Translate, os?: OsDoor): void
  onKanbanEventsFrame(slug: string, events?: CompletionEvent[]): Promise<boolean>
}

const { hostMock } = vi.hoisted(() => ({
  hostMock: { notify: vi.fn(), navigate: vi.fn() }
}))

vi.mock('@hermes/plugin-sdk', () => ({
  host: hostMock,
  // Pulled in transitively via ./i18n (the module reads its `en` bundle for
  // fallback titles); never called in these tests.
  usePluginI18n: () => (key: string) => key
}))

type NotifyInput = {
  message: string
  title?: string
  kind?: string
  detail?: string
  action?: { label: string; onClick: () => void }
}

const lastNotify = (): NotifyInput =>
  hostMock.notify.mock.calls[hostMock.notify.mock.calls.length - 1][0] as NotifyInput

/** Rest stub: GET /board resolves to the current latest_event_id. */
function makeRest(latest: () => number) {
  return vi.fn(async (path: string) => {
    if (path.startsWith('/board')) {
      return { latest_event_id: latest() }
    }

    throw new Error(`unexpected rest call: ${path}`)
  })
}

async function loadModule(): Promise<Mod> {
  vi.resetModules()

  return import('./completion-notify')
}

const ev = (
  id: number,
  kind = 'created',
  payload: Record<string, unknown> | null = null,
  taskId = `t${id}`
): CompletionEvent => ({
  id,
  kind,
  task_id: taskId,
  payload
})

beforeEach(() => {
  vi.resetModules()
  vi.clearAllMocks()
})

describe('authoritative baseline', () => {
  it('baselines from GET /board latest_event_id and suppresses replay history', async () => {
    const rest = makeRest(() => 100)
    const m = await loadModule()
    m.bindCompletionNotify(rest as never)

    // Replay/historical: ids <= baseline must never notify.
    const fired = await m.onKanbanEventsFrame('smoke', [ev(50, 'completed'), ev(100, 'completed')])

    expect(fired).toBe(false)
    expect(hostMock.notify).not.toHaveBeenCalled()
    expect(rest).toHaveBeenCalledWith('/board?board=smoke')
  })

  it('post-baseline completion notifies exactly once', async () => {
    const m = await loadModule()
    m.bindCompletionNotify(makeRest(() => 100) as never)

    const fired = await m.onKanbanEventsFrame('smoke', [
      ev(101, 'completed', { summary: 'Done', artifacts: ['/tmp/x/report.md'] })
    ])

    expect(fired).toBe(true)
    expect(hostMock.notify).toHaveBeenCalledTimes(1)

    // Same event delivered again (duplicate frame) must not re-notify.
    const again = await m.onKanbanEventsFrame('smoke', [ev(101, 'completed', { summary: 'Done' })])

    expect(again).toBe(false)
    expect(hostMock.notify).toHaveBeenCalledTimes(1)
  })

  it('duplicate ids inside one frame notify once', async () => {
    const m = await loadModule()
    m.bindCompletionNotify(makeRest(() => 100) as never)

    await m.onKanbanEventsFrame('smoke', [ev(101, 'completed'), ev(101, 'completed'), ev(101, 'completed')])

    expect(hostMock.notify).toHaveBeenCalledTimes(1)
  })

  it('reconnect replay dedupe: replayed history up to the cursor is suppressed', async () => {
    const m = await loadModule()
    m.bindCompletionNotify(makeRest(() => 100) as never)

    await m.onKanbanEventsFrame('smoke', [ev(101, 'completed'), ev(102, 'completed')])
    expect(hostMock.notify).toHaveBeenCalledTimes(2)

    // Reconnect replays from 0: ids <= cursor are history, only 103 is new.
    await m.onKanbanEventsFrame('smoke', [
      ev(99, 'completed'),
      ev(101, 'completed'),
      ev(102, 'completed'),
      ev(103, 'completed')
    ])

    expect(hostMock.notify).toHaveBeenCalledTimes(3)
    expect(hostMock.notify.mock.calls[2][0]).toMatchObject({ message: expect.stringContaining('t103') })
  })

  it('missed unseen event: frame arriving before the baseline resolves is classified after it', async () => {
    let resolveBoard!: (value: { latest_event_id: number }) => void

    const rest = vi.fn(async (path: string) => {
      if (path.startsWith('/board')) {
        return new Promise<{ latest_event_id: number }>(resolve => {
          resolveBoard = resolve
        })
      }

      throw new Error(`unexpected rest call: ${path}`)
    })

    const m = await loadModule()
    m.bindCompletionNotify(rest as never)

    // Fire the frame before the baseline resolves.
    const pending = m.onKanbanEventsFrame('smoke', [ev(100, 'created'), ev(105, 'completed')])
    resolveBoard({ latest_event_id: 100 })
    const fired = await pending

    expect(fired).toBe(true)
    expect(hostMock.notify).toHaveBeenCalledTimes(1)
    expect(lastNotify().message).toContain('t105')
  })

  it('fresh process baseline: a new module instance re-baselines and suppresses older events', async () => {
    // First instance: baseline 100, sees 101 -> notify.
    const m1 = await loadModule()
    m1.bindCompletionNotify(makeRest(() => 100) as never)
    await m1.onKanbanEventsFrame('smoke', [ev(101, 'completed')])
    expect(hostMock.notify).toHaveBeenCalledTimes(1)

    // "Restart": fresh module, board's MAX has advanced to 200 -> 150 is history.
    const m2 = await loadModule()
    m2.bindCompletionNotify(makeRest(() => 200) as never)
    const fired = await m2.onKanbanEventsFrame('smoke', [ev(150, 'completed')])
    expect(fired).toBe(false)
    expect(hostMock.notify).toHaveBeenCalledTimes(1)
  })

  it('baseline failure is fail-closed: unknown baseline suppresses, later success binds', async () => {
    let failBoard = true

    const rest = vi.fn(async (path: string) => {
      if (path.startsWith('/board')) {
        if (failBoard) {
          throw new Error('board unavailable')
        }

        return { latest_event_id: 200 }
      }

      throw new Error(`unexpected rest call: ${path}`)
    })

    const m = await loadModule()
    m.bindCompletionNotify(rest as never)

    const fired1 = await m.onKanbanEventsFrame('smoke', [ev(150, 'completed')])
    expect(fired1).toBe(false)
    expect(hostMock.notify).not.toHaveBeenCalled()

    // Baseline now succeeds: 150 <= 200 stays suppressed, 201 notifies.
    failBoard = false
    const fired2 = await m.onKanbanEventsFrame('smoke', [ev(150, 'completed')])
    expect(fired2).toBe(false)

    const fired3 = await m.onKanbanEventsFrame('smoke', [ev(201, 'completed')])
    expect(fired3).toBe(true)
    expect(hostMock.notify).toHaveBeenCalledTimes(1)
  })

  it('unbound module (no rest door) is fail-closed', async () => {
    const m = await loadModule()
    const fired = await m.onKanbanEventsFrame('smoke', [ev(101, 'completed')])
    expect(fired).toBe(false)
    expect(hostMock.notify).not.toHaveBeenCalled()
  })
})

describe('cursor advancement', () => {
  it('advances for every event kind; only completed emits', async () => {
    const m = await loadModule()
    m.bindCompletionNotify(makeRest(() => 100) as never)

    await m.onKanbanEventsFrame('smoke', [ev(101, 'created'), ev(102, 'assigned'), ev(103, 'status_changed')])
    expect(hostMock.notify).not.toHaveBeenCalled()

    // Cursor is at 103; a completed at 104 must still notify (cursor moved).
    const fired = await m.onKanbanEventsFrame('smoke', [ev(104, 'completed')])
    expect(fired).toBe(true)
    expect(hostMock.notify).toHaveBeenCalledTimes(1)
  })

  it('malformed ids are skipped without advancing the cursor', async () => {
    const m = await loadModule()
    m.bindCompletionNotify(makeRest(() => 100) as never)

    const bad = { id: 'not-a-number', kind: 'completed', task_id: 'tx' } as unknown as CompletionEvent
    await m.onKanbanEventsFrame('smoke', [bad, ev(102, 'completed')])

    expect(hostMock.notify).toHaveBeenCalledTimes(1)
    expect(lastNotify().message).toContain('t102')
  })
})

describe('board isolation', () => {
  it('never mixes cursors between boards', async () => {
    const latest = new Map<string, number>([
      ['a', 100],
      ['b', 200]
    ])

    const rest = vi.fn(async (path: string) => {
      if (path.startsWith('/board')) {
        const slug = new URLSearchParams(path.split('?')[1]).get('board') ?? ''

        return { latest_event_id: latest.get(slug) ?? 0 }
      }

      throw new Error(`unexpected rest call: ${path}`)
    })

    const m = await loadModule()
    m.bindCompletionNotify(rest as never)

    await m.onKanbanEventsFrame('a', [ev(150, 'completed')])
    expect(hostMock.notify).toHaveBeenCalledTimes(1)

    // Same id on board b is below b's baseline -> suppressed.
    await m.onKanbanEventsFrame('b', [ev(150, 'completed')])
    expect(hostMock.notify).toHaveBeenCalledTimes(1)
  })

  it('switch away and back reuses the prior cursor, never reset to current MAX', async () => {
    const latest = new Map<string, number>([
      ['a', 100],
      ['b', 50]
    ])

    const rest = vi.fn(async (path: string) => {
      if (path.startsWith('/board')) {
        const slug = new URLSearchParams(path.split('?')[1]).get('board') ?? ''

        return { latest_event_id: latest.get(slug) ?? 0 }
      }

      throw new Error(`unexpected rest call: ${path}`)
    })

    const m = await loadModule()
    m.bindCompletionNotify(rest as never)

    await m.onKanbanEventsFrame('a', [ev(150, 'completed')])
    expect(hostMock.notify).toHaveBeenCalledTimes(1)

    // Away to b.
    await m.onKanbanEventsFrame('b', [ev(60, 'completed')])
    expect(hostMock.notify).toHaveBeenCalledTimes(2)

    // Back to a: replay [100..150] must be silent — cursor kept at 150,
    // and the baseline must NOT be re-fetched (no reset to current MAX).
    const fired = await m.onKanbanEventsFrame('a', [ev(100, 'completed'), ev(150, 'completed')])
    expect(fired).toBe(false)
    expect(hostMock.notify).toHaveBeenCalledTimes(2)
    const boardCalls = rest.mock.calls.filter(call => String(call[0]).startsWith('/board?board=a'))
    expect(boardCalls).toHaveLength(1)
  })
})

describe('ambiguous alias', () => {
  it("empty slug ('' = server current board) is suppressed and never queried", async () => {
    const rest = makeRest(() => 100)
    const m = await loadModule()
    m.bindCompletionNotify(rest as never)

    const fired = await m.onKanbanEventsFrame('', [ev(101, 'completed')])

    expect(fired).toBe(false)
    expect(hostMock.notify).not.toHaveBeenCalled()
    expect(rest).not.toHaveBeenCalled()
  })
})

const event = (id: number, kind = 'blocked'): CompletionEvent => ({
  id, kind, task_id: 'fixture',
  notification: { text: `Event ${id}: current state done`, category: 'earlier-event history', historical: true }
})

async function fixture(latest = 10) {
  const module = await import('./completion-notify')
  const rest = vi.fn(async () => ({ latest_event_id: latest }))
  const os = { notify: vi.fn() }
  module.bindCompletionNotify(rest as never, undefined, os)
  return { ...module, rest, os }
}

describe('notification state and delivery', () => {
  it('uses backend interpretation for both in-app and OS notices', async () => {
    const m = await fixture()
    await m.onKanbanEventsFrame('board', [event(11)])
    expect(hostMock.notify).toHaveBeenCalledWith(expect.objectContaining({
      kind: 'info', title: 'earlier-event history', message: 'Event 11: current state done'
    }))
    expect(m.os.notify).toHaveBeenCalledWith({ title: 'earlier-event history', body: 'Event 11: current state done' })
    hostMock.notify.mock.calls[0][0].action.onClick()
    expect(hostMock.navigate).toHaveBeenCalledWith('/kanban')
  })

  it('keeps publication handoffs and actionable failures distinct', async () => {
    const m = await fixture()
    const publication = event(11, 'publication_pending')
    publication.notification = { text: 'Content accepted', category: 'publication handoff', historical: false }
    const blocked = event(12)
    blocked.notification = { text: 'Approval required', category: 'needs decision', historical: false }
    await m.onKanbanEventsFrame('board', [publication, blocked])
    expect(hostMock.notify.mock.calls[0][0]).toMatchObject({ title: 'publication handoff', message: 'Content accepted' })
    expect(hostMock.notify.mock.calls[1][0]).toMatchObject({ kind: 'warning', title: 'needs decision' })
  })

  it('suppresses backend unchanged notices and generic status updates', async () => {
    const m = await fixture()
    await m.onKanbanEventsFrame('board', [{ ...event(11), notification: null }, event(12, 'status')])
    expect(hostMock.notify).not.toHaveBeenCalled()
    expect(await m.onKanbanEventsFrame('board', [event(13)])).toBe(true)
  })

  it('states uncertainty when a backend provides no interpretation', async () => {
    const m = await fixture()
    await m.onKanbanEventsFrame('board', [{ id: 11, task_id: 'fixture', kind: 'completed',
      payload: { summary: '{private JSON}', artifacts: ['/private/stale/path'] } }])
    const text = hostMock.notify.mock.calls[0][0].message
    expect(text).toContain('[board] fixture')
    expect(text).toContain('Current state unavailable')
    expect(text).not.toContain('/private/stale')
    expect(text).not.toContain('{private')
  })

  it('retries a failed toast without advancing past its event', async () => {
    const m = await fixture()
    hostMock.notify.mockImplementationOnce(() => { throw new Error('fixture failure') })
    expect(await m.onKanbanEventsFrame('board', [event(11), event(12)])).toBe(false)
    expect(await m.onKanbanEventsFrame('board', [event(11), event(12)])).toBe(true)
    expect(hostMock.notify).toHaveBeenCalledTimes(3)
  })

  it('keeps in-app delivery when the OS door fails', async () => {
    const m = await fixture()
    m.os.notify.mockImplementation(() => { throw new Error('OS unavailable') })
    expect(await m.onKanbanEventsFrame('board', [event(11)])).toBe(true)
    expect(hostMock.notify).toHaveBeenCalledTimes(1)
  })
})

describe('board cursors', () => {
  it('baselines replay history, sorts a frame and suppresses repeated events', async () => {
    const m = await fixture()
    expect(await m.onKanbanEventsFrame('board', [event(10)])).toBe(false)
    await m.onKanbanEventsFrame('board', [event(12), event(11)])
    expect(hostMock.notify.mock.calls.map(call => call[0].message)).toEqual([
      'Event 11: current state done', 'Event 12: current state done'
    ])
    expect(await m.onKanbanEventsFrame('board', [event(11), event(12)])).toBe(false)
    expect(m.rest).toHaveBeenCalledTimes(1)
  })

  it('keeps board cursors independent through a board switch', async () => {
    const m = await fixture()
    await m.onKanbanEventsFrame('a', [event(11)])
    await m.onKanbanEventsFrame('b', [event(11)])
    await m.onKanbanEventsFrame('a', [event(11)])
    expect(hostMock.notify).toHaveBeenCalledTimes(2)
    expect(m.rest).toHaveBeenCalledTimes(2)
  })

  it('suppresses duplicate concurrent frames', async () => {
    const m = await fixture()
    await m.onKanbanEventsFrame('a', [event(10)])
    await Promise.all([m.onKanbanEventsFrame('a', [event(11)]), m.onKanbanEventsFrame('a', [event(11)])])
    expect(hostMock.notify).toHaveBeenCalledTimes(1)
  })

  it('fails closed for unknown baselines and permits a later retry', async () => {
    const m = await fixture()
    m.rest.mockRejectedValueOnce(new Error('Disconnected'))
    expect(await m.onKanbanEventsFrame('a', [event(11)])).toBe(false)
    expect(await m.onKanbanEventsFrame('a', [event(11)])).toBe(true)
  })

  it('rejects ambiguous boards, unbound delivery and malformed IDs', async () => {
    const unbound = await import('./completion-notify')
    expect(await unbound.onKanbanEventsFrame('a', [event(11)])).toBe(false)
    const m = await fixture()
    expect(await m.onKanbanEventsFrame('', [event(11)])).toBe(false)
    await m.onKanbanEventsFrame('a', [{ ...event(12), id: 'bad' }, { ...event(12), id: Infinity }, event(11)])
    expect(hostMock.notify).toHaveBeenCalledTimes(1)
  })
})
