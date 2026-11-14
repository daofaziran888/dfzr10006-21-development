import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { downloadUrl, request } from '@/api/client'

const ENDPOINT = '/api/subscribe'
const columns = [
  "任务编号",
  "数据源名称",
  "推送目标",
  "推送格式",
  "推送频率",
  "过滤条件",
  "最近推送",
  "任务状态"
]
const extraColumns = ["应用名称", "可恢复位置", "可恢复"]
const actions = [
  "启动推送",
  "暂停推送",
  "归档任务"
]
const statuses = [
  "草稿",
  "推送中",
  "已暂停",
  "已完成"
]

type Row = Record<string, string | number | boolean | null>
type ImportError = {
  行号: number | string
  任务编号: string
  数据源名称: string
  推送频率: string
  过滤条件: string
  错误原因: string
  原始数据: string
}
type ImportGroup = {
  index: number
  merge_key: string
  source: string
  frequency: string
  rows: number
  duplicates: number
  action: string
}
type ImportResult = {
  ok: boolean
  message: string
  batch_id: string
  status: string
  total: number
  created: number
  updated: number
  watermark_only: number
  duplicates: number
  failed: number
  groups: ImportGroup[]
  errors: ImportError[]
  error_list_url: string
}
type PushRecord = Record<string, string | number | null>
type Batch = ImportResult & { file_name: string; created_at: string }

const emptyResult: ImportResult = {
  ok: true, message: '', batch_id: '', status: '', total: 0, created: 0, updated: 0,
  watermark_only: 0, duplicates: 0, failed: 0, groups: [], errors: [], error_list_url: '',
}

export default function SubscribeView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [noticeMessage, setNoticeMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [stats, setStats] = useState<Record<string, number>>({})
  const [batches, setBatches] = useState<Batch[]>([])
  const [result, setResult] = useState<ImportResult>(emptyResult)
  const [importing, setImporting] = useState(false)
  const [selected, setSelected] = useState<Row | null>(null)
  const [pushes, setPushes] = useState<PushRecord[]>([])
  const [applications, setApplications] = useState<Record<string, string | number>[]>([])
  const fileRef = useRef<HTMLInputElement | null>(null)
  const filterFields = useMemo(() => [columns[0], columns[1], '应用名称'], [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    // 表单字段用中文列名，后端按 keyword/source/application/status 收口，这里显式映射。
    const query = new URLSearchParams()
    if (filters[columns[0]]) query.set('keyword', filters[columns[0]])
    if (filters[columns[1]]) query.set('source', filters[columns[1]])
    if (filters['应用名称']) query.set('application', filters['应用名称'])
    if (filters.status) query.set('status', filters.status)
    try {
      const [listResponse, statsResponse, batchResponse, appResponse] = await Promise.all([
        request(`${ENDPOINT}?${query}`),
        request(`${ENDPOINT}/stats`),
        request(`${ENDPOINT}/imports?size=5`),
        request('/api/whitelist/applications'),
      ])
      if (!listResponse.ok) {
        throw new Error('订阅任务列表读取失败')
      }
      const payload = await listResponse.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
      if (statsResponse.ok) setStats(await statsResponse.json())
      if (batchResponse.ok) setBatches((await batchResponse.json()).items ?? [])
      if (appResponse.ok) setApplications((await appResponse.json()).items ?? [])
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '数据订阅推送列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setNoticeMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ action }),
      })
      const payload = await response.json().catch(() => ({}))
      if (!response.ok || payload.ok === false) {
        throw new Error(payload.message ?? '数据订阅推送动作未生效，请稍后重试')
      }
      setNoticeMessage(payload.message ?? '')
      await reload()
      if (selected && selected.id === row.id) await openDetail(row)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '数据订阅推送操作失败')
    }
  }

  async function openDetail(row: Row) {
    setSelected(row)
    setPushes([])
    try {
      const response = await request(`${ENDPOINT}/${row.id}/pushes`)
      if (!response.ok) throw new Error('最近推送记录读取失败')
      const payload = await response.json()
      setPushes(payload.items ?? [])
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '最近推送记录读取失败')
    }
  }

  async function submitImport() {
    const file = fileRef.current?.files?.[0]
    if (!file) {
      setErrorMessage('请先选择要导入的 CSV 或 JSON 订阅任务清单')
      return
    }
    setImporting(true)
    setErrorMessage('')
    setNoticeMessage('')
    try {
      const form = new FormData()
      form.append('file', file)
      const response = await request(`${ENDPOINT}/imports`, { method: 'POST', body: form })
      if (!response.ok) {
        throw new Error('导入请求失败，请确认文件格式与大小')
      }
      const payload: ImportResult = await response.json()
      setResult(payload)
      if (payload.ok) {
        setNoticeMessage(payload.message)
      } else {
        setErrorMessage(payload.message)
      }
      if (fileRef.current) fileRef.current.value = ''
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '订阅任务清单导入失败')
    } finally {
      setImporting(false)
    }
  }

  function download(path: string) {
    window.open(downloadUrl(path), '_blank')
  }

  const statCards = [
    { label: '订阅任务总数', value: stats.total ?? total },
    { label: '推送中任务', value: stats.pushing ?? 0 },
    { label: '已暂停任务', value: stats.paused ?? 0 },
    { label: '可恢复水位', value: stats.recoverable ?? 0 },
    { label: '最近推送记录', value: stats.push_records ?? 0 },
  ]

  return (
    <section className="page" data-module="subscribe">
      <header className="page-head">
        <div>
          <h2>数据订阅推送管理</h2>
          <p className="page-desc">
            维护数据订阅的推送目标、格式与频率。同一「数据源名称 + 推送频率」合并为可恢复任务，
            重复导入只推进增量水位，过滤条件变化时保留旧最近推送记录并新建水位。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => fileRef.current?.click()}>
            导入订阅任务清单
          </button>
          <button className="btn" type="button" onClick={() => download(`${ENDPOINT}/export?format=csv`)}>
            导出清单（CSV）
          </button>
          <button className="btn" type="button" onClick={() => download(`${ENDPOINT}/export?format=json`)}>
            导出清单（JSON）
          </button>
          <button className="btn ghost" type="button" onClick={() => download(`${ENDPOINT}/template`)}>
            下载导入模板
          </button>
        </div>
      </header>

      <div className="stat-row">
        {statCards.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <section className="panel">
        <h3>订阅任务清单导入</h3>
        <p className="hint">
          CSV 至少包含「数据源名称」「推送频率」列；可选「任务编号」「过滤条件」「推送目标」「推送格式」「应用名称」。
          同一批次内同数据源名称 + 推送频率的行自动合并，显式任务编号优先，重复行不产生副本。
        </p>
        <div className="inline-form">
          <input ref={fileRef} type="file" accept=".csv,.json,text/csv,application/json" />
          <button className="btn primary" type="button" disabled={importing} onClick={() => void submitImport()}>
            {importing ? '导入中…' : '开始导入'}
          </button>
        </div>

        {result.batch_id && (
          <div className={`import-result ${result.ok ? 'is-ok' : 'is-fail'}`}>
            <div className="import-head">
              <strong>{result.ok ? '导入成功' : '导入失败，原清单保持不变'}</strong>
              <span>批次 {result.batch_id}</span>
            </div>
            <p>{result.message}</p>
            <div className="import-metrics">
              <span>读取 {result.total} 行</span>
              <span>新建 {result.created}</span>
              <span>合并更新 {result.updated}</span>
              <span>仅更新水位 {result.watermark_only}</span>
              <span>重复合并 {result.duplicates}</span>
              <span className="error-text">失败 {result.failed}</span>
            </div>
            {result.groups.length > 0 && (
              <table className="data-table compact">
                <thead>
                  <tr><th>子批次</th><th>合并键（数据源::频率）</th><th>行数</th><th>重复行</th><th>本次动作</th></tr>
                </thead>
                <tbody>
                  {result.groups.map((group) => (
                    <tr key={`${group.index}-${group.merge_key}`}>
                      <td>{group.index}</td>
                      <td>{group.merge_key}</td>
                      <td>{group.rows}</td>
                      <td>{group.duplicates}</td>
                      <td>{group.action}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            {result.errors.length > 0 && (
              <>
                <table className="data-table compact">
                  <thead>
                    <tr><th>行号</th><th>任务编号</th><th>数据源名称</th><th>推送频率</th><th>错误原因</th></tr>
                  </thead>
                  <tbody>
                    {result.errors.map((item, index) => (
                      <tr key={`${item.行号}-${index}`}>
                        <td>{item.行号}</td>
                        <td>{item.任务编号}</td>
                        <td>{item.数据源名称 || '—'}</td>
                        <td>{item.推送频率 || '—'}</td>
                        <td className="error-text">{item.错误原因}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                <button className="btn" type="button" onClick={() => download(result.error_list_url)}>
                  下载错误清单（CSV）
                </button>
              </>
            )}
          </div>
        )}
      </section>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            {field === '应用名称' ? (
              <select
                value={filters[field] ?? ''}
                onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
              >
                <option value="">全部应用</option>
                {applications.map((item) => (
                  <option key={String(item['应用名称'])} value={String(item['应用名称'])}>
                    {item['应用名称']}（{item['登记状态']}）
                  </option>
                ))}
              </select>
            ) : (
              <input
                value={filters[field] ?? ''}
                placeholder={`按${field}检索`}
                onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
              />
            )}
          </label>
        ))}
        <label className="filter-item">
          <span>任务状态</span>
          <select
            value={filters.status ?? ''}
            onChange={(event) => setFilters((prev) => ({ ...prev, status: event.target.value }))}
          >
            <option value="">全部状态</option>
            {statuses.map((status) => <option key={status} value={status}>{status}</option>)}
          </select>
        </label>
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            {extraColumns.map((column) => <th key={column}>{column}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)} className={row['可恢复'] === false ? 'row-muted' : undefined}>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              {extraColumns.map((column) => (
                <td key={column}>
                  {column === '可恢复' ? (row[column] === false ? '否（来源已注销）' : '是') : (row[column] ?? '—')}
                </td>
              ))}
              <td className="row-actions">
                <button className="link" type="button" onClick={() => void openDetail(row)}>最近推送记录</button>
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + extraColumns.length + 1}>
                暂无数据订阅推送数据，可先导入订阅任务清单
              </td>
            </tr>
          )}
        </tbody>
      </table>

      {selected && (
        <section className="panel">
          <div className="import-head">
            <strong>
              最近推送记录：{selected['任务编号']}（{selected['数据源名称']} / {selected['推送频率']}）
            </strong>
            <button className="link" type="button" onClick={() => { setSelected(null); setPushes([]) }}>
              收起
            </button>
          </div>
          <p className="hint">
            当前可恢复位置：{selected['可恢复位置'] ?? '—'}；旧批次记录标记为「已保留」，最新水位标记为「可恢复」。
          </p>
          <table className="data-table compact">
            <thead>
              <tr><th>记录编号</th><th>批次号</th><th>水位版本</th><th>可恢复位置</th><th>过滤条件</th><th>状态</th><th>创建时间</th></tr>
            </thead>
            <tbody>
              {pushes.map((record) => (
                <tr key={String(record['记录编号'])}>
                  <td>{record['记录编号']}</td>
                  <td>{record['批次号']}</td>
                  <td>{record['水位版本']}</td>
                  <td>{record['可恢复位置']}</td>
                  <td>{record['过滤条件'] || '—'}</td>
                  <td>{record['状态']}</td>
                  <td>{record['创建时间']}</td>
                </tr>
              ))}
              {pushes.length === 0 && (
                <tr><td className="empty-state" colSpan={7}>该任务还没有推送记录，导入或推送后生成首个水位</td></tr>
              )}
            </tbody>
          </table>
        </section>
      )}

      <section className="panel">
        <h3>最近导入批次</h3>
        <table className="data-table compact">
          <thead>
            <tr><th>批次号</th><th>文件名</th><th>状态</th><th>读取行</th><th>新建</th><th>更新</th><th>失败</th><th>时间</th><th>错误清单</th></tr>
          </thead>
          <tbody>
            {batches.map((batch) => (
              <tr key={batch.batch_id}>
                <td>{batch.batch_id}</td>
                <td>{batch.file_name || '—'}</td>
                <td className={batch.status === '失败' ? 'error-text' : undefined}>{batch.status}</td>
                <td>{batch.total}</td>
                <td>{batch.created}</td>
                <td>{batch.updated}</td>
                <td>{batch.failed}</td>
                <td>{batch.created_at}</td>
                <td>
                  {batch.failed > 0 ? (
                    <button className="link" type="button" onClick={() => download(`${ENDPOINT}/imports/${batch.batch_id}/errors`)}>
                      下载错误清单
                    </button>
                  ) : '—'}
                </td>
              </tr>
            ))}
            {batches.length === 0 && (
              <tr><td className="empty-state" colSpan={9}>还没有导入批次</td></tr>
            )}
          </tbody>
        </table>
      </section>

      <footer className="page-foot">
        <span>共 {total} 条数据订阅推送记录</span>
        {noticeMessage && <span className="ok-text">{noticeMessage}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
