import { useState, useEffect } from 'react'
import { C, S } from '../theme.js'
import { queue as queueApi } from '../api.js'
import { Toast, Badge, Loading, Empty, PageHeader, Mono } from '../components/ui.jsx'

export function QueueJobs() {
  const [status,    setStatus]    = useState(null)
  const [dead,      setDead]      = useState([])
  const [tasks,     setTasks]     = useState([])
  const [loading,   setLoading]   = useState(true)
  const [expanded,  setExpanded]  = useState(null)
  const [purging,   setPurging]   = useState(false)
  const [toast,     setToast]     = useState(null)
  // Enqueue form
  const [showEnq,   setShowEnq]   = useState(false)
  const [enqForm,   setEnqForm]   = useState({ task: '', priority: 'default', kwargs: '{}' })
  const [enqSaving, setEnqSaving] = useState(false)

  const showToast = (msg, type = 'success') => {
    setToast({ msg, type }); setTimeout(() => setToast(null), 4000)
  }

  const load = async () => {
    setLoading(true)
    try {
      const [st, dl, tk] = await Promise.all([
        queueApi.status(),
        queueApi.dead(50),
        queueApi.tasks(),
      ])
      setStatus(st)
      setDead(dl.jobs || [])
      setTasks(tk.tasks || [])
    } catch (e) {
      showToast(e.message, 'error')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [])

  const retry = async (jobId) => {
    try {
      await queueApi.retry(jobId)
      showToast(`Job ${jobId} requeued.`)
      load()
    } catch (e) { showToast(e.message, 'error') }
  }

  const purge = async () => {
    setPurging(true)
    try {
      const r = await queueApi.purge()
      showToast(`Purged ${r.purged} dead job(s).`)
      load()
    } catch (e) { showToast(e.message, 'error') }
    finally { setPurging(false) }
  }

  const enqueue = async () => {
    let kwargs = {}
    try { kwargs = JSON.parse(enqForm.kwargs || '{}') } catch {
      showToast('kwargs must be valid JSON.', 'error'); return
    }
    setEnqSaving(true)
    try {
      const r = await queueApi.enqueue({ task: enqForm.task, priority: enqForm.priority, kwargs })
      showToast(`Enqueued — job_id: ${r.job_id}`)
      setShowEnq(false)
    } catch (e) { showToast(e.message, 'error') }
    finally { setEnqSaving(false) }
  }

  if (loading) return <Loading />

  // Unavailable state (no redix)
  if (status && !status.available) {
    return (
      <div>
        <PageHeader title="Queue Jobs" />
        <div style={{ ...S.card, padding: 32, textAlign: 'center' }}>
          <div style={{ fontSize: 32, marginBottom: 12 }}>⟳</div>
          <div style={{ fontSize: 15, color: C.text, marginBottom: 8 }}>redix not available</div>
          <div style={{ fontSize: 13, color: C.textDim, maxWidth: 400, margin: '0 auto' }}>{status.detail}</div>
        </div>
      </div>
    )
  }

  const streams = status?.streams || {}
  const deadCount = status?.dead || 0

  return (
    <div>
      <PageHeader
        title="Queue Jobs"
        subtitle="Redis Streams queue. Dead-letter jobs can be retried or purged."
        action={
          <div style={{ display: 'flex', gap: 8 }}>
            <button style={S.btn('ghost')} onClick={load}>↺ Refresh</button>
            <button style={S.btn('primary')} onClick={() => setShowEnq(s => !s)}>+ Enqueue</button>
          </div>
        }
      />

      <Toast msg={toast?.msg} type={toast?.type} />

      {/* Enqueue form */}
      {showEnq && (
        <div style={{ ...S.card, marginBottom: 20, borderColor: C.accent + '55' }}>
          <div style={{ fontSize: 13, fontWeight: 600, color: C.text, marginBottom: 14 }}>Enqueue job</div>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginBottom: 12 }}>
            <div>
              <label style={S.label}>Task name</label>
              <select style={{ ...S.input, fontFamily: C.mono }}
                value={enqForm.task}
                onChange={e => setEnqForm(f => ({ ...f, task: e.target.value }))}>
                <option value="">— select task —</option>
                {tasks.map(t => <option key={t} value={t}>{t}</option>)}
              </select>
            </div>
            <div>
              <label style={S.label}>Priority</label>
              <select style={S.input}
                value={enqForm.priority}
                onChange={e => setEnqForm(f => ({ ...f, priority: e.target.value }))}>
                <option value="high">high</option>
                <option value="default">default</option>
                <option value="low">low</option>
              </select>
            </div>
          </div>
          <div style={{ marginBottom: 14 }}>
            <label style={S.label}>kwargs (JSON object)</label>
            <textarea
              style={{ ...S.input, fontFamily: C.mono, minHeight: 72, resize: 'vertical' }}
              value={enqForm.kwargs}
              onChange={e => setEnqForm(f => ({ ...f, kwargs: e.target.value }))}
              placeholder='{"user_id": "abc123"}'
            />
          </div>
          <div style={{ display: 'flex', gap: 8 }}>
            <button style={S.btn('primary')} onClick={enqueue}
              disabled={!enqForm.task || enqSaving}>
              {enqSaving ? 'Enqueueing…' : 'Enqueue'}
            </button>
            <button style={S.btn('ghost')} onClick={() => setShowEnq(false)}>Cancel</button>
          </div>
        </div>
      )}

      {/* Stream status cards */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 12, marginBottom: 24 }}>
        {[
          { label: 'high',    color: C.danger,  count: streams.high    || 0 },
          { label: 'default', color: C.accent,  count: streams.default || 0 },
          { label: 'low',     color: C.muted,   count: streams.low     || 0 },
          { label: 'dead',    color: C.warn,    count: deadCount },
        ].map(({ label, color, count }) => (
          <div key={label} style={S.card}>
            <div style={{ fontSize: 28, fontWeight: 700, color, fontFamily: C.mono }}>{count}</div>
            <div style={{ fontSize: 12, color: C.textDim, marginTop: 2 }}>{label} pending</div>
          </div>
        ))}
      </div>

      {/* Dead-letter table */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 10 }}>
        <div style={{ fontSize: 14, fontWeight: 600, color: C.text }}>
          Dead-letter queue <span style={{ ...S.pill(C.warn), marginLeft: 8 }}>{deadCount}</span>
        </div>
        {dead.length > 0 && (
          <button style={S.btn('danger')} onClick={purge} disabled={purging}>
            {purging ? 'Purging…' : 'Purge all'}
          </button>
        )}
      </div>

      {dead.length === 0 ? (
        <Empty msg="No dead-letter jobs — all workers healthy." />
      ) : (
        <div style={{ ...S.card, padding: 0, overflow: 'hidden' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead>
              <tr style={{ background: C.surfaceHigh }}>
                {['Job ID', 'Task', 'Attempts', 'Failed at', 'Error', 'Actions'].map(h => (
                  <th key={h} style={S.th}>{h.toUpperCase()}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {dead.map((j, i) => ([
                <tr key={j.id}
                  style={{ borderBottom: `1px solid ${C.border}`, cursor: 'pointer' }}
                  onClick={() => setExpanded(e => e === j.id ? null : j.id)}>
                  <td style={S.td}><Mono dim>{(j.id || '').slice(0, 8)}…</Mono></td>
                  <td style={S.td}><span style={S.pill(C.accent)}>{j.task}</span></td>
                  <td style={{ ...S.td, color: C.warn, fontFamily: C.mono }}>{j.attempts}</td>
                  <td style={{ ...S.td, fontFamily: C.mono, fontSize: 11 }}>
                    {j.failed_at ? new Date(j.failed_at * 1000).toLocaleString() : '—'}
                  </td>
                  <td style={{ ...S.td, maxWidth: 200, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', color: C.danger }}>
                    {j.error || '—'}
                  </td>
                  <td style={S.td}>
                    <button style={{ ...S.btn('success'), padding: '4px 10px', fontSize: 12 }}
                      onClick={e => { e.stopPropagation(); retry(j.id) }}>
                      Retry
                    </button>
                  </td>
                </tr>,
                expanded === j.id && (
                  <tr key={j.id + '-exp'} style={{ background: C.bg }}>
                    <td colSpan={6} style={{ padding: '12px 14px' }}>
                      <div style={{ fontSize: 11, color: C.textDim, marginBottom: 4 }}>PAYLOAD / TRACEBACK</div>
                      <pre style={{ margin: 0, fontFamily: C.mono, fontSize: 11, color: C.textDim, whiteSpace: 'pre-wrap' }}>
                        {JSON.stringify({ kwargs: j.kwargs, traceback: j.traceback }, null, 2)}
                      </pre>
                    </td>
                  </tr>
                )
              ]))}
            </tbody>
          </table>
        </div>
      )}

      <div style={{ marginTop: 10, fontSize: 11, color: C.textFaint }}>
        Transport: Redis Streams (XADD/consumer groups). Running jobs not individually enumerable. Dead-letter only.
      </div>
    </div>
  )
}
