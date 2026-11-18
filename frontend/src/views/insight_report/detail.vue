<template>
  <section class="page" data-module="insight_report_detail">
    <header class="page-head">
      <div>
        <h2>洞察报告详情 · {{ entry?.['报告编号'] ?? '—' }}</h2>
        <p class="page-desc">详情页与工作台共用同一份当前数据版本聚合结果，历史版本可回看。</p>
      </div>
      <div class="page-actions">
        <RouterLink class="btn" to="/insight_report">返回工作台</RouterLink>
      </div>
    </header>

    <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>
    <p v-if="!entry && !errorMessage" style="color:var(--muted)">加载中…</p>

    <template v-if="entry">
      <div class="stat-row">
        <article class="stat-card" data-testid="detail-status">
          <span class="stat-label">报告状态</span>
          <strong class="stat-value">{{ entry.status }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">当前数据版本</span>
          <strong class="stat-value">{{ entry['当前版本'] }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">结论版本状态</span>
          <strong class="stat-value">{{ entry['版本状态'] }}</strong>
        </article>
        <article class="stat-card" data-testid="detail-findings">
          <span class="stat-label">关键发现数量</span>
          <strong class="stat-value">{{ entry['关键发现数量'] }}</strong>
        </article>
      </div>

      <table class="data-table">
        <tbody>
          <tr v-for="field in baseFields" :key="field">
            <th style="width:160px">{{ field }}</th>
            <td>{{ entry[field] ?? '—' }}</td>
          </tr>
        </tbody>
      </table>

      <p v-if="entry['归档失败原因']" class="error-text" data-testid="archive-error">
        归档失败：{{ entry['归档失败原因'] }}
      </p>
      <p v-if="conflict" class="error-text" data-testid="metric-conflict">
        口径冲突（V008 拒绝共享同版本不同口径的结论）：
        {{ conflict['指标编号'] }} → {{ (conflict['指标口径'] || []).join(' / ') }}
      </p>
      <p v-if="entry['待归档重算版本']" data-testid="pending-recompute">
        待归档重算版本：{{ entry['待归档重算版本'] }}（归档时需选择先重算还是先归档）
      </p>

      <h3 style="margin:20px 0 8px">当前版本结论</h3>
      <table class="data-table" data-testid="conclusion-table">
        <thead><tr><th>结论编号</th><th>关键发现</th><th>指标编号</th><th>指标口径</th><th>状态</th></tr></thead>
        <tbody>
          <tr v-for="row in entry['结论'] || []" :key="String(row['结论编号'])">
            <td>{{ row['结论编号'] }}</td>
            <td>{{ row['关键发现'] }}</td>
            <td>{{ row['指标编号'] }}</td>
            <td>{{ row['指标口径'] }}</td>
            <td>{{ row.status }}</td>
          </tr>
          <tr v-if="!(entry['结论'] || []).length"><td colspan="5" class="empty-state">当前版本暂无结论</td></tr>
        </tbody>
      </table>

      <h3 style="margin:20px 0 8px">历史版本（去重聚合，不重复计数）</h3>
      <table class="data-table" data-testid="version-history">
        <thead><tr><th>报告版本</th><th>版本状态</th><th>关键发现数量</th><th>关键发现</th></tr></thead>
        <tbody>
          <tr v-for="item in entry['版本历史'] || []" :key="item['报告版本']">
            <td>{{ item['报告版本'] }}</td>
            <td>{{ item['版本状态'] }}</td>
            <td>{{ item['关键发现数量'] }}</td>
            <td>{{ item['关键发现'] || '—' }}</td>
          </tr>
          <tr v-if="!(entry['版本历史'] || []).length"><td colspan="4" class="empty-state">暂无历史版本</td></tr>
        </tbody>
      </table>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'

import { request } from '@/api/client'

type Row = Record<string, any>

const route = useRoute()
const entry = ref<Row | null>(null)
const errorMessage = ref('')
const baseFields = ['报告编号', '分析主题', '数据来源', '关键发现', '建议措施', '影响范围', '生成时间', '归档时间']
const conflict = computed(() => (entry.value?.['口径冲突'] || [])[0] || null)

async function load() {
  try {
    const response = await request(`/api/insight_report/${route.params.id}`)
    if (!response.ok) throw new Error('洞察报告详情读取失败')
    entry.value = await response.json()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '洞察报告详情读取失败'
  }
}

onMounted(load)
</script>
