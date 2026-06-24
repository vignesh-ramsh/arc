import { useState } from 'react'
import { C, S } from '../theme.js'

// ── Toast ─────────────────────────────────────────────────────────────────────
export function Toast({ msg, type = 'success' }) {
  if (!msg) return null
  const color = type === 'error' ? C.danger : C.success
  return (
    <div style={{
      background: color + '11', border: `1px solid ${color}44`,
      borderRadius: 6, padding: '10px 14px', marginBottom: 16,
      fontSize: 13, color,
    }}>
      {type === 'error' ? '✕' : '✓'} {msg}
    </div>
  )
}

export function useToast() {
  const [toast, setToast] = useState(null)
  const show = (msg, type = 'success') => {
    setToast({ msg, type })
    setTimeout(() => setToast(null), 4000)
  }
  return [toast, show]
}

// ── Badge ─────────────────────────────────────────────────────────────────────
const STATUS_COLORS = {
  pending:   C.warn,
  running:   C.accent,
  success:   C.success,
  failed:    C.danger,
  active:    C.success,
  inactive:  C.muted,
  dead:      C.danger,
}

export function Badge({ status }) {
  const color = STATUS_COLORS[status] || C.muted
  return <span style={S.pill(color)}>{status}</span>
}

// ── Spinner ───────────────────────────────────────────────────────────────────
export function Spinner({ size = 20 }) {
  return (
    <div style={{
      width: size, height: size, borderRadius: '50%',
      border: `2px solid ${C.border}`,
      borderTopColor: C.accent,
      animation: 'spin .7s linear infinite',
      display: 'inline-block',
    }} />
  )
}

// ── Mono ──────────────────────────────────────────────────────────────────────
export function Mono({ children, dim, size = 12 }) {
  return (
    <span style={{ fontFamily: C.mono, fontSize: size, color: dim ? C.textDim : C.text }}>
      {children}
    </span>
  )
}

// ── Page header ───────────────────────────────────────────────────────────────
export function PageHeader({ title, subtitle, action }) {
  return (
    <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', marginBottom: 24 }}>
      <div>
        <h2 style={{ color: C.text, margin: '0 0 4px', fontSize: 20, fontWeight: 600 }}>{title}</h2>
        {subtitle && <p style={{ color: C.textDim, margin: 0, fontSize: 13 }}>{subtitle}</p>}
      </div>
      {action && <div>{action}</div>}
    </div>
  )
}

// ── Empty state ───────────────────────────────────────────────────────────────
export function Empty({ msg = 'No data.' }) {
  return (
    <div style={{ ...S.card, textAlign: 'center', padding: 40, color: C.textDim, fontSize: 13 }}>
      {msg}
    </div>
  )
}

// ── Loading state ─────────────────────────────────────────────────────────────
export function Loading() {
  return (
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 48 }}>
      <Spinner size={28} />
    </div>
  )
}
