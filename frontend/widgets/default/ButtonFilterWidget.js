const { ref, computed } = Vue;

// YAML declares `filter:` as a list of single-key dicts, e.g.
// [{operator:"ge"}, {value:"10"}] — merge into one plain object. Unlike
// input_filter, both operator AND value are fixed here (the button just
// toggles whether that fixed condition is applied).
function normalizeFilterDecl(list) {
    const out = {};
    (list || []).forEach(entry => Object.assign(out, entry));
    return out;
}

// A plain button_filter (no `toggle_group` in its YAML config) is a
// standalone on/off toggle, tracked locally. Once `toggle_group` is set it
// behaves like ButtonDatetimeFilterWidget: only one button per toggle_group
// may be active at a time, and DashboardView — not this widget — owns that
// exclusivity, telling each instance whether it's the active one via the
// `activeGroupId` prop.
export default {
    name: 'ButtonFilterWidget',
    props: {
        id:            { type: String, required: true },
        config:        { type: Object, required: true },
        data:          { default: null },
        theme:         { type: String, default: 'light' },
        activeGroupId: { default: null }
    },
    emits: ['update:filter'],
    setup(props, { emit }) {
        const decl = normalizeFilterDecl(props.config.filter);
        // For the grouped path, initial visual state comes from `activeGroupId`
        // (DashboardView seeds `toggleGroups` from `default_active` before the
        // first render — see seedDefaultFilters in DashboardView.js). For the
        // standalone path there's no parent-owned state to read, so mirror the
        // same `default_active` flag locally.
        const standaloneActive = ref(!!props.config.default_active);

        const grouped = computed(() => !!props.config.toggle_group);
        const active = computed(() => grouped.value
            ? props.activeGroupId === props.id
            : standaloneActive.value);

        const toggle = () => {
            const isActive = active.value;
            if (grouped.value) {
                emit('update:filter', {
                    widgetId: props.id,
                    value: isActive ? null : decl.value,
                    operator: isActive ? null : (decl.operator || 'eq'),
                    toggleGroup: props.config.toggle_group
                });
            } else {
                standaloneActive.value = !isActive;
                emit('update:filter', {
                    widgetId: props.id,
                    value: standaloneActive.value ? decl.value : null,
                    operator: decl.operator || 'eq'
                });
            }
        };

        return { active, toggle };
    },
    template: `
        <div class="btn-widget-frame" :class="{ 'btn-widget-active': active }" @click="toggle" role="button" tabindex="0">
            <span>{{ config.label }}</span>
        </div>
    `
};
