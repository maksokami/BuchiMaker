const { ref, onErrorCaptured } = Vue;

export default {
    name: 'ErrorBoundary',
    setup() {
        const error = ref(null);
        const errorInfo = ref(null);

        onErrorCaptured((err, instance, info) => {
            error.value = err;
            errorInfo.value = info;
            console.error('Error captured by ErrorBoundary:', err, info);
            // Return false to prevent the error from propagating further up the component tree
            return false;
        });

        const resetError = () => {
            error.value = null;
            errorInfo.value = null;
        };

        return { error, errorInfo, resetError };
    },
    template: `
        <slot v-if="!error"></slot>
        <div v-else class="widget-error-fallback">
            <div class="error-icon"><i class="fa-solid fa-triangle-exclamation"></i></div>
            <div class="error-message">Widget Crashed</div>
            <div class="error-details" :title="error?.message">{{ error?.message || 'An unknown error occurred' }}</div>
            <button class="retry-btn" @click="resetError">Retry</button>
        </div>
    `
};
