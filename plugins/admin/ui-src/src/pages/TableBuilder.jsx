import { useState } from 'react'
import { C, S, ARC_TYPES, SYSTEM_FIELDS, genFldId } from '../theme.js'
import { schemas, migrate } from '../api.js'
import { FieldRow } from '../components/FieldRow.jsx'
import { Toast, PageHeader, Loading } from '../components/ui.jsx'

export function TableBuilder({ embedded = false, onSaved }) {
  const [tableName,  setTableName]  = useState('')
  const [pluginName, setPluginName] = useState('')
  const [fields,     setFields]     = useState([])
  const [preview,    setPreview]    = useState(false)
  const [toast,      setToast]      = useState(null)
  const [saving,     setSaving]     = useState(false)
  const [migrating,  setMigrating]  = useState(false)
  const [migrateOut, setMigrateOut] = useState(null)

  const showToast = (msg, type = 'success') => {
    setToast({ msg, type })
    setTimeout(() => setToast(null), 5000)
  }

  const usedIds = new Set(fields.map(f => f.fld_id))

  const addField = () => setFields(f => [...f, {
    fld_id: genFldId(usedIds), field_name: '', type: 'Data',
    reqd: false, unique: false, max_length: 140,
  }])

  const chg = (i, k, v) => setFields(f => f.map((x, idx) => idx === i ? { ...x, [k]: v } : x))
  const rm  = (i)       => setFields(f => f.filter((_, idx) => idx !== i))

  const errs = validate(tableName, pluginName, fields)
  const schema = { table: tableName, plugin: pluginName, fields }
  const path = pluginName && tableName
    ? `plugins/${pluginName}/schemas/${tableName}.json`
    : 'plugins/<plugin>/schemas/<Table>.json'

  const handleSave = async () => {
    if (errs.length) return
    setSaving(true)
    try {
      const res = await schemas.write(tableName, { plugin: pluginName, fields })
      showToast(`Written → ${res.path} (${res.bytes} bytes). Run Migrate to apply.`)
      onSaved && onSaved()
    } catch (e) {
      showToast(e.message, 'error')
    } finally {
      setSaving(false)
    }
  }

  const handleMigratePlan = async () => {
    setMigrating(true); setMigrateOut(null)
    try {
      const res = await migrate.plan()
      setMigrateOut(res)
    } catch (e) {
      showToast(e.message, 'error')
    } finally {
      setMigrating(false)
    }
  }

  const handleMigrate = async () => {
    setMigrating(true); setMigrateOut(null)
    try {
      const res = await migrate.run(false)
      setMigrateOut(res)
      if (res.ok) { showToast('Migration applied.'); onSaved && onSaved() }
      else showToast(res.stderr || 'Migration failed.', 'error')
    } catch (e) {
      showToast(e.message, 'error')
    } finally {
      setMigrating(false)
    }
  }

  return (
    <div>
      {!embedded && (
        <PageHeader
          title="Table Builder"
          subtitle={<>Design a new table. Saves <code style={{ fontFamily: C.mono, fontSize: 12 }}>{path}</code> on the server — then run Migrate.</>}
        />
      )}

      {/* Table + plugin */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16, marginBottom: 16 }}>
        <div>
          <label style={S.label}>Table name <span style={{ color: C.danger }}>*</span></label>
          <input style={{ ...S.input, fontFamily: C.mono, fontSize: 15 }}
            placeholder="Employee" value={tableName}
            onChange={e => setTableName(e.target.value)} />
          <div style={{ fontSize: 11, color: C.textFaint, marginTop: 4 }}>PascalCase · [A-Za-z][A-Za-z0-9_]*</div>
        </div>
        <div>
          <label style={S.label}>Plugin <span style={{ color: C.danger }}>*</span></label>
          <input style={{ ...S.input, fontFamily: C.mono, fontSize: 15 }}
            placeholder="hrms" value={pluginName}
            onChange={e => setPluginName(e.target.value)} />
        </div>
      </div>

      {/* Path hint */}
      <div style={{ background: C.accentGlow, border: `1px solid ${C.accent}33`,
        borderRadius: 6, padding: '8px 12px', marginBottom: 16, fontSize: 12 }}>
        <span style={{ color: C.textDim }}>Output → </span>
        <span style={{ fontFamily: C.mono }}>{path}</span>
      </div>

      {/* System fields notice */}
      <div style={{ background: C.surfaceHigh, border: `1px solid ${C.border}`,
        borderRadius: 6, padding: '8px 14px', marginBottom: 16, fontSize: 12, color: C.textDim }}>
        <strong style={{ color: C.text }}>Auto-injected (never declare):</strong>&nbsp;
        {SYSTEM_FIELDS.map(f => <span key={f} style={{ fontFamily: C.mono, marginLeft: 4 }}>{f}</span>)}
      </div>

      {/* Fields */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 10 }}>
        <label style={{ ...S.label, margin: 0, fontSize: 13 }}>Fields ({fields.length})</label>
        <button style={S.btn('primary')} onClick={addField}>+ Add field</button>
      </div>

      {fields.length === 0 && (
        <div style={{ ...S.card, textAlign: 'center', padding: 32, color: C.textDim, fontSize: 13 }}>
          No fields yet. Add at least one and mark one as unique (business key).
        </div>
      )}
      {fields.map((f, i) => <FieldRow key={i} field={f} index={i} onChg={chg} onRm={rm} />)}

      {/* Validation errors */}
      {errs.length > 0 && fields.length > 0 && (
        <div style={{ background: C.danger + '11', border: `1px solid ${C.danger}44`,
          borderRadius: 6, padding: '10px 14px', marginBottom: 12 }}>
          {errs.map((e, i) => <div key={i} style={{ fontSize: 12, color: C.danger }}>✕ {e}</div>)}
        </div>
      )}

      <Toast msg={toast?.msg} type={toast?.type} />

      {/* Actions */}
      <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', alignItems: 'center', marginTop: 8 }}>
        <button style={S.btn(errs.length ? 'ghost' : 'primary')}
          onClick={handleSave} disabled={!!errs.length || saving}>
          {saving ? 'Saving…' : `↓ Save ${tableName || 'Table'}.json`}
        </button>
        <button style={S.btn('ghost')} onClick={() => setPreview(p => !p)}>
          {preview ? 'Hide' : 'Preview'} JSON
        </button>
        <button style={S.btn('default')} onClick={handleMigratePlan} disabled={migrating}>
          {migrating ? 'Running…' : 'Preview migration'}
        </button>
        <button style={S.btn('warn')} onClick={handleMigrate} disabled={migrating}>
          {migrating ? 'Running…' : 'Migrate →'}
        </button>
      </div>

      {/* JSON preview */}
      {preview && (
        <pre style={{ marginTop: 16, background: C.bg, border: `1px solid ${C.border}`,
          borderRadius: 8, padding: 16, fontSize: 12, color: C.textDim,
          fontFamily: C.mono, overflow: 'auto', maxHeight: 320 }}>
          {JSON.stringify(schema, null, 2)}
        </pre>
      )}

      {/* Migrate output */}
      {migrateOut && (
        <div style={{ marginTop: 16 }}>
          <div style={{ fontSize: 12, color: C.textDim, marginBottom: 6 }}>
            MIGRATE OUTPUT (exit {migrateOut.returncode})
          </div>
          <pre style={{ background: C.bg, border: `1px solid ${C.border}`,
            borderRadius: 8, padding: 16, fontSize: 12,
            color: migrateOut.ok ? C.success : C.danger,
            fontFamily: C.mono, overflow: 'auto', maxHeight: 320 }}>
            {migrateOut.stdout || migrateOut.stderr || '(no output)'}
          </pre>
        </div>
      )}
    </div>
  )
}

// Client-side structural validation (mirrors schema_io.py — server re-validates)
function validate(table, plugin, fields) {
  const errs = []
  if (!table.match(/^[A-Za-z][A-Za-z0-9_]*$/)) errs.push('Table name must match [A-Za-z][A-Za-z0-9_]*')
  if (!plugin.match(/^[a-z][a-z0-9_]*$/))       errs.push('Plugin must be lowercase snake_case')
  if (!fields.some(f => f.unique))               errs.push('At least one field must be unique (business key)')
  const names = fields.map(f => f.field_name.toLowerCase())
  const dups  = names.filter((n, i) => n && names.indexOf(n) !== i)
  if (dups.length) errs.push(`Duplicate field names: ${[...new Set(dups)].join(', ')}`)
  fields.forEach((f, i) => {
    if (!f.field_name)                              errs.push(`Field ${i + 1}: name is required`)
    if (SYSTEM_FIELDS.includes(f.field_name))       errs.push(`Field ${i + 1}: "${f.field_name}" is reserved`)
    if (f.type === 'Link' && !f.link_table)         errs.push(`Field ${i + 1}: Link requires link_table`)
  })
  return errs
}
