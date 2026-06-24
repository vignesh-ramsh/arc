import { useState, useEffect } from 'react'
import { C, S } from '../theme.js'
import { users as usersApi } from '../api.js'
import { Toast, Badge, Loading, Empty, PageHeader } from '../components/ui.jsx'

export function Users() {
  const [list,       setList]       = useState([])
  const [loading,    setLoading]    = useState(true)
  const [showCreate, setShowCreate] = useState(false)
  const [form,       setForm]       = useState({ username: '', email: '', password: '', is_superuser: false, roles: '' })
  const [saving,     setSaving]     = useState(false)
  const [toast,      setToast]      = useState(null)

  const showToast = (msg, type = 'success') => {
    setToast({ msg, type }); setTimeout(() => setToast(null), 4000)
  }

  const load = () => {
    setLoading(true)
    usersApi.list()
      .then(d => setList(d.users || []))
      .catch(e => showToast(e.message, 'error'))
      .finally(() => setLoading(false))
  }

  useEffect(load, [])

  const create = async () => {
    if (!form.username || !form.email || !form.password) {
      showToast('Username, email and password are required.', 'error'); return
    }
    setSaving(true)
    try {
      await usersApi.create({
        username: form.username, email: form.email, password: form.password,
        is_superuser: form.is_superuser,
        roles: form.roles ? form.roles.split(',').map(r => r.trim()).filter(Boolean) : [],
      })
      showToast(`User "${form.username}" created — password hashed with argon2id.`)
      setForm({ username: '', email: '', password: '', is_superuser: false, roles: '' })
      setShowCreate(false)
      load()
    } catch (e) {
      showToast(e.message, 'error')
    } finally {
      setSaving(false)
    }
  }

  const toggle = async (user) => {
    try {
      if (user.is_active) await usersApi.disable(user.username)
      else                await usersApi.enable(user.username)
      load()
    } catch (e) { showToast(e.message, 'error') }
  }

  if (loading) return <Loading />

  return (
    <div>
      <PageHeader
        title="Users"
        subtitle={<>Manage <span style={{ fontFamily: C.mono, fontSize: 12 }}>AuthUser</span> records via auth.context.</>}
        action={
          <button style={S.btn('primary')} onClick={() => setShowCreate(s => !s)}>
            + New user
          </button>
        }
      />

      <Toast msg={toast?.msg} type={toast?.type} />

      {/* Create form */}
      {showCreate && (
        <div style={{ ...S.card, marginBottom: 20, borderColor: C.accent + '55' }}>
          <div style={{ fontSize: 13, fontWeight: 600, color: C.text, marginBottom: 14 }}>Create user</div>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginBottom: 12 }}>
            {[
              ['Username', 'username', 'alice', 'text'],
              ['Email',    'email',    'alice@acme.io', 'email'],
              ['Password', 'password', 'min 8 chars', 'password'],
              ['Roles',    'roles',    'Admin, Staff', 'text'],
            ].map(([lbl, key, ph, t]) => (
              <div key={key}>
                <label style={S.label}>{lbl}</label>
                <input style={S.input} type={t} placeholder={ph}
                  value={form[key]}
                  onChange={e => setForm(f => ({ ...f, [key]: e.target.value }))} />
              </div>
            ))}
          </div>
          <label style={{ ...S.label, display: 'flex', alignItems: 'center', gap: 8, marginBottom: 14, cursor: 'pointer' }}>
            <input type="checkbox" checked={form.is_superuser}
              onChange={e => setForm(f => ({ ...f, is_superuser: e.target.checked }))} />
            Superuser — bypasses all role checks
          </label>
          <div style={{ display: 'flex', gap: 8 }}>
            <button style={S.btn('primary')} onClick={create} disabled={saving}>
              {saving ? 'Creating…' : 'Create user'}
            </button>
            <button style={S.btn('ghost')} onClick={() => setShowCreate(false)}>Cancel</button>
          </div>
        </div>
      )}

      {list.length === 0 ? <Empty msg="No users found." /> : (
        <div style={{ ...S.card, padding: 0, overflow: 'hidden' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead>
              <tr style={{ background: C.surfaceHigh }}>
                {['Username', 'Email', 'Roles', 'Superuser', 'Status', 'Joined', 'Actions'].map(h => (
                  <th key={h} style={S.th}>{h.toUpperCase()}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {list.map((u, i) => (
                <tr key={u.id} style={{ borderBottom: i < list.length - 1 ? `1px solid ${C.border}` : 'none' }}>
                  <td style={S.td}><span style={{ fontFamily: C.mono, fontSize: 12, color: C.text }}>{u.username}</span></td>
                  <td style={S.td}>{u.email}</td>
                  <td style={S.td}>
                    {(u.roles || []).length
                      ? (u.roles || []).map(r => <span key={r} style={{ ...S.pill(C.accent), marginRight: 4 }}>{r}</span>)
                      : <span style={{ color: C.textFaint }}>—</span>}
                  </td>
                  <td style={S.td}>
                    {u.is_superuser ? <span style={S.pill(C.warn)}>superuser</span> : <span style={{ color: C.textFaint }}>—</span>}
                  </td>
                  <td style={S.td}><Badge status={u.is_active ? 'active' : 'inactive'} /></td>
                  <td style={{ ...S.td, fontFamily: C.mono, fontSize: 11 }}>{(u.created_at || '').slice(0, 10)}</td>
                  <td style={S.td}>
                    <button style={{ ...S.btn(u.is_active ? 'danger' : 'success'), padding: '4px 10px', fontSize: 12 }}
                      onClick={() => toggle(u)}>
                      {u.is_active ? 'Disable' : 'Enable'}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
