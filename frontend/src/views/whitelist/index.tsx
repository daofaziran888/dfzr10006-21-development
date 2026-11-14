import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/whitelist'
const columns = [
  "登记编号",
  "应用名称",
  "调用标识",
  "日配额上限",
  "优先级",
  "生效日期",
  "失效日期",
  "登记状态"
]
const actions = [
  "启用登记",
  "冻结来源",
  "注销来源"
]
const statuses = [
  "草稿",
  "已登记",
  "已冻结",
  "已注销"
]

type Row = Record<string, string | number | boolean | null>
type Application = Record<string, string | number | string[]>

export default function WhitelistView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [noticeMessage, setNoticeMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [applications, setApplications] = useState<Application[]>([])
  const filterFields = useMemo(() => [columns[0], columns[1]], [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    // 页面筛选字段用中文列名，后端按 keyword/application/status 收口，这里显式映射。
    const query = new URLSearchParams()
    if (filters[columns[0]]) query.set('keyword', filters[columns[0]])
    if (filters[columns[1]]) query.set('application', filters[columns[1]])
    if (filters.status) query.set('status', filters.status)
    try {
      const [listResponse, appResponse] = await Promise.all([
        request(`${ENDPOINT}?${query}`),
        request(`${ENDPOINT}/applications`),
      ])
      if (!listResponse.ok) {
        throw new Error('来源登记列表读取失败')
      }
      const payload = await listResponse.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
      if (appResponse.ok) setApplications((await appResponse.json()).items ?? [])
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '调用来源管理列表读取失败')
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
        throw new Error(payload.message ?? '调用来源管理动作未生效，请稍后重试')
      }
      setNoticeMessage(payload.message ?? '')
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '调用来源管理操作失败')
    }
  }

  const selectedApplication = filters['应用名称'] ?? ''
  const linkedTasks = applications.find((item) => item['应用名称'] === selectedApplication)?.['订阅任务']

  return (
    <section className="page" data-module="whitelist">
      <header className="page-head">
        <div>
          <h2>调用来源管理管理</h2>
          <p className="page-desc">
            维护调用来源的标识登记、配额分配与有效期。来源登记的应用名称与订阅任务的应用名称是同一引用键，
            注销来源会同步停止该应用已有订阅任务的推送，并阻止继续新增推送。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('来源登记登记入口尚未接入审批流')}>
            登记来源登记
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出调用来源管理清单
          </button>
        </div>
      </header>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <label className="filter-item">
          <span>登记状态</span>
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
            <th>关联订阅任务</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td>{row['关联订阅任务数'] ? `${row['关联订阅任务数']} 个（${row['关联订阅任务']}）` : '—'}</td>
              <td className="row-actions">
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
              <td className="empty-state" colSpan={columns.length + 2}>暂无调用来源管理数据，可先登记来源登记</td>
            </tr>
          )}
        </tbody>
      </table>

      <section className="panel">
        <h3>订阅任务引用的应用名称</h3>
        <p className="hint">
          下列应用名称来自订阅任务清单；只有全部登记都已注销的应用才会阻止新增推送。选择应用可筛选来源登记。
        </p>
        <div className="filter-bar">
          {applications.map((item) => (
            <button
              key={String(item['应用名称'])}
              className="btn ghost"
              type="button"
              onClick={() => setFilters((prev) => ({ ...prev, 应用名称: String(item['应用名称']) }))}
            >
              {item['应用名称']}（{item['登记状态']}，订阅任务 {Array.isArray(item['订阅任务']) ? item['订阅任务'].length : 0}）
            </button>
          ))}
          {applications.length === 0 && <span className="hint">暂无应用名称引用</span>}
        </div>
        {selectedApplication && (
          <p className="hint">
            已筛选应用「{selectedApplication}」
            {Array.isArray(linkedTasks) && linkedTasks.length > 0 ? `，引用任务：${linkedTasks.join('、')}` : '，无订阅任务引用'}
          </p>
        )}
      </section>

      <footer className="page-foot">
        <span>共 {total} 条调用来源管理记录</span>
        {noticeMessage && <span className="ok-text">{noticeMessage}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
