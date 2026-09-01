// Semantic colour tones shared by every status pill / coloured count in the UI.
// Keeps the `bg-x/10 text-x-400 ring-x-500/30` triple in one place instead of
// being re-derived in StatusBadge, Jobs, Freshness, ...

export type Tone = 'ok' | 'warn' | 'danger' | 'info' | 'neutral' | 'reboot'

const RING: Record<Tone, string> = {
  ok: 'bg-emerald-500/10 text-emerald-400 ring-emerald-500/30',
  warn: 'bg-amber-500/10 text-amber-400 ring-amber-500/30',
  danger: 'bg-red-500/10 text-red-400 ring-red-500/30',
  info: 'bg-sky-500/10 text-sky-400 ring-sky-500/30',
  neutral: 'bg-zinc-500/10 text-zinc-400 ring-zinc-500/30',
  reboot: 'bg-orange-500/10 text-orange-400 ring-orange-500/30',
}

export const TONE_TEXT: Record<Tone, string> = {
  ok: 'text-emerald-400',
  warn: 'text-amber-400',
  danger: 'text-red-400',
  info: 'text-sky-400',
  neutral: 'text-zinc-400',
  reboot: 'text-orange-400',
}

// Full className for a small pill/badge of the given tone.
export function pill(tone: Tone): string {
  return `inline-flex items-center rounded px-1.5 py-0.5 text-xs font-medium ring-1 ${RING[tone]}`
}
