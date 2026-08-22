import SettingsPage from './components/Settings.js';
import DashboardView from './components/DashboardView.js';
import LogsView from './components/LogsView.js';
import AccessDenied from './components/AccessDenied.js';
import ErrorBoundary from './components/ErrorBoundary.js';
import { authState, fetchAuthState, redirectToLogin, logout, installUnauthorizedRedirect, isAdministrator, isDataAdminOrAbove } from './services/auth.js';

const { createApp, ref, onMounted } = Vue;

const app = createApp({
    setup() {
        const currentPage        = ref('dashboard');
        const apiBaseUrl         = window.API_BASE_URL || 'http://localhost:8000/api/v1';
        const isSidebarCollapsed = ref(false);
        const dashboards         = ref([]);
        const activeDashboardId  = ref('default');

        const toggleSidebar = () => { isSidebarCollapsed.value = !isSidebarCollapsed.value; };

        // Dashboard ids come straight from the loaded YAML `id` field, so any
        // freshly-loaded dashboard is reachable at /dashboard/<id> immediately —
        // there's no separate route table to update.
        const dashboardIdFromPath = () => {
            const match = window.location.pathname.match(/^\/dashboard\/([^/]+)\/?$/);
            return match ? decodeURIComponent(match[1]) : null;
        };

        const navigateToDashboard = (id, { replace = false } = {}) => {
            activeDashboardId.value = id;
            currentPage.value = 'dashboard';
            const url = `/dashboard/${encodeURIComponent(id)}${window.location.search}`;
            if (replace) history.replaceState({ dashboardId: id }, '', url);
            else history.pushState({ dashboardId: id }, '', url);
        };

        const fetchDashboards = async () => {
            try {
                const res = await fetch(`${apiBaseUrl}/dashboards`);
                if (res.ok) {
                    const data = await res.json();
                    if (Array.isArray(data) && data.length > 0) {
                        dashboards.value = data;
                        const fromPath = dashboardIdFromPath();
                        const initial = (fromPath && data.some(d => d.id === fromPath)) ? fromPath : data[0].id;
                        activeDashboardId.value = initial;
                        history.replaceState({ dashboardId: initial }, '', `/dashboard/${encodeURIComponent(initial)}${window.location.search}`);
                        return;
                    }
                }
            } catch (e) {
                console.warn('Could not fetch dashboards from API, using fallback.', e);
            }
            // Fallback
            dashboards.value = [{ id: 'default', title: 'Dashboard' }];
            activeDashboardId.value = 'default';
        };

        onMounted(async () => {
            await fetchAuthState();

            if (!authState.isAuthenticated) {
                // Not anonymous and no (valid) session — leave the origin
                // entirely to sign in. A real navigation, not fetch, since
                // this is going to the identity provider.
                redirectToLogin();
                return;
            }

            if (authState.role === 'Deny') {
                currentPage.value = 'denied';
            } else {
                await fetchDashboards();
                window.addEventListener('popstate', () => {
                    const fromPath = dashboardIdFromPath();
                    if (fromPath && dashboards.value.some(d => d.id === fromPath)) {
                        activeDashboardId.value = fromPath;
                        currentPage.value = 'dashboard';
                    }
                });
            }

            // Installed only after the initial /auth/me round trip above is
            // resolved, so a session that expires mid-use (any later fetch
            // anywhere in the app returning 401) bounces back to login too.
            installUnauthorizedRedirect();
        });

        return {
            isSidebarCollapsed, toggleSidebar,
            currentPage,
            dashboards, activeDashboardId, navigateToDashboard,
            apiBaseUrl,
            fetchDashboards,
            authState, logout, isAdministrator, isDataAdminOrAbove,
        };
    }
});

app.component('settings-page', SettingsPage);
app.component('dashboard-view', DashboardView);
app.component('logs-view', LogsView);
app.component('access-denied', AccessDenied);
app.component('error-boundary', ErrorBoundary);
app.mount('#app');
