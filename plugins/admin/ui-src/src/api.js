// Centralised API client. All fetch calls go through here.
// Bearer token is read from localStorage on every request.
//
// Token auto-refresh:
//   • Reactive  — on a 401, transparently refresh once and retry the request.
//   • Proactive — a timer refreshes ~60s before the access token expires.
//   Both share a single-flight guard so concurrent calls trigger one refresh.

const ADMIN = '/api/v1/admin'
const AUTHN = '/api/v1/authn'

function token() {
  return localStorage.getItem('arc_token')
}

function clearSession() {
  localStorage.removeItem('arc_token')
  localStorage.removeItem('arc_refresh_token')
  localStorage.removeItem('arc_token_exp')
  localStorage.removeItem('arc_user')
}

// ── token refresh (single-flight) ─────────────────────────────────────────────
let refreshing = null

async function doRefresh() {
  const rt = localStorage.getItem('arc_refresh_token')
  if (!rt) return false
  try {
    const res = await fetch(`${AUTHN}/refresh`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: rt }),
    })
    if (!res.ok) return false
    const data = await res.json()
    localStorage.setItem('arc_token', data.access_token)
    // The refresh token ROTATES on every refresh — store the new one.
    if (data.refresh_token) localStorage.setItem('arc_refresh_token', data.refresh_token)
    if (data.expires_in) {
      localStorage.setItem('arc_token_exp', String(Date.now() + data.expires_in * 1000))
    }
    scheduleProactiveRefresh()   // re-arm the proactive timer
    return true
  } catch {
    return false
  }
}

function refreshOnce() {
  if (!refreshing) refreshing = doRefresh().finally(() => { refreshing = null })
  return refreshing
}

// ── proactive refresh timer ───────────────────────────────────────────────────
let refreshTimer = null

export function scheduleProactiveRefresh() {
  stopProactiveRefresh()
  const expMs = parseInt(localStorage.getItem('arc_token_exp') || '0', 10)
  if (!expMs || !localStorage.getItem('arc_refresh_token')) return
  const lead = 60 * 1000                       // refresh 60s before expiry
  const delay = Math.max(5000, expMs - Date.now() - lead)
  refreshTimer = setTimeout(async () => {
    const ok = await refreshOnce()
    if (ok) scheduleProactiveRefresh()
    else { clearSession(); window.location.hash = '#/login' }
  }, delay)
}

export function stopProactiveRefresh() {
  if (refreshTimer) { clearTimeout(refreshTimer); refreshTimer = null }
}

// ── core request ──────────────────────────────────────────────────────────────
async function req(method, path, body, _retried = false) {
  const headers = { 'Content-Type': 'application/json' }
  const t = token()
  if (t) headers['Authorization'] = `Bearer ${t}`

  const res = await fetch(path, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })

  // 401 → try a one-time refresh + retry (but never for the auth endpoints).
  if (res.status === 401 && !_retried && !path.includes('/authn/')) {
    const ok = await refreshOnce()
    if (ok) return req(method, path, body, true)
    clearSession()
    window.location.hash = '#/login'
    throw new Error('Session expired — please log in again.')
  }

  const data = await res.json().catch(() => ({}))
  if (!res.ok) {
    const msg = data?.error?.message || `Request failed (${res.status})`
    throw new Error(msg)
  }
  return data
}

// ── Auth ─────────────────────────────────────────────────────────────────────
export function getStoredUser() {
  try { return JSON.parse(localStorage.getItem('arc_user') || 'null') } catch { return null }
}
export function isLoggedIn() {
  return !!token() && !!getStoredUser()
}
export function logout() {
  clearSession()
  stopProactiveRefresh()
}

export const auth = {
  login:  (username, password) => req('POST', `${AUTHN}/login`, { username, password }),
  me:     ()                   => req('GET',  `${AUTHN}/me`),
  logout: (refreshToken)       => req('POST', `${AUTHN}/logout`, { refresh_token: refreshToken }),
}

// ── Schema ────────────────────────────────────────────────────────────────────
export const schemas = {
  tree:  ()             => req('GET',  `${ADMIN}/schemas`),
  table: (table)        => req('GET',  `${ADMIN}/schemas/${table}`),
  write: (table, body)  => req('POST', `${ADMIN}/schemas/${table}`, body),
}

// ── Migrate ───────────────────────────────────────────────────────────────────
export const migrate = {
  plan: ()                   => req('POST', `${ADMIN}/migrate/plan`),
  run:  (confirmDestructive) => req('POST', `${ADMIN}/migrate`, { confirm_destructive: !!confirmDestructive }),
}

// ── Users ─────────────────────────────────────────────────────────────────────
export const users = {
  list:        ()                   => req('GET',  `${ADMIN}/users`),
  create:      (body)               => req('POST', `${ADMIN}/users`, body),
  enable:      (username)           => req('POST', `${ADMIN}/users/${username}/enable`),
  disable:     (username)           => req('POST', `${ADMIN}/users/${username}/disable`),
  setPassword: (username, password) => req('POST', `${ADMIN}/users/${username}/set-password`, { password }),
  sessions:    (username)           => req('GET',  `${ADMIN}/users/${username}/sessions`),
}

// ── Rows ──────────────────────────────────────────────────────────────────────
export const rows = {
  list:   (table, limit = 50, cursor = null) =>
            req('GET', `${ADMIN}/rows/${table}?limit=${limit}${cursor ? '&cursor=' + encodeURIComponent(cursor) : ''}`),
  create: (table, body)     => req('POST',   `${ADMIN}/rows/${table}`, body),
  update: (table, id, body) => req('PATCH',  `${ADMIN}/rows/${table}/${id}`, body),
  remove: (table, id)       => req('DELETE', `${ADMIN}/rows/${table}/${id}`),
}

// ── Queue ─────────────────────────────────────────────────────────────────────
export const queue = {
  status:  ()           => req('GET',  `${ADMIN}/queue/status`),
  dead:    (limit = 50) => req('GET',  `${ADMIN}/queue/dead?limit=${limit}`),
  retry:   (jobId)      => req('POST', `${ADMIN}/queue/dead/${jobId}/retry`),
  purge:   ()           => req('POST', `${ADMIN}/queue/dead/purge`),
  job:     (jobId)      => req('GET',  `${ADMIN}/queue/jobs/${jobId}`),
  tasks:   ()           => req('GET',  `${ADMIN}/queue/tasks`),
  enqueue: (body)       => req('POST', `${ADMIN}/queue/enqueue`, body),
}
