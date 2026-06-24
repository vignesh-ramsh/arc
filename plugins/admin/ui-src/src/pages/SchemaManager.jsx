import { useState } from 'react'
import { S } from '../theme.js'
import { PageHeader } from '../components/ui.jsx'
import { SchemaViewer } from './SchemaViewer.jsx'
import { TableBuilder } from './TableBuilder.jsx'

// One tab for everything schema-related.
//   browse → SchemaViewer (tree + inline field editor)
//   create → TableBuilder (new table designer)
// "Add New Schema" switches to create; a successful save/migrate bumps
// refreshKey so the viewer's tree reloads when you go back.

export function SchemaManager() {
  const [mode, setMode]             = useState('browse')   // 'browse' | 'create'
  const [refreshKey, setRefreshKey] = useState(0)

  return (
    <div>
      <PageHeader
        title="Schema Manager"
        subtitle={mode === 'browse'
          ? 'Live schema from _field_registry. Edit inline → Save → Migrate.'
          : 'Design a new table schema, then save and migrate.'}
        action={mode === 'browse'
          ? <button style={S.btn('primary')} onClick={() => setMode('create')}>+ Add New Schema</button>
          : <button style={S.btn('ghost')} onClick={() => setMode('browse')}>← Back to schemas</button>}
      />

      {mode === 'browse'
        ? <SchemaViewer embedded refreshKey={refreshKey} />
        : <TableBuilder embedded onSaved={() => setRefreshKey(k => k + 1)} />}
    </div>
  )
}
