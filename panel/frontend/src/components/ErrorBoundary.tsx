import { Component, type ReactNode } from 'react'
import { AlertTriangle, RefreshCw } from 'lucide-react'

interface Props {
  children: ReactNode
  fallback?: ReactNode
}

interface State {
  hasError: boolean
  error: Error | null
  isChunkError: boolean
}

export default class ErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false, error: null, isChunkError: false }

  static getDerivedStateFromError(error: Error): State {
    const isChunkError =
      error.name === 'ChunkLoadError' ||
      error.message?.includes('Loading chunk') ||
      error.message?.includes('Failed to fetch dynamically imported module') ||
      error.message?.includes('Importing a module script failed')

    return { hasError: true, error, isChunkError }
  }

  handleReload = () => {
    window.location.reload()
  }

  handleRetry = () => {
    this.setState({ hasError: false, error: null, isChunkError: false })
  }

  render() {
    if (this.state.hasError) {
      if (this.props.fallback) return this.props.fallback

      return (
        <div className="min-h-screen bg-dark-950 flex items-center justify-center p-6">
          <div className="max-w-md w-full text-center">
            <div className="w-16 h-16 rounded-2xl bg-danger/10 border border-danger/20 flex items-center justify-center mx-auto mb-6">
              <AlertTriangle className="w-8 h-8 text-danger" />
            </div>

            <h2 className="text-xl font-bold text-dark-100 mb-2">
              {this.state.isChunkError ? 'Page failed to load' : 'Something went wrong'}
            </h2>

            <p className="text-dark-400 mb-8 text-sm">
              {this.state.isChunkError
                ? 'The page could not be loaded. This usually happens after an update. Please reload.'
                : 'An unexpected error occurred. Try reloading the page.'}
            </p>

            <div className="flex items-center justify-center gap-3">
              {!this.state.isChunkError && (
                <button
                  onClick={this.handleRetry}
                  className="px-5 py-2.5 bg-dark-800 hover:bg-dark-700 text-dark-200 rounded-xl
                             font-medium transition-all border border-dark-700 hover:border-dark-600"
                >
                  Retry
                </button>
              )}
              <button
                onClick={this.handleReload}
                className="px-5 py-2.5 bg-accent-500 hover:bg-accent-400 text-dark-950 rounded-xl
                           font-medium transition-all flex items-center gap-2 shadow-lg shadow-accent-500/20"
              >
                <RefreshCw className="w-4 h-4" />
                Reload page
              </button>
            </div>
          </div>
        </div>
      )
    }

    return this.props.children
  }
}
