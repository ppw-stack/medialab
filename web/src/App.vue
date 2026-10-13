<script setup lang="ts">
import zhCN from 'ant-design-vue/es/locale/zh_CN'

const theme = {
  token: {
    colorPrimary: '#F25C54',
    colorText: '#1A1815',
    colorBgBase: '#FAF8F5',
    colorBgContainer: '#FFFFFF',
    colorBorder: '#E8E3DA',
    colorBorderSecondary: '#E8E3DA',
    colorTextSecondary: '#6B655A',
    colorTextTertiary: '#948C7E',
    colorSuccess: '#16A34A',
    colorWarning: '#D97706',
    colorError: '#DC2626',
    borderRadius: 8,
    fontFamily:
      '"Inter", "PingFang SC", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", sans-serif',
  },
}
</script>

<template>
  <a-config-provider :locale="zhCN" :theme="theme">
    <RouterView />
  </a-config-provider>
</template>
