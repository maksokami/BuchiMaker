export default {
    name: 'ButtonUrlWidget',
    props: {
        id:     { type: String, required: true },
        config: { type: Object, required: true },
        data:   { default: null },
        theme:  { type: String, default: 'light' }
    },
    // No update:filter emit — button_url is a plain link, it never
    // contributes a filter condition (backend excludes it too, dashboard.py).
    template: `
        <a :href="config.url" target="_blank" class="btn-widget-frame btn-widget-url">
            <span>{{ config.label }}</span>
            <i class="fa-solid fa-arrow-up-right-from-square" style="font-size:0.85rem;"></i>
        </a>
    `
};
