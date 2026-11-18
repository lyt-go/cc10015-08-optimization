<template>
  <section class="page" data-module="insight_report">
    <header class="page-head">
      <div>
        <h2>洞察报告</h2>
        <p class="page-desc">
          按报告版本聚合洞察结论：一个报告只展示一条当前数据版本，历史版本可回看，重复生成不会重复累计关键发现。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出报告清单</button>
      </div>
    </header>

    <div class="analysis-summary">
      <article v-for="m in summaryMetrics" :key="m.label" class="analysis-card" data-testid="kpi-card">
        <span style="font-size:12px;color:var(--muted)">{{ m.label }}</span>
        <strong style="font-size:22px;display:block">{{ m.value }}</strong>
        <span :class="m.trend > 0 ? 'trend-up' : 'trend-down'" style="font-size:12px">{{ m.trend > 0 ? '↑' : '↓' }}{{ Math.abs(m.trend) }}%</span>
      </article>
    </div>

    <form class="analysis-form" @submit.prevent="runAnalysis">
      <label v-for="field in paramFields" :key="field"><span>{{ field }}</span>
        <input v-model="params[field]" :placeholder="`请输入${field}`" />
      </label>
      <button class="btn primary" type="submit" data-testid="run-analysis">运行分析</button>
    </form>
    <div v-if="chartData.length" class="analysis-chart">
      <div v-for="(bar, i) in chartData" :key="i" class="analysis-bar" :style="{ height: barHeight(bar.value) + 'px' }"><span>{{ bar.label }}</span></div>
    </div>
    <table class="data-table" v-if="results.length">
      <thead><tr><th v-for="c in resultColumns" :key="c">{{ c }}</th></tr></thead>
      <tbody><tr v-for="row in results" :key="row.id"><td v-for="c in resultColumns" :key="c">{{ row[c] ?? '—' }}</td></tr></tbody>
    </table>
    <p v-if="!results.length && !loading" style="text-align:center;color:var(--muted)">请配置参数后运行分析</p>

    <h3 style="margin:20px 0 8px">当前报告数据版本</h3>
    <p class="page-desc" style="margin-bottom:8px">
      每个报告只保留一条当前数据版本；关键发现数量按结论版本去重后统计，历史版本见详情页。
    </p>
    <table class="data-table" data-testid="report-table">
      <thead>
        <tr>
          <th>报告编号</th><th>分析主题</th><th>当前版本</th><th>报告状态</th>
          <th>版本状态</th><th>关键发现数量</th><th>关键发现</th><th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="row in reports"
          :key="String(row.id)"
          :data-testid="`report-${row.id}`"
          :data-status="String(row.status)"
        >
          <td>{{ row['报告编号'] }}</td>
          <td>{{ row['分析主题'] }}</td>
          <td>{{ row['当前版本'] }}</td>
          <td>{{ row.status }}</td>
          <td>{{ row['版本状态'] }}</td>
          <td>{{ row['关键发现数量'] }}</td>
          <td>{{ row['关键发现'] || '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >{{ action }}</button>
            <RouterLink class="link" :to="`/insight_report/${row.id}`">查看详情</RouterLink>
          </td>
        </tr>
        <tr v-if="!reports.length">
          <td colspan="8" class="empty-state">暂无洞察报告数据</td>
        </tr>
      </tbody>
    </table>

    <div v-if="selected" class="modal-backdrop" data-testid="conflict-modal">
      <form class="modal" @submit.prevent>
        <h3>处理归档与重算并发</h3>
        <p class="page-desc">报告 {{ selected['报告编号'] }} 存在待归档重算版本 {{ pendingVersion }}，请选择先后顺序。</p>
        <label class="create-field">
          <span>冲突处理方式</span>
          <select v-model="resolution" data-testid="resolution-select">
            <option value="recompute_then_archive">先完成重算再归档（归档含新结论）</option>
            <option value="archive_then_recompute">先归档旧结论再发布重算版本（重算列修订）</option>
          </select>
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="selected = null">取消</button>
          <button class="btn primary" type="button" data-testid="confirm-archive" @click="confirmArchive">确认归档</button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ reports.length }} 条当前报告数据版本</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, any>

const ENDPOINT = '/api/insight_report'
const actions = ['开始生成', '确认完成', '归档报告']
const summaryMetrics = ref<{ label: string; value: number; trend: number }[]>([])
const reports = ref<Row[]>([])
const results = ref<Row[]>([])
const chartData = ref<{ label: string; value: number }[]>([])
const loading = ref(false)
const errorMessage = ref('')
const params = ref<Record<string, string>>({})
const paramFields = ['样本范围']
const resultColumns = ['分组', '指标值', '对比基准', '差异']

const selected = ref<Row | null>(null)
const pendingVersion = ref('')
const resolution = ref('recompute_then_archive')

function barHeight(value: number) {
  const max = Math.max(...chartData.value.map((item) => item.value), 1)
  return Math.max(10, (value / max) * 140)
}

async function fetchSummary() {
  try {
    const response = await request(`${ENDPOINT}/summary`)
    summaryMetrics.value = (await response.json()).metrics || []
  } catch {
    summaryMetrics.value = []
  }
}

async function reload() {
  try {
    const response = await request(ENDPOINT)
    if (!response.ok) throw new Error('洞察报告列表读取失败')
    const payload = await response.json()
    reports.value = payload.items ?? []
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '洞察报告列表读取失败'
  }
}

async function runAnalysis() {
  loading.value = true
  try {
    const response = await request(`${ENDPOINT}/run`, {
      method: 'POST',
      body: JSON.stringify({ values: params.value }),
    })
    const data = await response.json()
    results.value = data.items || []
    chartData.value = (data.items || []).map((row: Row) => ({
      label: String(row['分组'] ?? ''),
      value: Number(row['指标值'] ?? 0),
    })).slice(0, 6)
  } catch {
    results.value = []
    chartData.value = []
  }
  loading.value = false
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  if (action === '归档报告') {
    const detail = await loadDetail(row.id)
    if (detail?.待归档重算版本) {
      // 归档请求与重算回执同时到达：必须先显式选择先后顺序。
      selected.value = row
      pendingVersion.value = detail.待归档重算版本
      return
    }
  }
  await submitAction(action, row, undefined)
}

async function confirmArchive() {
  if (!selected.value) return
  await submitAction('归档报告', selected.value, resolution.value)
  selected.value = null
}

async function submitAction(action: string, row: Row, mode: string | undefined) {
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action, resolution: mode, values: params.value }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || `${action}未生效`)
    await reload()
    await fetchSummary()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : `${action}失败`
    await reload()
  }
}

async function loadDetail(entryId: number): Promise<Row | null> {
  try {
    const response = await request(`${ENDPOINT}/${entryId}`)
    if (!response.ok) return null
    return await response.json()
  } catch {
    return null
  }
}

async function exportRows() {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/export`)
    if (!response.ok) throw new Error('导出失败，请稍后重试')
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = 'insight_report.csv'
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

onMounted(async () => {
  await Promise.all([fetchSummary(), reload()])
})
</script>
