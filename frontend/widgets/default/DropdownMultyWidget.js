const { ref, computed, onMounted, onUnmounted } = Vue;

// `data` (optional) names an aggregate or raw source that populates the
// option list — arrives as row-dicts, e.g. [{env:'prod'}, {env:'staging'}],
// same shape every other aggregate-backed widget gets. `mapping` is a
// separate concern: the DB column the widget's selection filters on.
export default {
    name: 'DropdownMultyWidget',
    props: {
        id:     { type: String, required: true },
        config: { type: Object, required: true },
        data:   { default: null },
        theme:  { type: String, default: 'light' }
    },
    emits: ['update:filter'],
    setup(props, { emit }) {
        const rootRef = ref(null);
        const isOpen = ref(false);
        const searchQuery = ref('');
        const selected = ref([]);

        const options = computed(() => {
            const rows = Array.isArray(props.data) ? props.data : [];
            if (rows.length === 0) return [];
            const first = rows[0];
            if (first !== null && typeof first === 'object') {
                const key = Object.keys(first)[0];
                return rows.map(r => r[key]);
            }
            return rows;
        });
        const filteredOptions = computed(() => {
            const q = searchQuery.value.toLowerCase();
            return options.value.filter(o => !q || String(o).toLowerCase().includes(q));
        });

        const emitUpdate = () => {
            // `in` tells the backend to build a `column IN (...)` clause over
            // the whole selected array — `eq` would only ever compare against
            // a single scalar, which is not what a multi-select means.
            emit('update:filter', { widgetId: props.id, value: selected.value.length ? selected.value : null, operator: 'in' });
        };

        const clear = () => { selected.value = []; searchQuery.value = ''; emitUpdate(); };

        // Checkboxes only update local `selected` state while the panel is
        // open — the filter itself is applied once, on close, so picking
        // several values doesn't refetch (and re-render the option list) on
        // every single click in between.
        const toggleOpen = () => {
            const wasOpen = isOpen.value;
            isOpen.value = !isOpen.value;
            if (wasOpen) emitUpdate(); // just closed -> apply the selection
        };
        const onDocumentClick = (event) => {
            if (isOpen.value && rootRef.value && !rootRef.value.contains(event.target)) {
                isOpen.value = false;
                emitUpdate();
            }
        };

        onMounted(() => document.addEventListener('click', onDocumentClick));
        onUnmounted(() => document.removeEventListener('click', onDocumentClick));

        return { rootRef, isOpen, searchQuery, selected, filteredOptions, clear, toggleOpen };
    },
    template: `
        <div class="combo-widget" :class="{ 'combo-open': isOpen }" ref="rootRef">
            <button class="input-filter-clear combo-clear" title="Clear selection" @click="clear"><i class="fa-solid fa-eraser"></i></button>
            <div class="combo-body">
                <label class="combo-label">{{ config.label }}</label>
                <div class="combo-trigger-wrap">
                    <div class="combo-trigger" @click="toggleOpen">
                        <span>{{ selected.length === 0 ? 'All' : selected.length + ' selected' }}</span>
                        <i class="fa-solid" :class="isOpen ? 'fa-chevron-up' : 'fa-chevron-down'"></i>
                    </div>
                    <div class="combo-panel" v-show="isOpen">
                        <div class="combo-search-row">
                            <i class="fa-solid fa-magnifying-glass combo-search-icon"></i>
                            <input class="combo-search-input" type="text" v-model="searchQuery" placeholder="Search">
                        </div>
                        <div class="combo-options">
                            <label class="combo-option" v-for="opt in filteredOptions" :key="opt">
                                <input type="checkbox" class="combo-checkbox" :value="opt" v-model="selected"><span>{{ opt }}</span>
                            </label>
                            <div class="combo-no-results" v-if="filteredOptions.length === 0">No matches</div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    `
};
