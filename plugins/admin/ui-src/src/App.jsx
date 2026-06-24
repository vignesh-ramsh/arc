import { useState, useEffect } from 'react'
import { C, S } from './theme.js'
import { isLoggedIn, getStoredUser, logout, auth, scheduleProactiveRefresh, stopProactiveRefresh } from './api.js'
import { Login } from './pages/Login.jsx'
import { SchemaManager } from './pages/SchemaManager.jsx'
import { Users } from './pages/Users.jsx'
import { QueueJobs } from './pages/QueueJobs.jsx'
import { RowEditor } from './pages/RowEditor.jsx'

const NAV = [
  { hash: '#/schema', icon: '◈', label: 'Schema Manager' },
  { hash: '#/users',  icon: '◎', label: 'Users' },
  { hash: '#/jobs',   icon: '⟳', label: 'Queue Jobs' },
  { hash: '#/rows',   icon: '▤', label: 'Row Editor' },
]

const DEFAULT_HASH = '#/schema'
const CONTENT_MAX_WIDTH = 1080

function currentHash() {
  return window.location.hash || DEFAULT_HASH
}

export function App() {
  const [hash, setHash] = useState(currentHash)
  const [user, setUser] = useState(getStoredUser)

  useEffect(() => {
    const handle = () => setHash(currentHash())
    window.addEventListener('hashchange', handle)
    return () => window.removeEventListener('hashchange', handle)
  }, [])

  // Start proactive token refresh while logged in; stop on logout/unmount.
  useEffect(() => {
    if (isLoggedIn()) scheduleProactiveRefresh()
    return () => stopProactiveRefresh()
  }, [user])

  // Guard: redirect to login if not authenticated
  useEffect(() => {
    if (hash === '#/login') return
    if (!isLoggedIn()) window.location.hash = '#/login'
  }, [hash])

  const handleLogin = (userData) => {
    setUser(userData)
    scheduleProactiveRefresh()
    window.location.hash = DEFAULT_HASH
  }

  const handleLogout = async () => {
    try { await auth.logout(localStorage.getItem('arc_refresh_token') || '') } catch {}
    logout()
    setUser(null)
    window.location.hash = '#/login'
  }

  if (hash === '#/login' || !isLoggedIn()) {
    return <Login onLogin={handleLogin} />
  }

  const page = {
    '#/schema': <SchemaManager />,
    '#/users':  <Users />,
    '#/jobs':   <QueueJobs />,
    '#/rows':   <RowEditor />,
  }[hash] || <SchemaManager />

  return (
    <div style={{ display: 'flex', height: '100vh', overflow: 'hidden' }}>
      {/* Sidebar */}
      <aside style={{
        width: 220, flexShrink: 0, background: C.surface,
        borderRight: `1px solid ${C.border}`,
        display: 'flex', flexDirection: 'column',
        height: '100vh', overflowY: 'auto',
      }}>
        {/* Logo */}
        <div style={{ padding: '20px 20px 16px', borderBottom: `1px solid ${C.border}` }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <div style={{
              width: 32, height: 32, borderRadius: 8, background: C.accent,
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              fontSize: 16, fontWeight: 700, color: '#fff',
            }}>⬡</div>
            <div>
              <div style={{ fontWeight: 700, fontSize: 15, letterSpacing: '-0.01em' }}>Arc</div>
              <div style={{ fontSize: 11, color: C.muted }}>Admin Panel</div>
            </div>
          </div>
        </div>

        {/* Nav */}
        <nav style={{ padding: '12px 10px', flex: 1 }}>
          {NAV.map(item => {
            const active = hash === item.hash
            return (
              <a key={item.hash} href={item.hash}
                style={{
                  display: 'flex', alignItems: 'center', gap: 10,
                  padding: '9px 10px', borderRadius: 6, marginBottom: 2,
                  textDecoration: 'none',
                  background: active ? C.accentGlow : 'transparent',
                  color:      active ? C.accent     : C.textDim,
                  fontWeight: active ? 600           : 400,
                  fontSize: 13, transition: 'all .12s',
                }}>
                <span style={{ fontFamily: C.mono, fontSize: 15, opacity: 0.8 }}>{item.icon}</span>
                {item.label}
              </a>
            )
          })}
        </nav>

        {/* User + logout */}
        <div style={{ padding: '12px 16px', borderTop: `1px solid ${C.border}` }}>
          {user && (
            <div style={{ fontSize: 12, color: C.textDim, marginBottom: 8, fontFamily: C.mono,
              overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {user.email}
            </div>
          )}
          <button style={{ ...S.btn('ghost'), width: '100%', justifyContent: 'center', fontSize: 12 }}
            onClick={handleLogout}>
            Sign out
          </button>
          <div style={{ fontSize: 10, color: C.textFaint, marginTop: 8, fontFamily: C.mono }}>
            arc v2 · python 3.12 · pg 16
          </div>
        </div>
      </aside>

      {/* Main content — centered with a max width, empty gutters on wide screens */}
      <main style={{ flex: 1, overflowY: 'auto', background: C.bg }}>
        <div style={{ maxWidth: CONTENT_MAX_WIDTH, margin: '0 auto', padding: '32px 32px 64px' }}>
          {page}
        </div>
      </main>
    </div>
  )
}
