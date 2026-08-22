// Toggle-like "Last X minutes" button. Only one button per `toggle_group`
// may be active at a time — DashboardView owns that exclusivity (this widget
// doesn't know about its siblings), and tells this instance whether it's the
// active one via the `activeGroupId` prop.
export default {
    name: 'ButtonDatetimeFilterWidget',
    props: {
        id:            { type: String, required: true },
        config:        { type: Object, required: true },
        data:          { default: null },
        theme:         { type: String, default: 'light' },
        activeGroupId: { default: null }
    },
    emits: ['update:filter'],
    setup(props, { emit }) {
        const toggle = () => {
            const isActive = props.activeGroupId === props.id;
            emit('update:filter', {
                widgetId: props.id,
                value: isActive ? null : props.config.filter,
                operator: isActive ? null : 'last_minutes',
                toggleGroup: props.config.toggle_group
            });
        };
        return { toggle };
    },
    template: `
        <div class="btn-widget-frame" :class="{ 'btn-widget-active': activeGroupId === id }" @click="toggle" role="button" tabindex="0">
            <i class="fa-solid" :class="activeGroupId === id ? 'fa-toggle-on' : 'fa-toggle-off'" style="font-size:1.1rem;"></i>
            <span>{{ config.label }}</span>
        </div>
    `
};
