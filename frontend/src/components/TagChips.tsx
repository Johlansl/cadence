// Read-only display of a host's {key: value} tags as small chips.
export function TagChips({
  tags,
  onClick,
}: {
  tags: Record<string, string>
  onClick?: (key: string, value: string) => void
}) {
  const entries = Object.entries(tags)
  if (entries.length === 0) return null
  return (
    <div className="flex flex-wrap gap-1">
      {entries.map(([k, v]) => {
        const label = v ? `${k}=${v}` : k
        const cls =
          'rounded bg-zinc-800 px-1.5 py-0.5 font-mono text-[10px] text-zinc-400 ring-1 ring-zinc-700'
        return onClick ? (
          <button
            key={k}
            type="button"
            onClick={() => onClick(k, v)}
            className={`${cls} hover:text-zinc-200`}
          >
            {label}
          </button>
        ) : (
          <span key={k} className={cls}>
            {label}
          </span>
        )
      })}
    </div>
  )
}
