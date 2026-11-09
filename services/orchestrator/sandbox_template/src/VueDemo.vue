<script setup>
import { ref } from 'vue'

const clicks = ref(0)

function handleClick() {
  clicks.value += 1
}
</script>

<template>
  <main class="vue-page">
    <section class="vue-card">
      <p class="vue-kicker">VUE 3 · INSPECT</p>
      <h1 class="vue-title">点击按钮，回传 SFC 行号</h1>
      <p class="vue-copy">这个页面由 Vue SFC 编译，插件会把 .vue 文件路径与行号写入 DOM。</p>
      <button class="vue-cta" type="button" @click="handleClick">
        Vue 精准选择 {{ clicks }}
      </button>
    </section>
  </main>
</template>

<style scoped>
.vue-page {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
  box-sizing: border-box;
}
.vue-card {
  width: min(720px, 100%);
  padding: 28px;
  border-radius: 22px;
  background: rgba(15, 23, 42, 0.82);
  border: 1px solid rgba(148, 163, 184, 0.22);
  box-shadow: 0 24px 64px rgba(0, 0, 0, 0.45);
  box-sizing: border-box;
}
.vue-kicker {
  margin: 0;
  letter-spacing: 0.3em;
  text-transform: uppercase;
  font-size: 11px;
  color: #38bdf8;
}
.vue-title {
  font-size: 32px;
  line-height: 1.16;
  margin: 14px 0 12px;
}
.vue-copy {
  margin: 0;
  color: #94a3b8;
  font-size: 13.5px;
  line-height: 1.7;
}
.vue-cta {
  margin-top: 22px;
  padding: 12px 24px;
  border: 0;
  border-radius: 999px;
  background: #2563eb;
  color: #fff;
  font-size: 14px;
  font-weight: 600;
  cursor: pointer;
}
.vue-cta:hover {
  background: #1d4ed8;
}
</style>
