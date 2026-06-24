import { useState, useEffect } from 'react'
import { C, S, genFldId } from '../theme.js'
import { schemas, migrate } from '../api.js'
import { FieldRow } from '../components/FieldRow.jsx'
import { Toast, Loading, Empty, PageHeader } from '../components/ui.jsx'

export function SchemaViewer({ embedded = false, refreshKey = 0 }) {
  const [tree,       setTree]       = useState(null)   // {plugins: {plugin: [{table, field_count}]}}
  const [selPlugin,  setSelPlugin]  = useState(null)
  const [selTable,   setSelTable]   = useState(null)
  const [fields,     setFields]     = useState(null)
  const [origPlugin, setOrigPlugin] = useState(null)
  const [dirty,      setDirty]      = useState(false)
  const [loading,    setLoading]    = useState(true)
  const [tableLoad,  setTableLoad]  = useState(false)
  const [saving,     setSaving]     = useState(false)
  const [migrating,  setMigrating]  = useState(false)
  const [migrateOut, setMigrateOut] = useState(null)
  const [preview,    setPreview]    = useState(false)
  const [toast,      setToast]      = useState(null)

  const showToast = (msg, type = 'success') => {
    setToast({ msg, type }); setTimeout(() => setToast(null), 5000)
  }

  useEffect(() => {
    setLoading(true)
    schemas.tree()
      .then(d => setTree(d))
      .catch(e => showToast(e.message, 'error'))
      .finally(() => setLoading(false))
  }, [refreshKey])

  const openTable = async (plugin, table) => {
    setSelPlugin(plugin); setSelTable(table)
    setFields(null); setDirty(false); setMigrateOut(null); setPreview(false)
    setTableLoad(true)
    try {
      const d = await schemas.table(table)
      setFields(d.fields.map(f => ({ ...f })))
      setOrigPlugin(d.plugin)
    } catch (e) {
      showToast(e.message, 'error')
    } finally {
      setTableLoad(false)
    }
  }

  const chg = (i, k, v) => { setFields(f => f.map((x, idx) => idx === i ? { ...x, [k]: v } : x)); setDirty(true) }
  const rm  = (i)       => { setFields(f => f.filter((_, idx) => idx !== i)); setDirty(true) }

  const addField = () => {
    const usedIds = new Set((fields || []).map(f => f.fld_id))
    setFields(f => [...(f || []), {
      fld_id: genFldId(usedIds), field_name: '', type: 'Data',
      reqd: false, unique: false, max_length: 140,
    }])
    setDirty(true)
  }

  const handleSave = async () => {
    setSaving(true)
    try {
      const res = await schemas.write(selTable, { plugin: origPlugin, fields })
      showToast(`Written → ${res.path}. Click Migrate to apply.`)
      setDirty(false)
      // Refresh tree counts
      const t = await schemas.tree()
      setTree(t)
    } catch (e) {
      showToast(e.message, 'error')
    } finally {
      setSaving(false)
    }
  }

  const handleMigrate = async () => {
    setMigrating(true); setMigrateOut(null)
    try {
      const res = await migrate.run(false)
      setMigrateOut(res)
      if (res.ok) showToast('Migration applied.')
      else showToast(res.stderr || 'Migration failed.', 'error')
    } catch (e) {
      showToast(e.message, 'error')
    } finally {
      setMigrating(false)
    }
  }

  if (loading) return <Loading />

  return (
    <div>
      {!embedded && (
        <PageHeader title="Schema Viewer" subtitle="Live schema from _field_registry. Edit inline → Save → Migrate." />
      )}
      <Toast msg={toast?.msg} type={toast?.type} />

      <div style={{ display: 'flex', gap: 0, minHeight: 500 }}>
        {/* Left tree */}
        <div style={{ width: 220, flexShrink: 0, background: C.surface,
          border: `1px solid ${C.border}`, borderRadius: 8,
          overflow: 'hidden', marginRight: 20 }}>
          <div style={{ padding: '10px 14px', borderBottom: `1px solid ${C.border}`,
            fontSize: 11, color: C.muted, fontWeight: 600, letterSpacing: '0.06em' }}>
            PLUGINS / TABLES
          </div>
          <div style={{ overflowY: 'auto', maxHeight: 600 }}>
            {tree && Object.entries(tree.plugins).map(([plugin, tables]) => (
              <div key={plugin}>
                <div style={{ padding: '8px 14px 4px', display: 'flex', alignItems: 'center', gap: 6 }}>
                  <span style={S.pill(C.accent)}>{plugin}</span>
                </div>
                {tables.map(t => {
                  const active = selPlugin === plugin && selTable === t.table
                  return (
                    <button key={t.table}
                      onClick={() => openTable(plugin, t.table)}
                      style={{
                        width: '100%', textAlign: 'left', border: 'none', cursor: 'pointer',
                        padding: '7px 14px 7px 22px', fontSize: 13,
                        background: active ? C.accentGlow : 'transparent',
                        color:      active ? C.accent     : C.textDim,
                        fontWeight: active ? 600           : 400,
                        borderLeft: active ? `2px solid ${C.accent}` : '2px solid transparent',
                        transition: 'all .12s',
                      }}>
                      <span style={{ fontFamily: C.mono, fontSize: 12 }}>{t.table}</span>
                      <span style={{ fontSize: 10, color: C.textFaint, marginLeft: 6 }}>{t.field_count}f</span>
                    </button>
                  )
                })}
              </div>
            ))}
          </div>
        </div>

        {/* Right panel */}
        <div style={{ flex: 1 }}>
          {!selTable ? (
            <Empty msg="Select a table from the list to view and edit its schema." />
          ) : tableLoad ? (
            <Loading />
          ) : (
            <div>
              {/* Header */}
              <div style={{ display: 'flex', alignItems: 'flex-start',
                justifyContent: 'space-between', marginBottom: 14 }}>
                <div>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 4 }}>
                    <h3 style={{ margin: 0, fontSize: 18, fontWeight: 600, color: C.text }}>{selTable}</h3>
                    <span style={S.pill(C.accent)}>{origPlugin}</span>
                    {dirty && <span style={S.pill(C.warn)}>unsaved</span>}
                  </div>
                  <div style={{ fontSize: 12, color: C.textDim, fontFamily: C.mono }}>
                    plugins/{origPlugin}/schemas/{selTable}.json · {(fields || []).length} fields
                  </div>
                </div>
                <div style={{ display: 'flex', gap: 8 }}>
                  <button style={S.btn('ghost')} onClick={() => setPreview(p => !p)}>
                    {preview ? 'Hide' : 'Preview'} JSON
                  </button>
                  <button style={S.btn('primary')} onClick={addField}>+ Add field</button>
                  <button style={S.btn(dirty ? 'warn' : 'ghost')}
                    onClick={handleSave} disabled={!dirty || saving}>
                    {saving ? 'Saving…' : '↓ Save'}
                  </button>
                  <button style={S.btn('default')} onClick={handleMigrate} disabled={migrating}>
                    {migrating ? '…' : 'Migrate →'}
                  </button>
                </div>
              </div>

              {/* System fields bar */}
              <div style={{ background: C.surfaceHigh, border: `1px solid ${C.border}`,
                borderRadius: 6, padding: '7px 14px', marginBottom: 12,
                fontSize: 12, color: C.textDim }}>
                <strong style={{ color: C.text }}>Auto-injected system fields — never modify:</strong>&nbsp;
                id, created_at, updated_at, created_by, updated_by, _state
              </div>

              {/* Fields */}
              {(fields || []).map((f, i) => (
                <FieldRow key={f.fld_id + i} field={f} index={i} onChg={chg} onRm={rm} />
              ))}
              {(fields || []).length === 0 && (
                <Empty msg="No fields. Add one above." />
              )}

              {/* JSON preview */}
              {preview && (
                <pre style={{ marginTop: 16, background: C.bg, border: `1px solid ${C.border}`,
                  borderRadius: 8, padding: 16, fontSize: 12, color: C.textDim,
                  fontFamily: C.mono, overflow: 'auto', maxHeight: 280 }}>
                  {JSON.stringify({ table: selTable, plugin: origPlugin, fields }, null, 2)}
                </pre>
              )}

              {/* Migrate output */}
              {migrateOut && (
                <pre style={{ marginTop: 12, background: C.bg, border: `1px solid ${C.border}`,
                  borderRadius: 8, padding: 14, fontSize: 12,
                  color: migrateOut.ok ? C.success : C.danger,
                  fontFamily: C.mono, overflow: 'auto', maxHeight: 200 }}>
                  {migrateOut.stdout || migrateOut.stderr || '(no output)'}
                </pre>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
