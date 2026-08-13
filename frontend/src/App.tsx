import { useState, type FormEvent } from 'react'
import { Eye, EyeOff, Sparkles } from 'lucide-react'
import Dashboard from './pages/Dashboard'
import { AppProvider } from './context/AppContext'
import {
  login,
  register,
  requestPasswordReset,
  resetPassword,
  setAccessToken,
} from './services/api'

type AuthMode = 'login' | 'register' | 'forgot' | 'reset'

const PASSWORD_MIN_LENGTH = 12

function initialAuthMode() {
  // Reset links arrive from email with a token query parameter.
  return new URLSearchParams(window.location.search).get('token') ? 'reset' : 'login'
}

export default function App() {
  const [authMode, setAuthMode] = useState<AuthMode>(initialAuthMode)
  const [isAuthenticated, setIsAuthenticated] = useState(false)
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [resetToken, setResetToken] = useState(
    () => new URLSearchParams(window.location.search).get('token') ?? '',
  )
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [showPassword, setShowPassword] = useState(false)
  const [showConfirmPassword, setShowConfirmPassword] = useState(false)

  function showMode(nextMode: AuthMode) {
    // Clear transient form state so errors from one flow do not bleed into another.
    setAuthMode(nextMode)
    setError('')
    setSuccess('')
    setPassword('')
    setConfirmPassword('')
    setShowPassword(false)
    setShowConfirmPassword(false)
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setError('')
    setSuccess('')
    setSubmitting(true)

    try {
      if (authMode === 'forgot') {
        const result = await requestPasswordReset(email.trim())
        setSuccess(result.message)
        return
      }

      if (authMode === 'reset') {
        if (!resetToken.trim()) {
          setError('Password reset link is missing or invalid.')
          return
        }
        if (password !== confirmPassword) {
          setError('Passwords do not match.')
          return
        }
        await resetPassword(resetToken.trim(), password)
        window.history.replaceState({}, '', window.location.pathname)
        setResetToken('')
        showMode('login')
        setSuccess('Password has been reset. You can now sign in.')
        return
      }

      if (authMode === 'register') {
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
    setShowPassword(false)
    setShowConfirmPassword(false)
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
        <div className="mb-6 flex flex-col items-center text-center">
          <span className="mb-4 grid h-11 w-11 place-items-center rounded-full bg-gradient-to-br from-blue-600 to-indigo-500 text-white shadow-[0_8px_22px_rgba(37,99,235,.25)]"><Sparkles size={19} /></span>
          <p className="text-[11px] font-semibold uppercase tracking-[.14em] text-blue-600">Docsense AI</p>
          <h1 className="mt-2 text-2xl font-bold tracking-[-.03em] text-slate-900">
            {authMode === 'register' && 'Create your account'}
            {authMode === 'login' && 'Sign in to your workspace'}
            {authMode === 'forgot' && 'Reset your password'}
            {authMode === 'reset' && 'Choose a new password'}
          </h1>
          <p className="mt-2 text-sm leading-6 text-slate-500">
            {authMode === 'forgot'
              ? 'Enter your email and we will send secure reset instructions.'
              : authMode === 'reset'
                ? 'Create a strong password before returning to your workspace.'
                : 'Upload documents, retrieve owned sources, and ask questions through the secured API.'}
          </p>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          {authMode !== 'reset' && (
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
          )}

          {authMode !== 'forgot' && (
            <label className="block">
              <span className="text-xs font-semibold text-slate-600">
                {authMode === 'reset' ? 'New password' : 'Password'}
              </span>
              <span className="relative mt-1 block">
                <input
                  type={showPassword ? 'text' : 'password'}
                  value={password}
                  onChange={event => setPassword(event.target.value)}
                  className="h-11 w-full rounded-xl border border-[#e6ecf5] bg-white px-3 pr-11 text-sm outline-none focus:border-blue-500 focus:ring-4 focus:ring-blue-100/60"
                  autoComplete={authMode === 'login' ? 'current-password' : 'new-password'}
                  minLength={PASSWORD_MIN_LENGTH}
                  required
                />
                <button
                  type="button"
                  onClick={() => setShowPassword(current => !current)}
                  className="absolute right-2 top-1/2 grid h-8 w-8 -translate-y-1/2 place-items-center rounded-lg text-slate-500 hover:bg-[#f5f9ff] hover:text-blue-600 focus:outline-none focus:ring-4 focus:ring-blue-100/60"
                  aria-label={showPassword ? 'Hide password' : 'Show password'}
                  title={showPassword ? 'Hide password' : 'Show password'}
                >
                  {showPassword ? <EyeOff size={17} /> : <Eye size={17} />}
                </button>
              </span>
            </label>
          )}

          {authMode === 'reset' && (
            <label className="block">
              <span className="text-xs font-semibold text-slate-600">Confirm password</span>
              <span className="relative mt-1 block">
                <input
                  type={showConfirmPassword ? 'text' : 'password'}
                  value={confirmPassword}
                  onChange={event => setConfirmPassword(event.target.value)}
                  className="h-11 w-full rounded-xl border border-[#e6ecf5] bg-white px-3 pr-11 text-sm outline-none focus:border-blue-500 focus:ring-4 focus:ring-blue-100/60"
                  autoComplete="new-password"
                  minLength={PASSWORD_MIN_LENGTH}
                  required
                />
                <button
                  type="button"
                  onClick={() => setShowConfirmPassword(current => !current)}
                  className="absolute right-2 top-1/2 grid h-8 w-8 -translate-y-1/2 place-items-center rounded-lg text-slate-500 hover:bg-[#f5f9ff] hover:text-blue-600 focus:outline-none focus:ring-4 focus:ring-blue-100/60"
                  aria-label={showConfirmPassword ? 'Hide confirm password' : 'Show confirm password'}
                  title={showConfirmPassword ? 'Hide confirm password' : 'Show confirm password'}
                >
                  {showConfirmPassword ? <EyeOff size={17} /> : <Eye size={17} />}
                </button>
              </span>
            </label>
          )}

          {authMode === 'login' && (
            <button
              type="button"
              onClick={() => showMode('forgot')}
              className="-mt-2 block text-xs font-semibold text-blue-600 hover:text-blue-700"
            >
              Forgot password?
            </button>
          )}

          {error && (
            <p className="rounded-xl border border-red-200 bg-red-50 px-3 py-2 text-xs font-semibold text-red-700">
              {error}
            </p>
          )}
          {success && (
            <p className="rounded-xl border border-emerald-200 bg-emerald-50 px-3 py-2 text-xs font-semibold text-emerald-700">
              {success}
            </p>
          )}

          <button
            type="submit"
            disabled={submitting}
            className="h-11 w-full rounded-xl bg-gradient-to-br from-blue-600 to-indigo-500 px-4 text-sm font-semibold text-white shadow-[0_6px_18px_rgba(37,99,235,.22)] hover:-translate-y-0.5 hover:shadow-[0_9px_22px_rgba(37,99,235,.28)] disabled:cursor-not-allowed disabled:opacity-60"
          >
            {submitting && 'Please wait...'}
            {!submitting && authMode === 'register' && 'Register and sign in'}
            {!submitting && authMode === 'login' && 'Sign in'}
            {!submitting && authMode === 'forgot' && 'Send reset link'}
            {!submitting && authMode === 'reset' && 'Reset password'}
          </button>

          {authMode === 'login' || authMode === 'register' ? (
            <button
              type="button"
              onClick={() => showMode(authMode === 'register' ? 'login' : 'register')}
              className="h-10 w-full rounded-xl border border-[#e6ecf5] bg-white text-sm font-semibold text-slate-600 hover:bg-[#f5f9ff] hover:text-blue-600"
            >
              {authMode === 'register' ? 'Use an existing account' : 'Create an account'}
            </button>
          ) : (
            <button
              type="button"
              onClick={() => showMode('login')}
              className="h-10 w-full rounded-xl border border-[#e6ecf5] bg-white text-sm font-semibold text-slate-600 hover:bg-[#f5f9ff] hover:text-blue-600"
            >
              Back to sign in
            </button>
          )}
        </form>
      </section>
    </main>
  )
}
