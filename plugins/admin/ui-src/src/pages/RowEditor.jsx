import { useState, useEffect } from 'react'
import { C, S } from '../theme.js'
import { schemas, rows as rowsApi } from '../api.js'
import { Toast, Loading, Empty, PageHeader, Mono } from '../components/ui.jsx'

export function RowEditor() {
  const [tree,       setTree]       = useState(null)
  const [treeLoad,   setTreeLoad]   = useState(true)
  const [selPlugin,  setSelPlugin]  = useState('')
  const [selTable,   setSelTable]   = useState('')
  const [schFields,  setSchFields]  = useState([])
  const [rowData,    setRowData]    = useState([])
  const [cursor,     setCursor]     = useState(null)
  const [hasMore,    setHasMore]    = useState(false)
  const [rowLoading, setRowLoading] = useState(false)
  const [showAdd,    setShowAdd]    = useState(false)
  const [editId,     setEditId]     = useState(null)
  const [form,       setForm]       = useState({})
  const [saving,     setSaving]     = useState(false)
  const [toast,      setToast]      = useState(null)

  const showToast = (msg, type = 'success') => {
    setToast({ msg, type }); setTimeout(() => setToast(null), 4000)
  }

  // Load schema tree on mount
  useEffect(() => {
    schemas.tree()
      .then(d => { setTree(d); const plugins = Object.keys(d.plugins || {}); if (plugins.length) setSelPlugin(plugins[0]) })
      .catch(e => showToast(e.message, 'error'))
      .finally(() => setTreeLoad(false))
  }, [])

  const tables = tree ? (tree.plugins[selPlugin] || []).map(t => t.table) : []

  // Load table schema + rows when table changes
  useEffect(() => {
    if (!selTable) { setSchFields([]); setRowData([]); return }
    setRowLoading(true); setSchFields([]); setRowData([]); setCursor(null)
    schemas.table(selTable)
      .then(d => setSchFields(d.fields || []))
      .catch(e => showToast(e.message, 'error'))
    rowsApi.list(selTable, 50, null)
      .then(d => { setRowData(d.data || []); setCursor(d.next_cursor || null); setHasMore(!!d.next_cursor) })
      .catch(e => showToast(e.message, 'error'))
      .finally(() => setRowLoading(false))
  }, [selTable])

  const loadMore = async () => {
    if (!cursor) return
    setRowLoading(true)
    try {
      const d = await rowsApi.list(selTable, 50, cursor)
      setRowData(r => [...r, ...(d.data || [])])
      setCursor(d.next_cursor || null)
      setHasMore(!!d.next_cursor)
    } catch (e) { showToast(e.message, 'error') }
    finally { setRowLoading(false) }
  }

  const columns = schFields.map(f => f.field_name)

  const handlePluginChange = (p) => {
    setSelPlugin(p); setSelTable(''); setShowAdd(false); setEditId(null); setForm({})
  }
  const handleTableChange = (t) => {
    setSelTable(t); setShowAdd(false); setEditId(null); setForm({})
  }

  const openAdd = () => { setShowAdd(true); setEditId(null); setForm({}) }
  const openEdit = (row) => { setEditId(row.id); setForm({ ...row }); setShowAdd(false) }
  const cancelForm = () => { setShowAdd(false); setEditId(null); setForm({}) }

  const handleSave = async () => {
    setSaving(true)
    try {
      if (editId) {
        const body = { ...form }; delete body.id
        await rowsApi.update(selTable, editId, body)
        setRowData(r => r.map(x => x.id === editId ? { ...x, ...body } : x))
        showToast('Row updated via arc.update.')
      } else {
        const row = await rowsApi.create(selTable, form)
        setRowData(r => [row, ...r])
        showToast('Row created via arc.save.')
      }
      cancelForm()
    } catch (e) { showToast(e.message, 'error') }
    finally { setSaving(false) }
  }

  const handleDelete = async (id) => {
    try {
      await rowsApi.remove(selTable, id)
      setRowData(r => r.filter(x => x.id !== id))
      showToast('Soft-deleted — _state set to 99.')
    } catch (e) { showToast(e.message, 'error') }
  }

  if (treeLoad) return <Loading />

  return (
    <div>
      <PageHeader
        title="Row Editor"
        subtitle={<>Browse and edit rows via <Mono>arc.list / arc.save / arc.update / arc.rm</Mono>.</>}
      />
      <Toast msg={toast?.msg} type={toast?.type} />

      {/* Selectors */}
      <div style={{ display: 'flex', gap: 12, alignItems: 'flex-end', marginBottom: 20 }}>
        <div style={{ width: 160 }}>
          <label style={S.label}>Plugin</label>
          <select style={{ ...S.input, fontFamily: C.mono }}
            value={selPlugin} onChange={e => handlePluginChange(e.target.value)}>
            {Object.keys(tree?.plugins || {}).map(p => <option key={p}>{p}</option>)}
          </select>
        </div>
        <div style={{ width: 200 }}>
          <label style={S.label}>Table</label>
          <select style={{ ...S.input, fontFamily: C.mono }}
            value={selTable} onChange={e => handleTableChange(e.target.value)}
            disabled={!selPlugin}>
            <option value="">— select table —</option>
            {tables.map(t => <option key={t}>{t}</option>)}
          </select>
        </div>
        {selTable && (
          <button style={S.btn('primary')} onClick={openAdd}>+ Add row</button>
        )}
      </div>

      {/* Add / Edit form */}
      {(showAdd || editId) && selTable && (
        <div style={{ ...S.card, marginBottom: 20, borderColor: C.accent + '55' }}>
          <div style={{ fontSize: 13, fontWeight: 600, color: C.text, marginBottom: 14 }}>
            {editId ? `Edit row — ${selTable}` : `New row — ${selTable}`}
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginBottom: 14 }}>
            {columns.map(col => {
              const f = schFields.find(x => x.field_name === col)
              return (
                <div key={col}>
                  <label style={S.label}>
                    {col}
                    {f?.reqd && <span style={{ color: C.danger }}> *</span>}
                    <span style={{ color: C.textFaint, marginLeft: 4, fontFamily: C.mono, fontSize: 10 }}>
                      {f?.type}
                    </span>
                  </label>
                  <input style={S.input} placeholder={col}
                    value={form[col] || ''}
                    onChange={e => setForm(fr => ({ ...fr, [col]: e.target.value }))} />
                </div>
              )
            })}
          </div>
          <div style={{ display: 'flex', gap: 8 }}>
            <button style={S.btn('primary')} onClick={handleSave} disabled={saving}>
              {saving ? 'Saving…' : editId ? 'Update row' : 'Save row'}
            </button>
            <button style={S.btn('ghost')} onClick={cancelForm}>Cancel</button>
          </div>
        </div>
      )}

      {/* Grid */}
      {!selTable ? (
        <Empty msg="Select a plugin and table to browse rows." />
      ) : rowLoading && rowData.length === 0 ? (
        <Loading />
      ) : rowData.length === 0 ? (
        <Empty msg={`No rows in ${selTable}.`} />
      ) : (
        <>
          <div style={{ ...S.card, padding: 0, overflow: 'auto' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse', minWidth: 500 }}>
              <thead>
                <tr style={{ background: C.surfaceHigh }}>
                  <th style={S.th}>ID</th>
                  {columns.map(c => <th key={c} style={{ ...S.th, fontFamily: C.mono }}>{c}</th>)}
                  <th style={{ ...S.th, width: 120 }}></th>
                </tr>
              </thead>
              <tbody>
                {rowData.map((row, i) => (
                  <tr key={row.id}
                    style={{ borderBottom: i < rowData.length - 1 ? `1px solid ${C.border}` : 'none',
                             background: editId === row.id ? C.accentGlow : 'transparent' }}>
                    <td style={S.td}><Mono dim>{String(row.id || '').slice(0, 8)}…</Mono></td>
                    {columns.map(col => (
                      <td key={col} style={{ ...S.td, maxWidth: 180,
                        overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                        {String(row[col] ?? '') || <span style={{ color: C.textFaint }}>—</span>}
                      </td>
                    ))}
                    <td style={S.td}>
                      <div style={{ display: 'flex', gap: 6 }}>
                        <button style={{ ...S.btn('default'), padding: '4px 10px', fontSize: 12 }}
                          onClick={() => openEdit(row)}>Edit</button>
                        <button style={{ ...S.btn('danger'), padding: '4px 10px', fontSize: 12 }}
                          onClick={() => handleDelete(row.id)}>Delete</button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {hasMore && (
            <div style={{ textAlign: 'center', marginTop: 12 }}>
              <button style={S.btn('ghost')} onClick={loadMore} disabled={rowLoading}>
                {rowLoading ? 'Loading…' : 'Load more'}
              </button>
            </div>
          )}
          <div style={{ marginTop: 8, fontSize: 11, color: C.textFaint }}>
            {rowData.length} row(s) · Delete = soft (_state=99) · Cursor pagination on id DESC
          </div>
        </>
      )}
    </div>
  )
}
