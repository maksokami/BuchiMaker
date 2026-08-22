// Single source of truth for "who is the current caller and what can they
// see" — backed by GET /auth/me (backend/app/api/auth.py). The backend
// performs the OIDC exchange itself (BFF pattern); this module never
// touches a token, only the resolved identity/role.
//
// Imported by app.js (route guarding + sidebar), Settings.js (tab
// visibility), and index.html's inline sidebar bindings.

const { reactive } = Vue;

const AUTH_BASE_URL = window.AUTH_BASE_URL || '/auth';

export const authState = reactive({
    loading: true,
    isAuthenticated: false,
    isAnonymous: false,
    name: '',
    email: null,
    role: null, // 'Administrator' | 'Data Admin' | 'Viewer' | 'Deny' | null
    ssoConfigured: false,
});

export const isAdministrator = () => authState.role === 'Administrator';
export const isDataAdminOrAbove = () => authState.role === 'Administrator' || authState.role === 'Data Admin';
export const isDenied = () => authState.role === 'Deny';
// Any role that can actually use the app (Viewer/Data Admin/Administrator) — Deny cannot.
export const canUseApp = () => authState.isAuthenticated && authState.role !== 'Deny';

export async function fetchAuthState() {
    authState.loading = true;
    try {
        const res = await fetch(`${AUTH_BASE_URL}/me`);
        if (res.ok) {
            const data = await res.json();
            authState.isAuthenticated = true;
            authState.isAnonymous = data.is_anonymous;
            authState.name = data.name;
            authState.email = data.email;
            authState.role = data.role;
            authState.ssoConfigured = data.sso_configured;
        } else {
            authState.isAuthenticated = false;
            authState.isAnonymous = false;
            authState.name = '';
            authState.email = null;
            authState.role = null;
        }
    } catch (e) {
        console.warn('Could not reach /auth/me', e);
        authState.isAuthenticated = false;
    } finally {
        authState.loading = false;
    }
    return authState;
}

// Full-page navigation (not fetch) — this is meant to leave the app's
// origin entirely to reach the identity provider.
export function redirectToLogin() {
    const next = window.location.pathname + window.location.search;
    window.location.href = `${AUTH_BASE_URL}/login?next=${encodeURIComponent(next)}`;
}

export function logout() {
    window.location.href = `${AUTH_BASE_URL}/logout`;
}

// Installed once by app.js: any same-origin 401 while a real (non-anonymous)
// session is expected means the session expired mid-use — bounce to login
// rather than leaving the SPA showing stale/broken state. This is the one
// centralized place that handles it, since the app's ~50 fetch() call sites
// across Settings.js/DashboardView.js/LogsView.js/app.js all rely on the
// same-origin cookie implicitly and have no per-call auth handling of their own.
export function installUnauthorizedRedirect() {
    const originalFetch = window.fetch.bind(window);
    window.fetch = async (...args) => {
        const response = await originalFetch(...args);
        if (response.status === 401 && !authState.isAnonymous) {
            redirectToLogin();
        }
        return response;
    };
}
