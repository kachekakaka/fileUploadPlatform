<script setup lang="ts">
defineProps<{ receipt: Record<string, unknown> }>()
</script>
<template>
  <h3>Bilibili 回执</h3>
  <pre>{{ JSON.stringify(receipt, null, 2) }}</pre>
</template>
