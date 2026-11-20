<script setup lang="ts">
import { state } from './display'
defineProps<{ value: string }>()
</script>
<template><span class="status" :data-state="value">{{ state(value) }}</span></template>
