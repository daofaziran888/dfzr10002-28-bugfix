import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/cache'
const columns = [
  '规则编号',
  '缓存目标',
  '缓存时间',
  '失效条件',
  '淘汰策略',
  '是否启用',
]
const statuses = ['草稿', '待校验', '已启用', '已停用'] as const
type RuleStatus = (typeof statuses)[number]

// 状态机：每次只允许流转一步，已停用是终态（恢复需重新登记走生命周期）。
const NEXT_ACTION: Record<RuleStatus, string | null> = {
  草稿: '提交校验',
  待校验: '启用配置',
  已启用: '停用配置',
  已停用: null,
}

type Row = Record<string, string | number | null> & {
  id: number
  status: RuleStatus
  缓存状态?: string
  目标缓存状态?: string
  快照数量?: number
  失效作业?: JobRecord | null
}

type JobRecord = {
  jobId: number
  target: string
  ruleCode: string
  status: '待失效' | '已失效' | '失效失败'
  attempts: number
  lastError: string | null
  createdAt: string
  updatedAt: string
  clearedSnapshots?: number
}

type HitRecord = {
  hitId: number
  target: string
  callerModule: string
  callerId: number | null
  result: string
  ruleCode: string | null
  snapshotId: number | null
  time: string
}

type SnapshotRecord = {
  snapshotId: number
  target: string
  ruleCode: string
  payload: string
  createdAt: string
}

type RuleDetail = Row & {
  runtime?: {
    cacheStatus: string
    snapshotCount: number
    snapshots: SnapshotRecord[]
    jobs: JobRecord[]
    ruleCount: number
    activeRuleCount: number
  }
  hits: HitRecord[]
}

function badgeClass(text?: string | null): string {
  if (!text) return 'badge badge-muted'
  if (text.includes('失败')) return 'badge badge-danger'
  if (text.includes('待失效') || text.includes('待生效') || text.includes('待重试') || text.includes('待校验')) return 'badge badge-warn'
  if (text.includes('正常') || text.includes('已失效')) return 'badge badge-info'
  if (text.includes('停用') || text.includes('未启用')) return 'badge badge-muted'
  return 'badge badge-muted'
}

const callerLabel: Record<string, string> = { query: '查询模板', service: '数据服务' }

export default function CacheView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [successMessage, setSuccessMessage] = useState('')
  const [targetFilter, setTargetFilter] = useState('')
  const [keywordFilter, setKeywordFilter] = useState('')
  const [statusFilter, setStatusFilter] = useState<RuleStatus | ''>('')
  const [detail, setDetail] = useState<RuleDetail | null>(null)

  const reload = useCallback(async () => {
    setErrorMessage('')
    const params = new URLSearchParams()
    if (targetFilter.trim()) params.set('target', targetFilter.trim())
    if (keywordFilter.trim()) params.set('keyword', keywordFilter.trim())
    if (statusFilter) params.set('status', statusFilter)
    try {
      const response = await request(`${ENDPOINT}?${params.toString()}`)
      if (!response.ok) throw new Error('缓存规则列表读取失败')
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '查询缓存管理列表读取失败')
    }
  }, [targetFilter, keywordFilter, statusFilter])

  useEffect(() => {
    void reload()
  }, [reload])

  const loadDetail = useCallback(async (id: number) => {
    try {
      const response = await request(`${ENDPOINT}/${id}`)
      if (!response.ok) throw new Error('缓存规则详情读取失败')
      setDetail(await response.json())
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '缓存规则详情读取失败')
    }
  }, [])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setSuccessMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values: { action } }),
      })
      const payload = await response.json()
      if (!response.ok || !payload.ok) throw new Error(payload.message ?? '查询缓存管理动作未生效')
      setSuccessMessage(payload.message)
      await reload()
      if (detail && detail.id === row.id) await loadDetail(row.id)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '查询缓存管理操作失败')
    }
  }

  async function retryJob(jobId: number) {
    setErrorMessage('')
    setSuccessMessage('')
    try {
      const response = await request(`${ENDPOINT}/jobs/${jobId}/retry`, { method: 'POST' })
      const payload = await response.json()
      if (!response.ok || !payload.ok) throw new Error(payload.message ?? '重试失败')
      setSuccessMessage(payload.message)
      await reload()
      if (detail) await loadDetail(detail.id)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '失效重试失败')
    }
  }

  async function simulateHit(target: string, callerModule: string) {
    setErrorMessage('')
    setSuccessMessage('')
    try {
      const response = await request(`${ENDPOINT}/runtime/simulate-hit`, {
        method: 'POST',
        body: JSON.stringify({ values: { target, callerModule } }),
      })
      const payload = await response.json()
      if (!response.ok || !payload.ok) throw new Error(payload.message ?? '取数模拟失败')
      setSuccessMessage(`${callerLabel[callerModule]}取数：${payload.entry.result}`)
      if (detail) await loadDetail(detail.id)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '取数模拟失败')
    }
  }

  const stats = useMemo(() => {
    const enabled = rows.filter((row) => row.status === '已启用').length
    const retrying = rows.filter((row) => row.失效作业?.status === '失效失败').length
    return [
      { label: '缓存规则总数', value: total },
      { label: '已启用缓存规则', value: enabled },
      { label: '失效失败待重试', value: retrying },
    ]
  }, [rows, total])

  return (
    <section className="page" data-module="cache">
      <header className="page-head">
        <div>
          <h2>查询缓存管理</h2>
          <p className="page-desc">维护分析查询的缓存策略、停用失效与下游同步：停用后清除旧快照，查询模板与数据服务不再命中旧数据。</p>
        </div>
        <div className="page-actions">
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出查询缓存管理清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form
        className="filter-bar"
        onSubmit={(event) => {
          event.preventDefault()
          void reload()
        }}
      >
        <label className="filter-item">
          <span>缓存目标</span>
          <input
            value={targetFilter}
            placeholder="按缓存目标检索，如：查询模板-地区月报"
            onChange={(event) => setTargetFilter(event.target.value)}
          />
        </label>
        <label className="filter-item">
          <span>规则编号</span>
          <input
            value={keywordFilter}
            placeholder="按规则编号检索"
            onChange={(event) => setKeywordFilter(event.target.value)}
          />
        </label>
        <button className="btn primary" type="submit">查询</button>
        <button
          className="btn ghost"
          type="button"
          onClick={() => {
            setTargetFilter('')
            setKeywordFilter('')
            setStatusFilter('')
          }}
        >
          重置条件
        </button>
      </form>

      <div className="status-filter" role="group" aria-label="按规则状态筛选">
        <button type="button" className={statusFilter === '' ? 'active' : ''} onClick={() => setStatusFilter('')}>全部</button>
        {statuses.map((status) => (
          <button
            key={status}
            type="button"
            className={statusFilter === status ? 'active' : ''}
            onClick={() => setStatusFilter(status)}
          >
            {status}
          </button>
        ))}
      </div>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>规则状态</th>
            <th>缓存状态（本规则）</th>
            <th>目标缓存状态（下游同步）</th>
            <th>快照</th>
            <th>失效作业</th>
            <th>可执行动作</th>
            <th>详情</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const allowedAction = NEXT_ACTION[row.status]
            const job = row.失效作业
            return (
              <tr key={String(row.id)}>
                {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
                <td><span className="badge badge-info">{row.status}</span></td>
                <td><span className={badgeClass(row.缓存状态)}>{row.缓存状态 ?? '—'}</span></td>
                <td><span className={badgeClass(row.目标缓存状态)}>{row.目标缓存状态 ?? '—'}</span></td>
                <td>{row.快照数量 ?? 0}</td>
                <td>
                  {job ? (
                    <span className={badgeClass(job.status)}>
                      {job.status}（第{job.attempts}次）
                    </span>
                  ) : (
                    <span className="badge badge-muted">无</span>
                  )}
                </td>
                <td className="row-actions">
                  {allowedAction ? (
                    <button className="link" type="button" onClick={() => void runAction(allowedAction, row)}>
                      {allowedAction}
                    </button>
                  ) : (
                    <span className="badge badge-muted">终态，需重新登记</span>
                  )}
                  {job?.status === '失效失败' && (
                    <button className="link" type="button" onClick={() => void retryJob(job.jobId)}>
                      重试失效
                    </button>
                  )}
                </td>
                <td>
                  <button className="link" type="button" onClick={() => void loadDetail(row.id)}>
                    查看详情
                  </button>
                </td>
              </tr>
            )
          })}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 7}>未检索到缓存规则，请调整缓存目标或规则编号条件</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条查询缓存管理记录</span>
        {successMessage && <span className="success-text">{successMessage}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>

      {detail && (
        <CacheDetailDrawer
          detail={detail}
          onClose={() => setDetail(null)}
          onRetry={(jobId) => void retryJob(jobId)}
          onHit={(callerModule) => void simulateHit(String(detail.缓存目标), callerModule)}
        />
      )}
    </section>
  )
}

function CacheDetailDrawer({
  detail,
  onClose,
  onRetry,
  onHit,
}: {
  detail: RuleDetail
  onClose: () => void
  onRetry: (jobId: number) => void
  onHit: (callerModule: string) => void
}) {
  const jobs = detail.runtime?.jobs ?? []
  const snapshots = detail.runtime?.snapshots ?? []
  const latestJob = jobs.length ? jobs[jobs.length - 1] : null
  return (
    <>
      <div className="drawer-mask" onClick={onClose} />
      <aside className="drawer" role="dialog" aria-label="缓存规则详情">
        <div className="drawer-head">
          <h3>缓存规则详情 · {String(detail.规则编号)}</h3>
          <button className="btn ghost small" type="button" onClick={onClose}>关闭</button>
        </div>
        <div className="drawer-body">
          <section className="drawer-section">
            <h4>规则信息（与列表同一接口口径）</h4>
            <dl className="kv-grid">
              <dt>规则编号</dt><dd>{String(detail.规则编号)}</dd>
              <dt>缓存目标</dt><dd>{String(detail.缓存目标)}</dd>
              <dt>规则状态</dt><dd><span className="badge badge-info">{detail.status}</span></dd>
              <dt>本规则缓存状态</dt><dd><span className={badgeClass(detail.缓存状态)}>{detail.缓存状态 ?? '—'}</span></dd>
              <dt>目标缓存状态</dt><dd><span className={badgeClass(detail.目标缓存状态)}>{detail.目标缓存状态 ?? '—'}</span></dd>
              <dt>同目标规则数</dt><dd>{detail.runtime?.ruleCount ?? 1}（启用 {detail.runtime?.activeRuleCount ?? 0}）</dd>
            </dl>
          </section>

          <section className="drawer-section">
            <h4>取数链路验证（停用结论优先）</h4>
            <div className="drawer-actions">
              <button className="btn small" type="button" onClick={() => onHit('query')}>模拟查询模板取数</button>
              <button className="btn small" type="button" onClick={() => onHit('service')}>模拟数据服务取数</button>
              <span className="page-desc">停用后只返回未命中，不会再读到旧快照</span>
            </div>
          </section>

          <section className="drawer-section">
            <h4>存活快照（{snapshots.length}）</h4>
            {snapshots.length ? (
              <table className="mini-table">
                <thead><tr><th>快照ID</th><th>来源规则</th><th>内容</th><th>生成时间</th></tr></thead>
                <tbody>
                  {snapshots.map((snapshot) => (
                    <tr key={snapshot.snapshotId}>
                      <td>{snapshot.snapshotId}</td>
                      <td>{snapshot.ruleCode}</td>
                      <td>{snapshot.payload}</td>
                      <td>{snapshot.createdAt}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <p className="page-desc">该缓存目标当前无存活快照（停用失效后旧快照已清除）。</p>
            )}
          </section>

          <section className="drawer-section">
            <h4>失效作业</h4>
            {jobs.length ? (
              <table className="mini-table">
                <thead><tr><th>作业ID</th><th>状态</th><th>尝试次数</th><th>更新时间</th><th>说明 / 操作</th></tr></thead>
                <tbody>
                  {jobs.map((job) => (
                    <tr key={job.jobId}>
                      <td>{job.jobId}</td>
                      <td><span className={badgeClass(job.status)}>{job.status}</span></td>
                      <td>{job.attempts}</td>
                      <td>{job.updatedAt}</td>
                      <td>
                        {job.status === '失效失败' ? (
                          <>
                            <span className="error-text">{job.lastError}</span>
                            <button className="btn small" type="button" onClick={() => onRetry(job.jobId)}>重试</button>
                          </>
                        ) : job.status === '已失效' ? (
                          `已清除快照 ${job.clearedSnapshots ?? 0} 份`
                        ) : (
                          '等待失效执行'
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <p className="page-desc">暂无失效作业。</p>
            )}
            {latestJob?.status === '失效失败' && (
              <p className="error-text">失效失败：下游已同步为「失效失败-待重试」，可点击重试；历史命中记录不受影响。</p>
            )}
          </section>

          <section className="drawer-section">
            <h4>历史命中记录（{detail.hits.length}，只追加不删除）</h4>
            {detail.hits.length ? (
              <table className="mini-table">
                <thead><tr><th>时间</th><th>调用方</th><th>结果</th><th>规则编号</th><th>快照ID</th></tr></thead>
                <tbody>
                  {[...detail.hits].reverse().map((hit) => (
                    <tr key={hit.hitId}>
                      <td>{hit.time}</td>
                      <td>{callerLabel[hit.callerModule] ?? hit.callerModule}</td>
                      <td><span className={badgeClass(hit.result.includes('未命中') ? '已停用' : '正常')}>{hit.result}</span></td>
                      <td>{hit.ruleCode ?? '—'}</td>
                      <td>{hit.snapshotId ?? '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <p className="page-desc">暂无命中记录。</p>
            )}
          </section>
        </div>
      </aside>
    </>
  )
}
