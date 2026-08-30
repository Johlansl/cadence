import { Component, type ErrorInfo, type ReactNode } from 'react'

interface Props {
  children: ReactNode
}

interface State {
  error: Error | null
}

// A render error anywhere below this used to blank the whole page. Now it shows
// a fallback with a reload button and logs the error for debugging.
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error('Cadence UI crashed:', error, info.componentStack)
  }

  render(): ReactNode {
    if (!this.state.error) return this.props.children
    return (
      <div className="flex h-full flex-col items-center justify-center gap-3 bg-zinc-950 p-6 text-center">
        <p className="text-sm text-zinc-300">The dashboard hit an unexpected error.</p>
        <pre className="max-w-lg overflow-auto rounded bg-zinc-900 p-2 font-mono text-[11px] text-zinc-500">
          {this.state.error.message}
        </pre>
        <button
          type="button"
          onClick={() => window.location.reload()}
          className="rounded bg-zinc-100 px-3 py-1.5 text-xs font-medium text-zinc-900 hover:bg-white"
        >
          reload
        </button>
      </div>
    )
  }
}
