import { useEffect, useState } from 'react'

export default function DataList({ endpoint }) {
  const [items, setItems] = useState([])
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(Boolean(endpoint))

  useEffect(() => {
    let cancelled = false
    if (!endpoint) {
      setLoading(false)
      return () => { cancelled = true }
    }
    setLoading(true)
    fetch(endpoint)
      .then((response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`)
        return response.json()
      })
      .then((data) => {
        if (cancelled) return
        const rows = Array.isArray(data)
          ? data
          : data.items || data.data || data.records || data.results || []
        setItems(Array.isArray(rows) ? rows : [])
        setError('')
      })
      .catch((cause) => {
        if (!cancelled) setError(String(cause))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => { cancelled = true }
  }, [endpoint])

  const tableStyle = { width: '100%', borderCollapse: 'collapse', fontSize: 13 }
  const cellStyle = { border: '1px solid #cbd5e1', padding: '6px 8px', textAlign: 'left' }
  if (!endpoint) return <p style={{ color: '#64748b' }}>未配置数据端点</p>
  if (loading) return <p style={{ color: '#64748b' }}>加载中…</p>
  if (error) return <p style={{ color: '#b91c1c' }}>加载失败：{error}</p>
  if (!items.length) return <p style={{ color: '#64748b' }}>暂无数据</p>
  const columns = Object.keys(items[0]).slice(0, 8)
  return (
    <div data-testid="data-list" style={{ overflowX: 'auto' }}>
      <table style={tableStyle}>
        <thead>
          <tr>
            {columns.map((key) => (
              <th key={key} style={cellStyle}>{key}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {items.map((row, index) => (
            <tr key={row.id ?? index}>
              {columns.map((key) => (
                <td key={key} style={cellStyle}>
                  {typeof row[key] === 'object' ? JSON.stringify(row[key]) : String(row[key] ?? '')}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
