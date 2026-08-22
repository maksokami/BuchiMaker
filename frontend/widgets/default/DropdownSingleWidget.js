const { ref, computed, onMounted, onUnmounted } = Vue;

// See DropdownMultyWidget.js for the note on the `data` (option-list) /
// `mapping` (filter column) split — same applies here.
export default {
    name: 'DropdownSingleWidget',
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
        const selected = ref('');

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

        const select = (opt) => {
            selected.value = opt;
            isOpen.value = false;
            searchQuery.value = '';
            emit('update:filter', { widgetId: props.id, value: opt || null, operator: 'eq' });
        };
        const clear = () => select('');

        const onDocumentClick = (event) => {
            if (isOpen.value && rootRef.value && !rootRef.value.contains(event.target)) {
                isOpen.value = false;
                searchQuery.value = '';
            }
        };

        onMounted(() => document.addEventListener('click', onDocumentClick));
        onUnmounted(() => document.removeEventListener('click', onDocumentClick));

        return { rootRef, isOpen, searchQuery, selected, filteredOptions, select, clear };
    },
    template: `
        <div class="combo-widget" :class="{ 'combo-open': isOpen }" ref="rootRef">
            <button class="input-filter-clear combo-clear" title="Clear selection" @click="clear"><i class="fa-solid fa-eraser"></i></button>
            <div class="combo-body">
                <label class="combo-label">{{ config.label }}</label>
                <div class="combo-trigger-wrap">
                    <div class="combo-trigger" @click="isOpen = !isOpen">
                        <span>{{ selected ? selected : 'Select...' }}</span>
                        <i class="fa-solid" :class="isOpen ? 'fa-chevron-up' : 'fa-chevron-down'"></i>
                    </div>
                    <div class="combo-panel" v-show="isOpen">
                        <div class="combo-search-row">
                            <i class="fa-solid fa-magnifying-glass combo-search-icon"></i>
                            <input class="combo-search-input" type="text" v-model="searchQuery" placeholder="Search">
                        </div>
                        <div class="combo-options">
                            <label class="combo-option" v-for="opt in filteredOptions" :key="opt">
                                <input type="radio" name="single-dropdown-radio" class="combo-checkbox" :value="opt" :checked="opt === selected" @change="select(opt)"><span>{{ opt }}</span>
                            </label>
                            <div class="combo-no-results" v-if="filteredOptions.length === 0">No matches</div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    `
};
