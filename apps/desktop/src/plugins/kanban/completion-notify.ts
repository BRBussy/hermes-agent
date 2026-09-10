/** Board event notices consume the backend state snapshot when available. */

import { host, type PluginOs, type PluginRestOptions, type PluginTranslate } from '@hermes/plugin-sdk'

import { en } from './i18n'

type Rest = <T>(path: string, opts?: PluginRestOptions) => Promise<T>

export interface CompletionEvent {
  id?: unknown
  task_id?: string
  kind?: string
  payload?: Record<string, unknown> | null
  notification?: { text: string; category: string; historical: boolean } | null
}

type ToastKind = 'error' | 'success' | 'warning' | 'info'

const TERMINAL_NOTIFY = new Map<string, { titleKey: string; toast: ToastKind }>([
  ['publication_pending', { titleKey: 'notify.completedTitle', toast: 'success' }],
  ['review_requested', { titleKey: 'notify.completedTitle', toast: 'success' }],
  ['changes_requested', { titleKey: 'notify.blockedTitle', toast: 'warning' }],
  ['recovery_required', { titleKey: 'notify.blockedTitle', toast: 'warning' }],
  ['progress_warning', { titleKey: 'notify.blockedTitle', toast: 'warning' }],
  ['blocked', { titleKey: 'notify.blockedTitle', toast: 'warning' }],
  ['block_loop_detected', { titleKey: 'notify.blockLoopTitle', toast: 'warning' }],
  ['completed', { titleKey: 'notify.completedTitle', toast: 'success' }],
  ['crashed', { titleKey: 'notify.crashedTitle', toast: 'error' }],
  ['gave_up', { titleKey: 'notify.gaveUpTitle', toast: 'error' }],
  ['timed_out', { titleKey: 'notify.timedOutTitle', toast: 'warning' }]
])

const seenEventIdByBoard = new Map<string, number>()
const baselinePending = new Set<string>()

let rest: Rest | null = null
let translate: PluginTranslate | null = null
let osDoor: PluginOs | null = null

/** Resolve a dot-path against the plugin's own English bundle — the same
 *  last-rung fallback the plugin i18n registry applies, usable before (or
 *  without) a bound translator. */
function fallbackT(key: string, ...args: unknown[]): string {
  let node: unknown = en

  for (const part of key.split('.')) {
    node = (node as Record<string, unknown> | undefined)?.[part]
  }

  if (typeof node === 'function') {
    return (node as (...a: unknown[]) => string)(...args)
  }

  return typeof node === 'string' ? node : key
}

function t(key: string, ...args: unknown[]): string {
  const translated = translate?.(key, ...args)

  // The registry returns the raw key when the bundle isn't registered yet.
  return translated && translated !== key ? translated : fallbackT(key, ...args)
}

export function bindCompletionNotify(r: Rest, pluginTranslate?: PluginTranslate, os?: PluginOs): void {
  rest = r
  translate = pluginTranslate ?? null
  osDoor = os ?? null
}

async function ensureBaseline(slug: string): Promise<void> {
  if (seenEventIdByBoard.has(slug) || baselinePending.has(slug)) {
    return
  }

  baselinePending.add(slug)

  try {
    const board = (await rest!<{ latest_event_id?: unknown }>(`/board?board=${encodeURIComponent(slug)}`)) as {
      latest_event_id?: unknown
    }

    seenEventIdByBoard.set(slug, typeof board.latest_event_id === 'number' ? board.latest_event_id : 0)
  } catch {
    // Fail-closed: unknown baseline → notifications stay suppressed.
  } finally {
    baselinePending.delete(slug)
  }
}

function notifyOne(slug: string, spec: { titleKey: string; toast: ToastKind }, ev: CompletionEvent): void {
  const taskId = (ev.task_id ?? '').trim()
  const notice = ev.notification
  const title = notice ? notice.category : 'Kanban task update'
  const message = notice?.text || `[${slug}] ${taskId}: ${ev.kind}. Current state unavailable. Inspect the card`
  host.notify({
    kind: notice?.historical || notice?.category === 'information' ? 'info' : spec.toast,
    title,
    message,
    action: { label: t('notify.openKanban'), onClick: () => host.navigate('/kanban') }
  })
  try {
    osDoor?.notify({ title, body: message })
  } catch {
    /* The in-app notice remains available if the OS door fails. */
  }
}

/** Consume one /events frame for a board. Returns true when a terminal-event
 *  notification was fired. Never throws: notification failure cannot
 *  interfere with api.ts cache invalidation. */
export async function onKanbanEventsFrame(slug: string, events?: CompletionEvent[]): Promise<boolean> {
  if (!events?.length || slug === '' || !rest) {
    return false
  }

  await ensureBaseline(slug)
  const seen = seenEventIdByBoard.get(slug)

  if (seen === undefined) {
    return false
  } // fail-closed

  let fired = false
  let cursor = seen

  for (const ev of [...events].sort((a, b) => Number(a.id) - Number(b.id))) {
    if (typeof ev.id !== 'number' || !Number.isSafeInteger(ev.id) || ev.id <= cursor) {
      continue
    }

    cursor = ev.id
    const spec = TERMINAL_NOTIFY.get(ev.kind ?? '')

    if (spec && ev.notification !== null) {
      try {
        notifyOne(slug, spec, ev)
        fired = true
      } catch {
        break
      }
    }
    seenEventIdByBoard.set(slug, cursor)
  }

  return fired
}
