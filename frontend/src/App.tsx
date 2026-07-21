import { useState, type FormEvent } from 'react'
import { Sparkles } from 'lucide-react'
import Dashboard from './pages/Dashboard'
import { AppProvider } from './context/AppContext'
import { login, register, setAccessToken } from './services/api'

export default function App() {
  const [isRegistering, setIsRegistering] = useState(false)
  const [isAuthenticated, setIsAuthenticated] = useState(false)
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setError('')
    setSubmitting(true)

    try {
      if (isRegistering) {
        await register(email.trim(), password)
      }

      const result = await login(email.trim(), password)
      setAccessToken(result.access_token)
      setIsAuthenticated(true)
      setPassword('')
    } catch {
      setError('Unable to sign in. Check your details and try again.')
    } finally {
      setSubmitting(false)
    }
  }

  function handleLogout() {
    setAccessToken('')
    setIsAuthenticated(false)
    setPassword('')
  }

  if (isAuthenticated) {
    return (
      <AppProvider userEmail={email.trim()} onLogout={handleLogout}>
        <Dashboard />
      </AppProvider>
    )
  }

  return (
    <main className="relative grid min-h-screen place-items-center overflow-hidden bg-[#f8fafc] px-4 py-10 before:absolute before:left-1/2 before:top-1/2 before:h-[520px] before:w-[720px] before:-translate-x-1/2 before:-translate-y-1/2 before:rounded-full before:bg-blue-100/60 before:blur-3xl">
      <section className="relative w-full max-w-sm rounded-[18px] border border-[#e6ecf5] bg-white/90 p-6 shadow-[0_20px_60px_rgba(37,99,235,.12)] backdrop-blur-xl">
        <div className="mb-6">
          <span className="mb-4 grid h-11 w-11 place-items-center rounded-full bg-gradient-to-br from-blue-600 to-indigo-500 text-white shadow-[0_8px_22px_rgba(37,99,235,.25)]"><Sparkles size={19} /></span>
          <p className="text-[11px] font-semibold uppercase tracking-[.14em] text-blue-600">Simple RAG</p>
          <h1 className="mt-2 text-2xl font-bold tracking-[-.03em] text-slate-900">
            {isRegistering ? 'Create your account' : 'Sign in to your workspace'}
          </h1>
          <p className="mt-2 text-sm leading-6 text-slate-500">
            Upload documents, retrieve owned sources, and ask questions through the secured API.
          </p>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          <label className="block">
            <span className="text-xs font-semibold text-slate-600">Email</span>
            <input
              type="email"
              value={email}
              onChange={event => setEmail(event.target.value)}
              className="mt-1 h-11 w-full rounded-xl border border-[#e6ecf5] bg-white px-3 text-sm outline-none focus:border-blue-500 focus:ring-4 focus:ring-blue-100/60"
              autoComplete="email"
              required
            />
          </label>

          <label className="block">
            <span className="text-xs font-semibold text-slate-600">Password</span>
            <input
              type="password"
              value={password}
              onChange={event => setPassword(event.target.value)}
              className="mt-1 h-11 w-full rounded-xl border border-[#e6ecf5] bg-white px-3 text-sm outline-none focus:border-blue-500 focus:ring-4 focus:ring-blue-100/60"
              autoComplete={isRegistering ? 'new-password' : 'current-password'}
              minLength={12}
              required
            />
          </label>

          {error && (
            <p className="rounded-xl border border-red-200 bg-red-50 px-3 py-2 text-xs font-semibold text-red-700">
              {error}
            </p>
          )}

          <button
            type="submit"
            disabled={submitting}
            className="h-11 w-full rounded-xl bg-gradient-to-br from-blue-600 to-indigo-500 px-4 text-sm font-semibold text-white shadow-[0_6px_18px_rgba(37,99,235,.22)] hover:-translate-y-0.5 hover:shadow-[0_9px_22px_rgba(37,99,235,.28)] disabled:cursor-not-allowed disabled:opacity-60"
          >
            {submitting ? 'Please wait...' : isRegistering ? 'Register and sign in' : 'Sign in'}
          </button>

          <button
            type="button"
            onClick={() => {
              setIsRegistering(current => !current)
              setError('')
            }}
            className="h-10 w-full rounded-xl border border-[#e6ecf5] bg-white text-sm font-semibold text-slate-600 hover:bg-[#f5f9ff] hover:text-blue-600"
          >
            {isRegistering ? 'Use an existing account' : 'Create an account'}
          </button>
        </form>
      </section>
    </main>
  )
}
