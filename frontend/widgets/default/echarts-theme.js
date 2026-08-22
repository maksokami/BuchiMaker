// Registers the 'default' package's eCharts palettes.
// Colors are lifted from the old DashboardView.js initCharts() hardcoded
// hex values, so the visual identity is preserved across the refactor.
echarts.registerTheme('default-light', {
    color: ['#0052cc', '#00bcd4', '#6b3fa0', '#3d2fa0', '#1a1aaa', '#c5cae9'],
    backgroundColor: 'transparent'
});

echarts.registerTheme('default-dark', {
    color: ['#3b82f6', '#22d3ee', '#a78bfa', '#818cf8', '#6366f1', '#c5cae9'],
    backgroundColor: 'transparent'
});
