import { useState } from 'react'
import { C, S } from '../theme.js'
import { auth } from '../api.js'

export function Login({ onLogin }) {
  const [identifier, setIdentifier] = useState('')
  const [password, setPassword]     = useState('')
  const [loading, setLoading]       = useState(false)
  const [error, setError]           = useState(null)

  const submit = async () => {
    setError(null)
    if (!identifier || !password) { setError('Username and password are required.'); return }
    setLoading(true)
    try {
      const res = await auth.login(identifier, password)
      localStorage.setItem('arc_token', res.access_token)
      localStorage.setItem('arc_refresh_token', res.refresh_token || '')
      if (res.expires_in) {
        localStorage.setItem('arc_token_exp', String(Date.now() + res.expires_in * 1000))
      }
      // Fetch user identity to confirm is_superuser
      const me = await auth.me()
      if (!me.is_superuser) {
        localStorage.removeItem('arc_token')
        setError('Superuser access required. This panel is restricted to superusers.')
        return
      }
      localStorage.setItem('arc_user', JSON.stringify(me))
      onLogin(me)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }

  const onKey = (e) => { if (e.key === 'Enter') submit() }

  return (
    <div style={{
      minHeight: '100vh', background: C.bg,
      display: 'flex', alignItems: 'center', justifyContent: 'center',
    }}>
      <div style={{ width: 360 }}>
        {/* Logo */}
        <div style={{ textAlign: 'center', marginBottom: 32 }}>
          <div style={{
            width: 48, height: 48, borderRadius: 12, background: C.accent,
            display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
            fontSize: 24, color: '#fff', marginBottom: 12,
          }}>⬡</div>
          <div style={{ fontSize: 22, fontWeight: 700 }}>Arc Admin</div>
          <div style={{ fontSize: 13, color: C.textDim, marginTop: 4 }}>
            Superuser access only
          </div>
        </div>

        {/* Card */}
        <div style={{ ...S.card, padding: 28 }}>
          {error && (
            <div style={{
              background: C.danger + '11', border: `1px solid ${C.danger}44`,
              borderRadius: 6, padding: '10px 14px', marginBottom: 16,
              fontSize: 13, color: C.danger,
            }}>✕ {error}</div>
          )}

          <div style={{ marginBottom: 14 }}>
            <label style={S.label}>Username or email</label>
            <input
              style={S.input} placeholder="alice" value={identifier}
              autoFocus onChange={e => setIdentifier(e.target.value)}
              onKeyDown={onKey}
            />
          </div>
          <div style={{ marginBottom: 20 }}>
            <label style={S.label}>Password</label>
            <input
              style={S.input} type="password" placeholder="••••••••"
              value={password}
              onChange={e => setPassword(e.target.value)}
              onKeyDown={onKey}
            />
          </div>
          <button
            style={{ ...S.btn('primary'), width: '100%', justifyContent: 'center', padding: '10px 0' }}
            onClick={submit} disabled={loading}
          >
            {loading ? 'Signing in…' : 'Sign in'}
          </button>
        </div>
      </div>
    </div>
  )
}
