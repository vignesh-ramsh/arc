// Design tokens — single source of truth for colours, typography, shared styles.
// Import this wherever you need a colour or a base style rule.

export const C = {
  bg:           '#0f1117',
  surface:      '#181c27',
  surfaceHigh:  '#1f2435',
  border:       '#2a2f42',
  borderLight:  '#353b52',
  accent:       '#6366f1',
  accentDim:    '#4338ca',
  accentGlow:   'rgba(99,102,241,0.15)',
  success:      '#22c55e',
  warn:         '#f59e0b',
  danger:       '#ef4444',
  muted:        '#6b7280',
  text:         '#e2e8f0',
  textDim:      '#94a3b8',
  textFaint:    '#4b5563',
  mono:         "'JetBrains Mono','Fira Code','Cascadia Code',monospace",
}

// Shared style factories
export const S = {
  pill: (color) => ({
    display: 'inline-flex', alignItems: 'center', padding: '2px 8px',
    borderRadius: 4, fontSize: 11, fontWeight: 600, letterSpacing: '0.04em',
    background: color + '22', color, border: `1px solid ${color}44`,
    fontFamily: C.mono,
  }),

  btn: (v = 'default') => ({
    display: 'inline-flex', alignItems: 'center', gap: 6,
    padding: '6px 14px', borderRadius: 6, border: '1px solid',
    fontSize: 13, fontWeight: 500, cursor: 'pointer', transition: 'all .15s',
    ...(v === 'primary' ? { background: C.accent,       borderColor: C.accent,   color: '#fff'       } : {}),
    ...(v === 'danger'  ? { background: C.danger+'22',  borderColor: C.danger,   color: C.danger     } : {}),
    ...(v === 'ghost'   ? { background: 'transparent',  borderColor: C.border,   color: C.textDim    } : {}),
    ...(v === 'success' ? { background: C.success+'22', borderColor: C.success,  color: C.success    } : {}),
    ...(v === 'warn'    ? { background: C.warn+'22',    borderColor: C.warn,     color: C.warn       } : {}),
    ...(v === 'default' ? { background: C.surfaceHigh,  borderColor: C.border,   color: C.text       } : {}),
  }),

  input: {
    background: C.surface, border: `1px solid ${C.border}`,
    borderRadius: 6, padding: '7px 10px', color: C.text,
    fontSize: 13, outline: 'none', width: '100%', boxSizing: 'border-box',
    fontFamily: 'inherit',
  },

  label: {
    fontSize: 12, color: C.textDim, marginBottom: 4,
    display: 'block', fontWeight: 500,
  },

  card: {
    background: C.surface, border: `1px solid ${C.border}`,
    borderRadius: 8, padding: 16,
  },

  th: {
    padding: '10px 14px', textAlign: 'left', fontSize: 11,
    color: C.muted, fontWeight: 600, letterSpacing: '0.06em',
    borderBottom: `1px solid ${C.border}`,
  },

  td: { padding: '11px 14px', fontSize: 13, color: C.textDim },
}

export const ARC_TYPES = [
  { value: 'Data',     pg: 'VARCHAR(n)',       note: 'Default 140 chars' },
  { value: 'Text',     pg: 'TEXT',             note: 'Unbounded' },
  { value: 'Int',      pg: 'INTEGER',          note: '' },
  { value: 'Float',    pg: 'DOUBLE PRECISION', note: '' },
  { value: 'Decimal',  pg: 'NUMERIC',          note: '' },
  { value: 'Bool',     pg: 'BOOLEAN',          note: '' },
  { value: 'Date',     pg: 'DATE',             note: '' },
  { value: 'Datetime', pg: 'TIMESTAMPTZ',      note: 'Always UTC' },
  { value: 'JSON',     pg: 'JSONB',            note: '' },
  { value: 'Link',     pg: 'UUID → FK',        note: 'Requires link_table' },
  { value: 'Email',    pg: 'VARCHAR(n)',        note: 'Format-validated' },
  { value: 'Password', pg: 'VARCHAR(255)',      note: 'Stripped from responses' },
]

export const SYSTEM_FIELDS = [
  'id', 'created_at', 'updated_at', 'created_by', 'updated_by', '_state',
]

export function genFldId(usedIds) {
  const alpha = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
  for (let i = 0; i < 676; i++) {
    const a = alpha[Math.floor(i / 26)]
    const b = alpha[i % 26]
    for (let n = 1; n <= 99; n++) {
      const id = `${a}${b}${String(n).padStart(2, '0')}`
      if (!usedIds.has(id)) return id
    }
  }
  return 'ZZ99'
}
