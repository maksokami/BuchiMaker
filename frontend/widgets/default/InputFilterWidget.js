const { ref, watch } = Vue;

// YAML declares `filter:` as a list of single-key dicts, e.g.
// [{operator:"ge"}, {value:"10"}] — merge into one plain object.
// `value` is only an example placeholder in the YAML; the live filter
// value always comes from what the user types.
function normalizeFilterDecl(list) {
    const out = {};
    (list || []).forEach(entry => Object.assign(out, entry));
    return out;
}

// Per-instance debounce factory — a module-level timer would be shared
// (and clobbered) across every InputFilterWidget instance on the dashboard.
function debounce(fn, ms) {
    let timer = null;
    return (...args) => {
        clearTimeout(timer);
        timer = setTimeout(() => fn(...args), ms);
    };
}

export default {
    name: 'InputFilterWidget',
    props: {
        id:     { type: String, required: true },
        config: { type: Object, required: true },
        data:   { default: null },
        theme:  { type: String, default: 'light' }
    },
    emits: ['update:filter'],
    setup(props, { emit }) {
        const text = ref('');
        const operator = normalizeFilterDecl(props.config.filter).operator || 'eq';

        const emitUpdate = debounce(() => {
            emit('update:filter', { widgetId: props.id, value: text.value || null, operator });
        }, 300);

        watch(text, emitUpdate);

        const clear = () => { text.value = ''; };

        return { text, clear };
    },
    template: `
        <div class="input-filter-widget">
            <button class="input-filter-clear" title="Clear filter" @click="clear"><i class="fa-solid fa-eraser"></i></button>
            <div class="input-filter-body">
                <label class="input-filter-label">{{ config.label }}</label>
                <input class="input-filter-field" type="text" v-model="text" placeholder="Type to filter…">
            </div>
        </div>
    `
};
