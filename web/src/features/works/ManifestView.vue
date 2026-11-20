<script setup lang="ts">
import type { Manifest } from '../../api/types'
import { platformName } from '../../platforms/registry'
defineProps<{ manifest: Manifest }>()
</script>
<template>
  <section>
    <h3>{{ manifest.work.title || manifest.work.id }}</h3>
    <ul>
      <li v-for="unit in manifest.upload_units" :key="unit.id">
        <strong>{{ unit.id }}</strong> · {{ unit.mode === 'multipart' ? '合并为多 P 稿件' : '按视频分别投稿' }}
        <ol><li v-for="video in unit.videos" :key="video">
          {{ manifest.assets.videos.find(item => item.id === video)?.path ?? video }}
        </li></ol>
        <p v-for="target in unit.targets" :key="`${target.platform}:${target.account}:${target.enabled}`">
          {{ platformName(target.platform) }} · {{ target.account }} · {{ target.enabled ? '启用' : '关闭' }}
        </p>
      </li>
    </ul>
    <details><summary>查看完整投稿配置</summary><pre>{{ JSON.stringify(manifest, null, 2) }}</pre></details>
  </section>
</template>
