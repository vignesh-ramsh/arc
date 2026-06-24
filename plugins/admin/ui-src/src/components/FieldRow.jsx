import { C, S, ARC_TYPES } from '../theme.js'

// All field options on ONE aligned row:
// fld_id | field_name | type | required | unique | max_length | link_table | ✕
// Columns are a fixed grid so every row lines up vertically. The max_length and
// link_table cells stay reserved (rendered empty) when the type doesn't use
// them, so columns never shift.

const GRID = '68px minmax(110px,1fr) 122px 58px 60px 78px 116px 30px'

function MiniLabel({ children }) {
  return (
    <div style={{ fontSize: 10, color: C.textDim, marginBottom: 3,
      fontWeight: 500, height: 12, whiteSpace: 'nowrap', overflow: 'hidden' }}>
      {children}
    </div>
  )
}

export function FieldRow({ field, index, onChg, onRm, locked = false }) {
  const needsLink = field.type === 'Link'
  const needsLen  = field.type === 'Data' || field.type === 'Email'
  const borderColor = field.unique ? C.accent : locked ? C.textFaint : C.border

  const cell = { display: 'flex', flexDirection: 'column', justifyContent: 'flex-end' }
  const checkWrap = {
    ...cell, alignItems: 'center',
  }
  const checkBox = { width: 16, height: 16, cursor: locked ? 'default' : 'pointer', accentColor: C.accent }

  return (
    <div style={{
      ...S.card, marginBottom: 6, padding: '10px 12px',
      borderLeft: `3px solid ${borderColor}`,
      opacity: locked ? 0.55 : 1, position: 'relative',
    }}>
      <div style={{ display: 'grid', gridTemplateColumns: GRID, gap: 8, alignItems: 'end' }}>
        {/* fld_id */}
        <div style={cell}>
          <MiniLabel>fld_id</MiniLabel>
          <input
            style={{ ...S.input, fontFamily: C.mono, color: C.accent, padding: '6px 8px' }}
            value={field.fld_id} maxLength={4} disabled={locked}
            onChange={e => onChg(index, 'fld_id', e.target.value.toUpperCase())}
          />
        </div>

        {/* field_name */}
        <div style={cell}>
          <MiniLabel>field_name</MiniLabel>
          <input
            style={{ ...S.input, padding: '6px 8px' }} placeholder="field_name"
            value={field.field_name} disabled={locked}
            onChange={e => onChg(index, 'field_name', e.target.value)}
          />
        </div>

        {/* type */}
        <div style={cell}>
          <MiniLabel>type</MiniLabel>
          <select
            style={{ ...S.input, fontFamily: C.mono, padding: '6px 6px' }}
            value={field.type} disabled={locked}
            onChange={e => onChg(index, 'type', e.target.value)}
          >
            {ARC_TYPES.map(t => <option key={t.value} value={t.value}>{t.value}</option>)}
          </select>
        </div>

        {/* required */}
        <div style={checkWrap}>
          <MiniLabel>req</MiniLabel>
          <input type="checkbox" style={checkBox} checked={!!field.reqd} disabled={locked}
            onChange={e => onChg(index, 'reqd', e.target.checked)} />
        </div>

        {/* unique */}
        <div style={checkWrap}>
          <MiniLabel>unique</MiniLabel>
          <input type="checkbox" style={checkBox} checked={!!field.unique} disabled={locked}
            onChange={e => onChg(index, 'unique', e.target.checked)} />
        </div>

        {/* max_length (reserved cell) */}
        <div style={cell}>
          <MiniLabel>{needsLen ? 'length' : ''}</MiniLabel>
          {needsLen ? (
            <input style={{ ...S.input, padding: '6px 8px' }} type="number" min={1} max={65535}
              value={field.max_length || 140} disabled={locked}
              onChange={e => onChg(index, 'max_length', parseInt(e.target.value) || 140)} />
          ) : <div style={{ height: 31 }} />}
        </div>

        {/* link_table (reserved cell) */}
        <div style={cell}>
          <MiniLabel>{needsLink ? 'link_table' : ''}</MiniLabel>
          {needsLink ? (
            <input style={{ ...S.input, fontFamily: C.mono, padding: '6px 8px' }}
              placeholder="Table" value={field.link_table || ''} disabled={locked}
              onChange={e => onChg(index, 'link_table', e.target.value)} />
          ) : <div style={{ height: 31 }} />}
        </div>

        {/* delete */}
        <div style={{ ...cell, alignItems: 'center' }}>
          <MiniLabel>{locked ? 'sys' : ''}</MiniLabel>
          {!locked && onRm ? (
            <button
              style={{ ...S.btn('danger'), padding: '6px 8px', justifyContent: 'center' }}
              onClick={() => onRm(index)} title="Remove field">✕</button>
          ) : <div style={{ height: 31 }} />}
        </div>
      </div>
    </div>
  )
}
