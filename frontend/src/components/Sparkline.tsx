interface Series {
  values: number[]
  stroke: string // tailwind text-* class; the line uses currentColor
}

// Minimal inline-SVG sparkline. All series share one y-scale (0..max) so they
// are visually comparable. x is the sample index.
export function Sparkline({
  series,
  width = 240,
  height = 40,
}: {
  series: Series[]
  width?: number
  height?: number
}) {
  const len = Math.max(...series.map((s) => s.values.length), 0)
  if (len < 2) return null
  const max = Math.max(1, ...series.flatMap((s) => s.values))
  const dx = width / (len - 1)
  const y = (v: number) => height - (v / max) * (height - 2) - 1

  return (
    <svg
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      preserveAspectRatio="none"
      role="img"
      aria-hidden="true"
    >
      {series.map((s, i) => (
        <polyline
          key={i}
          className={s.stroke}
          fill="none"
          stroke="currentColor"
          strokeWidth={1.5}
          strokeLinejoin="round"
          points={s.values.map((v, x) => `${x * dx},${y(v)}`).join(' ')}
        />
      ))}
    </svg>
  )
}
