const { ref, reactive, computed, onMounted, watch, nextTick } = Vue;

import { isAdministrator } from '../services/auth.js';

const VALID_ROLES = ['Administrator', 'Data Admin', 'Viewer', 'Deny'];

// ---------------------------------------------------------------------------
// Field definitions per connector type — drives the accordion forms
// ---------------------------------------------------------------------------
const CONNECTOR_FIELDS = {
    csv: [
        { key: 'name', label: 'Name', type: 'text', required: true, hint: 'SQL-safe identifier used in queries (e.g. orders)' },
        { key: 'title', label: 'Title', type: 'text', required: true, hint: 'Human-readable display name shown in the UI' },
        { key: 'filepath', label: 'File Path', type: 'text', required: true, hint: 'Absolute path inside the container (e.g. /app/data/orders.csv)' },
        { key: 'description', label: 'Description', type: 'text', required: false, hint: 'Optional free-text description' },
    ],
    json: [
        { key: 'name', label: 'Name', type: 'text', required: true, hint: 'SQL-safe identifier used in queries (e.g. inventory)' },
        { key: 'title', label: 'Title', type: 'text', required: true, hint: 'Human-readable display name shown in the UI' },
        { key: 'filepath', label: 'File Path', type: 'text', required: true, hint: 'Absolute path inside the container (e.g. /app/data/inventory.csv)' },
        { key: 'description', label: 'Description', type: 'text', required: false, hint: 'Optional free-text description' },
    ],
    bigquery: [
        { key: 'name', label: 'Name', type: 'text', required: true, hint: 'SQL-safe identifier for queries (e.g. bq_events)' },
        { key: 'title', label: 'Title', type: 'text', required: true, hint: 'Human-readable display name' },
        { key: 'project_id', label: 'Project ID', type: 'text', required: true, hint: 'GCP project identifier (e.g. my-gcp-project)' },
        { key: 'dataset_id', label: 'Dataset ID', type: 'text', required: true, hint: 'BigQuery dataset identifier (e.g. analytics)' },
        { key: 'table_id', label: 'Table ID', type: 'text', required: true, hint: 'BigQuery table identifier (e.g. events)' },
        { key: 'credentials_path', label: 'Credentials Path', type: 'text', required: true, hint: 'Container path to service-account JSON key (e.g. /run/secrets/bq_sa.json)' },
        { key: 'query', label: 'Query', type: 'textarea', required: false, hint: 'Optional override SQL — defaults to SELECT * FROM <table>' },
        { key: 'description', label: 'Description', type: 'text', required: false, hint: 'Optional free-text description' },
    ],
    parquet: [
        { key: 'name', label: 'ID', type: 'text', required: true, hint: 'unique name in the system' },
        { key: 'title', label: 'Title', type: 'text', required: true, hint: 'Human-readable display name' },
        { key: 'filepath', label: 'File Path', type: 'text', required: true, hint: '/data/agg_daily/**/*.parquet' },
        { key: 'description', label: 'Description', type: 'text', required: false, hint: 'Optional free-text description' },
        { key: 'hive_partitioning', label: 'Enable Hive Partitioning', type: 'checkbox', required: false, hint: 'Infer Hive partitioning from paths' },
    ],
};

// Build an initial empty form object for a given type
function buildEmptyForm(type) {
    const fields = CONNECTOR_FIELDS[type] || [];
    const obj = {};
    fields.forEach(f => {
        if (f.type === 'checkbox') obj[f.key] = false;
        else obj[f.key] = '';
    });
    return obj;
}

export default {
    props: {
        apiBaseUrl: {
            type: String,
            required: true
        }
    },
    emits: ['dashboards-changed'],
    setup(props, { emit }) {
        const activeTab = ref('data_sources');
        const connectorTypes = ref([]);
        const dataSources = ref([]);
        const expandedAccordion = ref(null);

        // Track per-accordion submission in-progress state
        const submitting = reactive({});
        const isRefreshing = ref(false);
        const isLoading = ref(false);

        const dashboardsList = ref([]);
        const isDashboardsLoading = ref(false);
        const dashboardMessage = reactive({
            text: '',
            type: '',
            detail: '',
            visible: false
        });
        const isDashboardAccordionOpen = ref(false);
        const dashboardFilePath = ref('');
        const isRegisteringDashboard = ref(false);

        const toggleDashboardAccordion = () => {
            isDashboardAccordionOpen.value = !isDashboardAccordionOpen.value;
            if (isDashboardAccordionOpen.value) dashboardFilePath.value = '';
        };

        const widgetSetsList = ref([]);
        const isWidgetSetsLoading = ref(false);
        const widgetMessage = reactive({
            text: '',
            type: '',
            detail: '',
            visible: false
        });
        const isWidgetAccordionOpen = ref(false);
        const widgetFolderPath = ref('');
        const isRegisteringWidgetSet = ref(false);

        const toggleWidgetAccordion = () => {
            isWidgetAccordionOpen.value = !isWidgetAccordionOpen.value;
            if (isWidgetAccordionOpen.value) widgetFolderPath.value = '';
        };

        const sqlQuery = ref('SELECT * FROM data_source_name;');
        const sqlResults = ref('');
        const isSqlLoading = ref(false);

        // Access Tab state — backed by GET/PUT /system/access (Administrator only;
        // this whole component is only ever mounted for Administrator/Data Admin,
        // and the Access/SSO tabs are further hidden from Data Admin in the template).
        const accessAnonymousAccess = ref(true);
        const accessNewMapping = reactive({
            claim: 'groups',
            value: '',
            role: 'Administrator'
        });
        const accessMappings = ref([]);
        const accessMessage = reactive({ text: '', type: '', detail: '', visible: false });
        const isSavingAccess = ref(false);

        const showAccessMessage = (text, type = 'success', detail = '') => {
            accessMessage.text = text;
            accessMessage.type = type;
            accessMessage.detail = detail;
            accessMessage.visible = true;
            if (type !== 'error') {
                setTimeout(() => { accessMessage.visible = false; }, 6000);
            }
        };
        const clearAccessMessage = () => { accessMessage.visible = false; };

        const fetchAccessSettings = async () => {
            try {
                const res = await fetch(`${props.apiBaseUrl}/system/access`);
                if (res.ok) {
                    const data = await res.json();
                    accessAnonymousAccess.value = !!data.anonymous_access;
                    accessMappings.value = data.role_mappings || [];
                } else {
                    console.error('Failed to fetch access settings', res.status);
                }
            } catch (e) {
                console.error('Network error fetching access settings', e);
            }
        };

        const saveAccessSettings = async () => {
            isSavingAccess.value = true;
            try {
                const res = await fetch(`${props.apiBaseUrl}/system/access`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        anonymous_access: accessAnonymousAccess.value,
                        role_mappings: accessMappings.value,
                    })
                });
                if (res.ok) {
                    const data = await res.json();
                    accessAnonymousAccess.value = !!data.anonymous_access;
                    accessMappings.value = data.role_mappings || [];
                    showAccessMessage('Access settings saved successfully.');
                } else {
                    const err = await res.json().catch(() => ({}));
                    showAccessMessage(err.detail || 'Failed to save access settings.', 'error');
                }
            } catch (e) {
                showAccessMessage('Network error saving access settings.', 'error');
            } finally {
                isSavingAccess.value = false;
            }
        };

        const setAnonymousAccess = (value) => {
            if (accessAnonymousAccess.value === value) return;
            accessAnonymousAccess.value = value;
            saveAccessSettings();
        };

        const addAccessMapping = () => {
            if (!accessNewMapping.claim.trim() || !accessNewMapping.value.trim()) return;
            accessMappings.value.push({ claim: accessNewMapping.claim.trim(), value: accessNewMapping.value.trim(), role: accessNewMapping.role });
            accessNewMapping.value = '';
            saveAccessSettings();
        };
        const deleteAccessMapping = (index) => {
            accessMappings.value.splice(index, 1);
            saveAccessSettings();
        };

        // AI Tab state
        const aiProvider = ref('Gemini');
        const aiSettings = reactive({
            apiKey: '',
            model: '',
            organizationId: '',
            baseUrl: ''
        });
        const aiMessage = reactive({ text: '', type: '', detail: '', visible: false });
        const clearAiMessage = () => { aiMessage.visible = false; };

        // SSO Tab state — backed by GET/PUT /system/sso and POST /system/sso/test.
        const ssoSettings = reactive({
            issuerUrl: '',
            clientId: '',
            clientSecret: '',
            clientSecretSet: false,
            scopes: 'openid profile email',
            redirectUrl: '',
        });
        const ssoMessage = reactive({ text: '', type: '', detail: '', visible: false });
        const isSavingSso = ref(false);
        const isTestingSso = ref(false);

        // Suggested value shown as a placeholder — the admin must still save it
        // explicitly (and register the exact same URI with the identity provider).
        const suggestedRedirectUrl = computed(() => `${window.location.origin}/auth/callback`);

        const showSsoMessage = (text, type = 'success', detail = '') => {
            ssoMessage.text = text;
            ssoMessage.type = type;
            ssoMessage.detail = detail;
            ssoMessage.visible = true;
            if (type !== 'error') {
                setTimeout(() => { ssoMessage.visible = false; }, 6000);
            }
        };
        const clearSsoMessage = () => { ssoMessage.visible = false; };

        const fetchSsoSettings = async () => {
            try {
                const res = await fetch(`${props.apiBaseUrl}/system/sso`);
                if (res.ok) {
                    const data = await res.json();
                    ssoSettings.issuerUrl = data.issuer_url || '';
                    ssoSettings.clientId = data.client_id || '';
                    ssoSettings.clientSecret = '';
                    ssoSettings.clientSecretSet = !!data.client_secret_set;
                    ssoSettings.scopes = data.scopes || 'openid profile email';
                    ssoSettings.redirectUrl = data.redirect_uri || '';
                } else {
                    console.error('Failed to fetch SSO settings', res.status);
                }
            } catch (e) {
                console.error('Network error fetching SSO settings', e);
            }
        };

        const saveSsoSettings = async () => {
            isSavingSso.value = true;
            try {
                const res = await fetch(`${props.apiBaseUrl}/system/sso`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        issuer_url: ssoSettings.issuerUrl || null,
                        client_id: ssoSettings.clientId || null,
                        client_secret: ssoSettings.clientSecret || null, // blank = keep existing
                        scopes: ssoSettings.scopes || null,
                        redirect_uri: ssoSettings.redirectUrl || null,
                    })
                });
                if (res.ok) {
                    showSsoMessage('SSO settings saved. Use "Sign In" below to test a real login round trip.');
                    ssoSettings.clientSecret = '';
                    await fetchSsoSettings();
                } else {
                    const err = await res.json().catch(() => ({}));
                    showSsoMessage(err.detail || 'Failed to save SSO settings.', 'error');
                }
            } catch (e) {
                showSsoMessage('Network error saving SSO settings.', 'error');
            } finally {
                isSavingSso.value = false;
            }
        };

        const testSsoConnection = async () => {
            if (!ssoSettings.issuerUrl.trim()) {
                showSsoMessage('Enter an issuer URL first.', 'warning');
                return;
            }
            isTestingSso.value = true;
            try {
                const res = await fetch(`${props.apiBaseUrl}/system/sso/test`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ issuer_url: ssoSettings.issuerUrl.trim() })
                });
                const data = await res.json().catch(() => ({}));
                if (res.ok && data.ok) {
                    showSsoMessage(
                        'Discovery document resolved successfully.',
                        'success',
                        `Authorization: ${data.authorization_endpoint}\nToken: ${data.token_endpoint}`
                    );
                } else {
                    showSsoMessage(data.message || 'Could not resolve the discovery document.', 'error');
                }
            } catch (e) {
                showSsoMessage('Network error testing SSO connection.', 'error');
            } finally {
                isTestingSso.value = false;
            }
        };

        // Opens the real OIDC round trip in a new tab so an admin can verify the
        // resolved role before flipping "Allow Anonymous Access" off — /auth/login
        // works regardless of the current anonymous-access setting.
        const trySsoSignIn = () => {
            window.open(`${window.AUTH_BASE_URL || '/auth'}/login`, '_blank');
        };

        const settings = reactive({
            auto_refresh_mode: 'disabled',
            basic_min: 60,
            cron_str: '*/15 * * * 1-5',

            redis_host: '',
            redis_port: '',
            redis_user: '',
            redis_password: '',
            redis_password_set: false,
            row_limit: 1000,
            redis_ttl_seconds: 1800,
            redis_tls_enabled: 'disabled',

            syslog_enabled: 'disabled',
            syslog_host: '',
            syslog_port: '',
            syslog_tls_enabled: 'disabled',
            syslog_cert_path: '',
            syslog_key_path: '',
            syslog_ca_cert_path: ''
        });

        const getCookie = (name) => {
            const value = `; ${document.cookie}`;
            const parts = value.split(`; ${name}=`);
            if (parts.length === 2) return parts.pop().split(';').shift();
            return null;
        };

        const setCookie = (name, value, days) => {
            const d = new Date();
            d.setTime(d.getTime() + (days * 24 * 60 * 60 * 1000));
            document.cookie = `${name}=${value};expires=${d.toUTCString()};path=/`;
        };

        const uiTheme = ref(getCookie('uiTheme') || 'light');
        const changeTheme = () => {
            setCookie('uiTheme', uiTheme.value, 365);
            document.documentElement.setAttribute('data-theme', uiTheme.value);
        };

        const isSavingRedis = ref(false);
        const isSavingSyslog = ref(false);
        const isResettingCache = ref(false);

        const message = reactive({
            text: '',
            type: '',   // 'success' | 'error' | 'warning'
            detail: '',
            visible: false
        });

        // Forms — keyed by connector type (populated when connectorTypes loads)
        const forms = reactive({});

        // Validation errors — keyed by type, then field key
        const formErrors = reactive({});

        // ---------------------------------------------------------------------------
        // Helpers
        // ---------------------------------------------------------------------------

        const showMessage = (text, type = 'success', detail = '') => {
            message.text = text;
            message.type = type;
            message.detail = detail;
            message.visible = true;
            if (type !== 'error') {
                setTimeout(() => { message.visible = false; }, 6000);
            }
        };

        const clearMessage = () => { message.visible = false; };

        const showDashboardMessage = (text, type = 'success', detail = '') => {
            dashboardMessage.text = text;
            dashboardMessage.type = type;
            dashboardMessage.detail = detail;
            dashboardMessage.visible = true;
            if (type !== 'error') {
                setTimeout(() => { dashboardMessage.visible = false; }, 6000);
            }
        };

        const clearDashboardMessage = () => { dashboardMessage.visible = false; };

        const showWidgetMessage = (text, type = 'success', detail = '') => {
            widgetMessage.text = text;
            widgetMessage.type = type;
            widgetMessage.detail = detail;
            widgetMessage.visible = true;
            if (type !== 'error') {
                setTimeout(() => { widgetMessage.visible = false; }, 6000);
            }
        };

        const clearWidgetMessage = () => { widgetMessage.visible = false; };

        const formatTimestamp = (ts) => {
            if (!ts || ts === 0) return '—';
            try {
                return new Date(ts * 1000).toLocaleString();
            } catch {
                return '—';
            }
        };

        const getStatusLabel = (ds) => {
            if (!ds.last_updated || ds.last_updated === 0) return 'Never loaded';
            return 'Loaded';
        };

        const getStatusClass = (ds) => {
            if (!ds.last_updated || ds.last_updated === 0) return 'status-badge status-error';
            return 'status-badge status-ok';
        };

        const getConnectorIcon = (type) => {
            const icons = { csv: 'fa-file-csv', json: 'fa-file-code', bigquery: 'fa-database', parquet: 'fa-file-lines' };
            return icons[type] || 'fa-plug';
        };

        const getConnectorLabel = (type) => {
            const labels = { csv: 'CSV', json: 'JSON', bigquery: 'BigQuery', parquet: 'Parquet' };
            return labels[type] || type.toUpperCase();
        };

        // ---------------------------------------------------------------------------
        // Validation
        // ---------------------------------------------------------------------------

        const validateForm = (type) => {
            const fields = CONNECTOR_FIELDS[type] || [];
            const form = forms[type] || {};
            const errors = {};
            fields.forEach(f => {
                if (f.required && !form[f.key]?.trim()) {
                    errors[f.key] = `${f.label} is required.`;
                }
            });
            formErrors[type] = errors;
            return Object.keys(errors).length === 0;
        };

        // ---------------------------------------------------------------------------
        // API calls
        // ---------------------------------------------------------------------------

        const fetchConnectorTypes = async () => {
            try {
                const res = await fetch(`${props.apiBaseUrl}/system/connector-types`);
                if (res.ok) {
                    const types = await res.json();
                    connectorTypes.value = types;
                    // Initialise forms + errors for each type
                    types.forEach(type => {
                        if (!forms[type]) {
                            forms[type] = buildEmptyForm(type);
                        }
                        if (!formErrors[type]) {
                            formErrors[type] = {};
                        }
                        submitting[type] = false;
                    });
                } else {
                    console.error('Failed to fetch connector types', res.status);
                }
            } catch (e) {
                console.error('Network error fetching connector types', e);
            }
        };

        const fetchDataSources = async () => {
            isLoading.value = true;
            try {
                const res = await fetch(`${props.apiBaseUrl}/system/data-sources`);
                if (res.ok) {
                    dataSources.value = await res.json();
                } else {
                    console.error('Failed to fetch data sources', res.status);
                }
            } catch (e) {
                console.error('Network error fetching data sources', e);
            } finally {
                isLoading.value = false;
            }
        };

        const fetchDashboards = async () => {
            isDashboardsLoading.value = true;
            try {
                const res = await fetch(`${props.apiBaseUrl}/dashboards`);
                if (res.ok) {
                    const data = await res.json();
                    dashboardsList.value = data.map(d => ({ ...d, _status: 'Loaded', _errorMsg: '', _reloading: false, _deleting: false }));
                } else {
                    console.error('Failed to fetch dashboards', res.status);
                }
            } catch (e) {
                console.error('Network error fetching dashboards', e);
            } finally {
                isDashboardsLoading.value = false;
            }
        };

        const reloadDashboard = async (dashId) => {
            const dash = dashboardsList.value.find(d => d.id === dashId);
            if (!dash) return;
            dash._reloading = true;
            dash._status = 'Loaded';
            dash._errorMsg = '';
            clearDashboardMessage();
            try {
                const res = await fetch(`${props.apiBaseUrl}/dashboards/load`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ filepath: dash.filepath })
                });
                if (res.ok) {
                    dash._status = 'Loaded';
                    showDashboardMessage(`Dashboard "${dash.id}" reloaded successfully.`, 'success');
                } else {
                    const err = await res.json().catch(() => ({}));
                    dash._status = 'error';
                    dash._errorMsg = err.detail || `HTTP ${res.status}`;
                    showDashboardMessage(`Failed to reload dashboard "${dash.id}".`, 'error', err.detail);
                }
            } catch (e) {
                dash._status = 'error';
                dash._errorMsg = 'Network error';
                showDashboardMessage('Network error reloading dashboard.', 'error');
            } finally {
                dash._reloading = false;
            }
        };

        const reloadAllDashboards = async () => {
            isDashboardsLoading.value = true;
            clearDashboardMessage();
            let successCount = 0;
            let failCount = 0;
            for (const dash of dashboardsList.value) {
                dash._reloading = true;
                dash._status = 'Loaded';
                dash._errorMsg = '';
                try {
                    const res = await fetch(`${props.apiBaseUrl}/dashboards/load`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ filepath: dash.filepath })
                    });
                    if (res.ok) {
                        dash._status = 'Loaded';
                        successCount++;
                    } else {
                        const err = await res.json().catch(() => ({}));
                        dash._status = 'error';
                        dash._errorMsg = err.detail || `HTTP ${res.status}`;
                        failCount++;
                    }
                } catch (e) {
                    dash._status = 'error';
                    dash._errorMsg = 'Network error';
                    failCount++;
                } finally {
                    dash._reloading = false;
                }
            }
            isDashboardsLoading.value = false;
            if (failCount === 0 && successCount > 0) {
                showDashboardMessage(`Successfully reloaded ${successCount} dashboard(s).`, 'success');
            } else if (failCount > 0) {
                showDashboardMessage(`Reload completed with errors. ${successCount} succeeded, ${failCount} failed.`, 'warning');
            }
        };

        const registerDashboard = async () => {
            if (!dashboardFilePath.value.trim()) return;
            isRegisteringDashboard.value = true;
            clearDashboardMessage();
            try {
                const res = await fetch(`${props.apiBaseUrl}/dashboards/load`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ filepath: dashboardFilePath.value.trim() })
                });
                if (res.ok) {
                    showDashboardMessage(`Dashboard "${dashboardFilePath.value}" registered successfully.`, 'success');
                    dashboardFilePath.value = '';
                    isDashboardAccordionOpen.value = false;
                    fetchDashboards();
                    emit('dashboards-changed');
                } else {
                    const err = await res.json().catch(() => ({}));
                    showDashboardMessage(`Failed to register dashboard.`, 'error', err.detail || `HTTP ${res.status}`);
                }
            } catch (e) {
                showDashboardMessage('Network error registering dashboard.', 'error');
            } finally {
                isRegisteringDashboard.value = false;
            }
        };

        const deleteDashboard = async (dashId) => {
            const dash = dashboardsList.value.find(d => d.id === dashId);
            if (!dash) return;
            if (!confirm(`Are you sure you want to delete dashboard "${dash.name}"?`)) return;

            dash._deleting = true;
            clearDashboardMessage();
            try {
                const res = await fetch(`${props.apiBaseUrl}/dashboards/${dash.id}`, {
                    method: 'DELETE'
                });
                if (res.ok) {
                    showDashboardMessage(`Dashboard "${dash.id}" deleted successfully.`, 'success');
                    fetchDashboards();
                    emit('dashboards-changed');
                } else {
                    const err = await res.json().catch(() => ({}));
                    showDashboardMessage(`Failed to delete dashboard "${dash.id}".`, 'error', err.detail || `HTTP ${res.status}`);
                    dash._deleting = false;
                }
            } catch (e) {
                showDashboardMessage('Network error deleting dashboard.', 'error');
                dash._deleting = false;
            }
        };

        const fetchWidgetSets = async () => {
            isWidgetSetsLoading.value = true;
            try {
                const res = await fetch(`${props.apiBaseUrl}/widgets`);
                if (res.ok) {
                    const data = await res.json();
                    widgetSetsList.value = data.map(w => ({
                        ...w, _status: 'Loaded', _errorMsg: '', _reloading: false, _deleting: false, _activating: false
                    }));
                } else {
                    console.error('Failed to fetch widget sets', res.status);
                }
            } catch (e) {
                console.error('Network error fetching widget sets', e);
            } finally {
                isWidgetSetsLoading.value = false;
            }
        };

        const reloadWidgetSet = async (widgetId) => {
            const ws = widgetSetsList.value.find(w => w.id === widgetId);
            if (!ws) return;
            ws._reloading = true;
            ws._status = 'Loaded';
            ws._errorMsg = '';
            clearWidgetMessage();
            try {
                const res = await fetch(`${props.apiBaseUrl}/widgets/load`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ folder_path: ws.folder_path })
                });
                if (res.ok) {
                    ws._status = 'Loaded';
                    showWidgetMessage(`Widget set "${ws.id}" reloaded successfully.`, 'success');
                    fetchWidgetSets();
                } else {
                    const err = await res.json().catch(() => ({}));
                    ws._status = 'error';
                    ws._errorMsg = err.detail || `HTTP ${res.status}`;
                    showWidgetMessage(`Failed to reload widget set "${ws.id}".`, 'error', err.detail);
                }
            } catch (e) {
                ws._status = 'error';
                ws._errorMsg = 'Network error';
                showWidgetMessage('Network error reloading widget set.', 'error');
            } finally {
                ws._reloading = false;
            }
        };

        const reloadAllWidgetSets = async () => {
            isWidgetSetsLoading.value = true;
            clearWidgetMessage();
            let successCount = 0;
            let failCount = 0;
            for (const ws of widgetSetsList.value) {
                ws._reloading = true;
                ws._status = 'Loaded';
                ws._errorMsg = '';
                try {
                    const res = await fetch(`${props.apiBaseUrl}/widgets/load`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ folder_path: ws.folder_path })
                    });
                    if (res.ok) {
                        ws._status = 'Loaded';
                        successCount++;
                    } else {
                        const err = await res.json().catch(() => ({}));
                        ws._status = 'error';
                        ws._errorMsg = err.detail || `HTTP ${res.status}`;
                        failCount++;
                    }
                } catch (e) {
                    ws._status = 'error';
                    ws._errorMsg = 'Network error';
                    failCount++;
                } finally {
                    ws._reloading = false;
                }
            }
            isWidgetSetsLoading.value = false;
            if (failCount === 0 && successCount > 0) {
                showWidgetMessage(`Successfully reloaded ${successCount} widget set(s).`, 'success');
            } else if (failCount > 0) {
                showWidgetMessage(`Reload completed with errors. ${successCount} succeeded, ${failCount} failed.`, 'warning');
            }
        };

        const registerWidgetSet = async () => {
            if (!widgetFolderPath.value.trim()) return;
            isRegisteringWidgetSet.value = true;
            clearWidgetMessage();
            try {
                const res = await fetch(`${props.apiBaseUrl}/widgets/load`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ folder_path: widgetFolderPath.value.trim() })
                });
                if (res.ok) {
                    showWidgetMessage(`Widget set "${widgetFolderPath.value}" registered successfully.`, 'success');
                    widgetFolderPath.value = '';
                    isWidgetAccordionOpen.value = false;
                    fetchWidgetSets();
                } else {
                    const err = await res.json().catch(() => ({}));
                    showWidgetMessage(`Failed to register widget set.`, 'error', err.detail || `HTTP ${res.status}`);
                }
            } catch (e) {
                showWidgetMessage('Network error registering widget set.', 'error');
            } finally {
                isRegisteringWidgetSet.value = false;
            }
        };

        const deleteWidgetSet = async (widgetId) => {
            const ws = widgetSetsList.value.find(w => w.id === widgetId);
            if (!ws) return;
            if (ws.active) {
                showWidgetMessage(`Cannot delete "${ws.id}" while it is active.`, 'error', 'Activate a different widget set first.');
                return;
            }
            if (!confirm(`Are you sure you want to delete widget set "${ws.title}"?`)) return;

            ws._deleting = true;
            clearWidgetMessage();
            try {
                const res = await fetch(`${props.apiBaseUrl}/widgets/${ws.id}`, {
                    method: 'DELETE'
                });
                if (res.ok) {
                    showWidgetMessage(`Widget set "${ws.id}" deleted successfully.`, 'success');
                    fetchWidgetSets();
                } else {
                    const err = await res.json().catch(() => ({}));
                    showWidgetMessage(`Failed to delete widget set "${ws.id}".`, 'error', err.detail || `HTTP ${res.status}`);
                    ws._deleting = false;
                }
            } catch (e) {
                showWidgetMessage('Network error deleting widget set.', 'error');
                ws._deleting = false;
            }
        };

        const activateWidgetSet = async (widgetId) => {
            const ws = widgetSetsList.value.find(w => w.id === widgetId);
            if (!ws || ws.active) return;
            ws._activating = true;
            clearWidgetMessage();
            try {
                const res = await fetch(`${props.apiBaseUrl}/widgets/${ws.id}/activate`, {
                    method: 'POST'
                });
                if (res.ok) {
                    showWidgetMessage(`Widget set "${ws.id}" is now active. Dashboards will use its style.`, 'success');
                    fetchWidgetSets();
                } else {
                    const err = await res.json().catch(() => ({}));
                    showWidgetMessage(`Failed to activate widget set "${ws.id}".`, 'error', err.detail || `HTTP ${res.status}`);
                }
            } catch (e) {
                showWidgetMessage('Network error activating widget set.', 'error');
            } finally {
                ws._activating = false;
            }
        };

        const fetchSettings = async () => {
            try {
                const res = await fetch(`${props.apiBaseUrl}/system/settings`);
                if (res.ok) {
                    const data = await res.json();
                    if (data.auto_refresh === null || data.auto_refresh === undefined || data.auto_refresh === 'disabled') {
                        settings.auto_refresh_mode = 'disabled';
                    } else if (typeof data.auto_refresh === 'number') {
                        settings.auto_refresh_mode = 'basic';
                        settings.basic_min = data.auto_refresh;
                    } else if (Array.isArray(data.auto_refresh)) {
                        settings.auto_refresh_mode = 'cron';
                        settings.cron_str = data.auto_refresh.join('\n');
                    }

                    settings.redis_host = data.redis_host || '';
                    settings.redis_port = data.redis_port || '';
                    settings.redis_user = data.redis_user || '';
                    // The API never echoes back the real password — only whether one is set.
                    // Leaving the field blank on save keeps the existing password unchanged.
                    settings.redis_password = '';
                    settings.redis_password_set = !!data.redis_password_set;
                    settings.row_limit = data.row_limit || 1000;
                    settings.redis_ttl_seconds = data.redis_ttl_seconds || 1800;
                    settings.redis_tls_enabled = data.redis_tls_enabled ? 'enabled' : 'disabled';

                    if (data.syslog) {
                        settings.syslog_enabled = data.syslog.enabled ? 'enabled' : 'disabled';
                        settings.syslog_host = data.syslog.host || '';
                        settings.syslog_port = data.syslog.port || '';
                        settings.syslog_tls_enabled = data.syslog.tls_enabled ? 'enabled' : 'disabled';
                        settings.syslog_cert_path = data.syslog.cert_path || '';
                        settings.syslog_key_path = data.syslog.key_path || '';
                        settings.syslog_ca_cert_path = data.syslog.ca_cert_path || '';
                    }
                }
            } catch (e) {
                console.error('Failed to fetch settings', e);
            }
        };

        const saveSettings = async () => {
            let auto_refresh = null;
            if (settings.auto_refresh_mode === 'disabled') {
                auto_refresh = 'disabled';
            } else if (settings.auto_refresh_mode === 'basic') {
                auto_refresh = Number(settings.basic_min);
            } else if (settings.auto_refresh_mode === 'cron') {
                auto_refresh = settings.cron_str.split('\n').map(s => s.trim()).filter(s => s);
            }
            try {
                const res = await fetch(`${props.apiBaseUrl}/system/settings`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ auto_refresh })
                });
                if (res.ok) {
                    showMessage('Settings saved successfully.');
                } else {
                    const err = await res.json();
                    showMessage(err.detail || 'Failed to save settings.', 'error');
                }
            } catch (e) {
                showMessage('Network error saving settings.', 'error');
            }
        };

        const saveRedisSettings = async () => {
            isSavingRedis.value = true;
            try {
                const payload = {
                    redis_host: settings.redis_host || null,
                    redis_port: settings.redis_port ? Number(settings.redis_port) : null,
                    redis_user: settings.redis_user || null,
                    redis_password: settings.redis_password || null,
                    row_limit: settings.row_limit ? Number(settings.row_limit) : 1000,
                    redis_ttl_seconds: settings.redis_ttl_seconds ? Number(settings.redis_ttl_seconds) : 1800,
                    redis_tls_enabled: settings.redis_tls_enabled === 'enabled'
                };
                const res = await fetch(`${props.apiBaseUrl}/system/settings`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
                if (res.ok) {
                    showMessage('Redis settings saved successfully.');
                } else {
                    const err = await res.json().catch(() => ({}));
                    showMessage(err.detail || 'Failed to save Redis settings.', 'error');
                }
            } catch (e) {
                showMessage('Network error saving Redis settings.', 'error');
            } finally {
                isSavingRedis.value = false;
            }
        };

        const resetRedisCache = async () => {
            if (!confirm('Clear the entire Redis cache? Cached dashboard data will be recomputed on next load.')) return;
            isResettingCache.value = true;
            try {
                const res = await fetch(`${props.apiBaseUrl}/system/cache/reset`, {
                    method: 'POST'
                });
                if (res.ok) {
                    showMessage('Redis cache cleared successfully.');
                } else {
                    const err = await res.json().catch(() => ({}));
                    showMessage(err.detail || 'Failed to clear Redis cache.', 'error');
                }
            } catch (e) {
                showMessage('Network error clearing Redis cache.', 'error');
            } finally {
                isResettingCache.value = false;
            }
        };

        const saveSyslogSettings = async () => {
            isSavingSyslog.value = true;
            try {
                const payload = {
                    syslog: {
                        enabled: settings.syslog_enabled === 'enabled',
                        host: settings.syslog_host || null,
                        port: settings.syslog_port ? Number(settings.syslog_port) : null,
                        tls_enabled: settings.syslog_tls_enabled === 'enabled',
                        cert_path: settings.syslog_cert_path || null,
                        key_path: settings.syslog_key_path || null,
                        ca_cert_path: settings.syslog_ca_cert_path || null
                    }
                };
                const res = await fetch(`${props.apiBaseUrl}/system/settings`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
                if (res.ok) {
                    showMessage('Syslog settings saved successfully.');
                } else {
                    const err = await res.json().catch(() => ({}));
                    showMessage(err.detail || 'Failed to save Syslog settings.', 'error');
                }
            } catch (e) {
                showMessage('Network error saving Syslog settings.', 'error');
            } finally {
                isSavingSyslog.value = false;
            }
        };

        const toggleAccordion = (type) => {
            if (expandedAccordion.value === type) {
                expandedAccordion.value = null;
            } else {
                expandedAccordion.value = type;
                // Clear previous errors when opening a new accordion
                formErrors[type] = {};
            }
        };

        const addDataSource = async (type) => {
            if (!validateForm(type)) return;

            submitting[type] = true;
            clearMessage();

            // Build payload — strip empty optional strings → null
            const formData = forms[type];
            const payload = {};
            (CONNECTOR_FIELDS[type] || []).forEach(f => {
                if (f.type === 'checkbox') {
                    payload[f.key] = formData[f.key] || false;
                } else {
                    const val = formData[f.key]?.trim();
                    payload[f.key] = (val === '' && !f.required) ? null : (val || null);
                }
            });

            try {
                const res = await fetch(`${props.apiBaseUrl}/system/data-sources/${type}`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });

                if (res.ok || res.status === 201) {
                    const body = await res.json();
                    showMessage(
                        `Data source "${body.name}" registered successfully.`,
                        'success',
                        `Type: ${body.source_type.toUpperCase()} — Last updated: ${formatTimestamp(body.last_updated)}`
                    );
                    // Reset form and close accordion
                    forms[type] = buildEmptyForm(type);
                    formErrors[type] = {};
                    expandedAccordion.value = null;
                    fetchDataSources();
                } else {
                    const err = await res.json();
                    const detail = err.detail || `Failed to register ${type} data source.`;
                    showMessage(`Registration failed.`, 'error', detail);
                }
            } catch (e) {
                showMessage('Network error — check your connection and try again.', 'error');
            } finally {
                submitting[type] = false;
            }
        };

        const deleteDataSource = async (name) => {
            if (!confirm(`Delete data source "${name}"?\n\nAny dashboards referencing this source will fail to load data until a replacement with the same name is registered.`)) return;
            clearMessage();
            try {
                const res = await fetch(`${props.apiBaseUrl}/system/data-sources/${encodeURIComponent(name)}`, {
                    method: 'DELETE'
                });
                if (res.ok || res.status === 204) {
                    showMessage(`Data source "${name}" deleted.`);
                    fetchDataSources();
                } else {
                    const err = await res.json().catch(() => ({}));
                    showMessage('Deletion failed.', 'error', err.detail || `HTTP ${res.status}`);
                }
            } catch (e) {
                showMessage('Network error deleting data source.', 'error');
            }
        };

        const reloadDataSource = async (name) => {
            clearMessage();
            isRefreshing.value = true;
            try {
                const res = await fetch(`${props.apiBaseUrl}/system/refresh`, {
                    method: 'POST'
                });
                if (res.ok || res.status === 202) {
                    showMessage(`Global refresh triggered. Data sources are reloading…`, 'success');
                    // Give the backend a moment then refetch
                    setTimeout(fetchDataSources, 2500);
                } else {
                    const err = await res.json().catch(() => ({}));
                    const code = res.status;
                    if (code === 429) {
                        showMessage('Rate limit reached.', 'error', err.detail || 'Max 2 refreshes per minute. Please wait and try again.');
                    } else {
                        showMessage('Refresh failed.', 'error', err.detail || `HTTP ${code}`);
                    }
                }
            } catch (e) {
                showMessage('Network error triggering refresh.', 'error');
            } finally {
                isRefreshing.value = false;
            }
        };

        const triggerGlobalRefresh = () => reloadDataSource(null);

        const runSql = async () => {
            isSqlLoading.value = true;
            sqlResults.value = '';
            try {
                const res = await fetch(`${props.apiBaseUrl}/system/sql`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ query: sqlQuery.value })
                });
                if (res.ok) {
                    sqlResults.value = await res.text();
                } else {
                    const err = await res.json().catch(() => ({}));
                    sqlResults.value = `Error: ${err.detail || res.statusText}`;
                }
            } catch (e) {
                sqlResults.value = `Network error: ${e.message}`;
            } finally {
                isSqlLoading.value = false;
            }
        };

        const copySqlResults = () => {
            navigator.clipboard.writeText(sqlResults.value).then(() => {
                showMessage('Results copied to clipboard', 'success');
            }).catch(e => {
                showMessage('Failed to copy results', 'error');
            });
        };

        let dashboardGrid = null;
        let widgetsGrid = null;
        let sqlGrid = null;
        let generalGrid = null;
        let accessGrid = null;
        let ssoGrid = null;
        let aiGrid = null;
        watch(activeTab, async (newVal) => {
            if (newVal === 'dashboards') {
                await nextTick();
                setTimeout(() => {
                    if (window.GridStack) {
                        if (dashboardGrid) {
                            try { dashboardGrid.destroy(false); } catch (e) { }
                        }
                        dashboardGrid = GridStack.init({
                            cellHeight: '70px',
                            margin: 10,
                            disableOneColumnMode: true,
                            acceptWidgets: false,
                            float: true,
                            staticGrid: true
                        }, '.dashboards-grid-stack');
                    }
                }, 50);
            } else if (newVal === 'widgets') {
                await nextTick();
                setTimeout(() => {
                    if (window.GridStack) {
                        if (widgetsGrid) {
                            try { widgetsGrid.destroy(false); } catch (e) { }
                        }
                        widgetsGrid = GridStack.init({
                            cellHeight: '70px',
                            margin: 10,
                            disableOneColumnMode: true,
                            acceptWidgets: false,
                            float: true,
                            staticGrid: true
                        }, '.widgets-grid-stack');
                    }
                }, 50);
            } else if (newVal === 'db_sql') {
                await nextTick();
                // small delay to let DOM render
                setTimeout(() => {
                    if (window.GridStack) {
                        if (sqlGrid) {
                            try { sqlGrid.destroy(false); } catch (e) { }
                        }
                        sqlGrid = GridStack.init({
                            cellHeight: '70px',
                            margin: 10,
                            disableOneColumnMode: true,
                            acceptWidgets: false,
                            float: true,
                            staticGrid: true
                        }, '.sql-grid-stack');
                    }
                }, 50);
            } else if (newVal === 'general') {
                await nextTick();
                setTimeout(() => {
                    if (window.GridStack) {
                        if (generalGrid) {
                            try { generalGrid.destroy(false); } catch (e) { }
                        }
                        generalGrid = GridStack.init({
                            cellHeight: '70px',
                            margin: 10,
                            disableOneColumnMode: true,
                            acceptWidgets: false,
                            float: true,
                            staticGrid: true
                        }, '.general-grid-stack');
                    }
                }, 50);
            } else if (newVal === 'access') {
                await nextTick();
                setTimeout(() => {
                    if (window.GridStack) {
                        if (accessGrid) { try { accessGrid.destroy(false); } catch (e) { } }
                        accessGrid = GridStack.init({ cellHeight: '70px', margin: 10, disableOneColumnMode: true, acceptWidgets: false, float: true, staticGrid: true }, '.access-grid-stack');
                    }
                }, 50);
            } else if (newVal === 'sso') {
                await nextTick();
                setTimeout(() => {
                    if (window.GridStack) {
                        if (ssoGrid) { try { ssoGrid.destroy(false); } catch (e) { } }
                        ssoGrid = GridStack.init({ cellHeight: '70px', margin: 10, disableOneColumnMode: true, acceptWidgets: false, float: true, staticGrid: true }, '.sso-grid-stack');
                    }
                }, 50);
            } else if (newVal === 'ai') {
                await nextTick();
                setTimeout(() => {
                    if (window.GridStack) {
                        if (aiGrid) { try { aiGrid.destroy(false); } catch (e) { } }
                        aiGrid = GridStack.init({ cellHeight: '70px', margin: 10, disableOneColumnMode: true, acceptWidgets: false, float: true, staticGrid: true }, '.ai-grid-stack');
                    }
                }, 50);
            } else {
                if (dashboardGrid) {
                    try { dashboardGrid.destroy(false); } catch (e) { }
                    dashboardGrid = null;
                }
                if (widgetsGrid) {
                    try { widgetsGrid.destroy(false); } catch (e) { }
                    widgetsGrid = null;
                }
                if (sqlGrid) {
                    try { sqlGrid.destroy(false); } catch (e) { }
                    sqlGrid = null;
                }
                if (generalGrid) {
                    try { generalGrid.destroy(false); } catch (e) { }
                    generalGrid = null;
                }
                if (accessGrid) {
                    try { accessGrid.destroy(false); } catch (e) { }
                    accessGrid = null;
                }
                if (ssoGrid) {
                    try { ssoGrid.destroy(false); } catch (e) { }
                    ssoGrid = null;
                }
                if (aiGrid) {
                    try { aiGrid.destroy(false); } catch (e) { }
                    aiGrid = null;
                }
            }
        });

        // ---------------------------------------------------------------------------
        // Lifecycle
        // ---------------------------------------------------------------------------

        onMounted(async () => {
            await fetchConnectorTypes();
            fetchDataSources();
            fetchDashboards();
            // Administrator-only tabs/APIs — Data Admin never sees these tabs
            // (see the template's v-if guards), so skip the calls entirely to
            // avoid a guaranteed 403 for that role.
            if (isAdministrator()) {
                fetchSettings();
                fetchWidgetSets();
                fetchAccessSettings();
                fetchSsoSettings();
            }
        });

        return {
            activeTab,
            connectorTypes,
            dataSources,
            expandedAccordion,
            submitting,
            isRefreshing,
            isLoading,
            settings,
            forms,
            formErrors,
            message,
            CONNECTOR_FIELDS,
            toggleAccordion,
            saveSettings,
            addDataSource,
            deleteDataSource,
            reloadDataSource,
            triggerGlobalRefresh,
            clearMessage,
            formatTimestamp,
            getStatusLabel,
            getStatusClass,
            getConnectorIcon,
            getConnectorLabel,
            sqlQuery,
            sqlResults,
            isSqlLoading,
            runSql,
            copySqlResults,
            uiTheme,
            changeTheme,
            isSavingRedis,
            isSavingSyslog,
            isResettingCache,
            resetRedisCache,
            saveRedisSettings,
            saveSyslogSettings,
            dashboardsList,
            isDashboardsLoading,
            dashboardMessage,
            reloadDashboard,
            reloadAllDashboards,
            clearDashboardMessage,
            isDashboardAccordionOpen,
            dashboardFilePath,
            isRegisteringDashboard,
            toggleDashboardAccordion,
            registerDashboard,
            deleteDashboard,
            widgetSetsList,
            isWidgetSetsLoading,
            widgetMessage,
            reloadWidgetSet,
            reloadAllWidgetSets,
            clearWidgetMessage,
            isWidgetAccordionOpen,
            widgetFolderPath,
            isRegisteringWidgetSet,
            toggleWidgetAccordion,
            registerWidgetSet,
            deleteWidgetSet,
            activateWidgetSet,
            accessAnonymousAccess,
            setAnonymousAccess,
            accessNewMapping,
            accessMappings,
            accessMessage,
            isSavingAccess,
            addAccessMapping,
            deleteAccessMapping,
            clearAccessMessage,
            VALID_ROLES,
            aiProvider,
            aiSettings,
            aiMessage,
            clearAiMessage,
            ssoSettings,
            ssoMessage,
            clearSsoMessage,
            isSavingSso,
            isTestingSso,
            suggestedRedirectUrl,
            saveSsoSettings,
            testSsoConnection,
            trySsoSignIn,
            isAdministrator,
        };
    },
    template: `
        <div class="settings-page">
            <header class="settings-header">
                <h1>System Settings</h1>
            </header>

            <div class="settings-tabs">
                <button class="tab-btn" :class="{ active: activeTab === 'data_sources' }" @click="activeTab = 'data_sources'">Data Sources</button>
                <button class="tab-btn" :class="{ active: activeTab === 'dashboards' }" @click="activeTab = 'dashboards'">Dashboards</button>
                <button v-if="isAdministrator()" class="tab-btn" :class="{ active: activeTab === 'widgets' }" @click="activeTab = 'widgets'">Widgets</button>
                <button v-if="isAdministrator()" class="tab-btn" :class="{ active: activeTab === 'general' }" @click="activeTab = 'general'">General</button>
                <button v-if="isAdministrator()" class="tab-btn" :class="{ active: activeTab === 'access' }" @click="activeTab = 'access'">Access</button>
                <button v-if="isAdministrator()" class="tab-btn" :class="{ active: activeTab === 'sso' }" @click="activeTab = 'sso'">SSO</button>
                <button v-if="isAdministrator()" class="tab-btn" :class="{ active: activeTab === 'ai' }" @click="activeTab = 'ai'">AI</button>
                <button class="tab-btn" :class="{ active: activeTab === 'db_sql' }" @click="activeTab = 'db_sql'">DB SQL</button>
            </div>

            <!-- ── Data Sources Tab ─────────────────────────────────────── -->
            <div class="settings-content" v-if="activeTab === 'data_sources'">
                <div class="settings-main-area">

                    <!-- Left Column -->
                    <div class="settings-left-col">

                        <!-- Auto Refresh -->
                        <div class="settings-section form-group-row align-start">
                            <label class="section-label">Automatic Refresh Mode</label>
                            <div class="radio-group">
                                <label><input type="radio" v-model="settings.auto_refresh_mode" value="disabled"> Disabled</label>
                                <label><input type="radio" v-model="settings.auto_refresh_mode" value="basic"> Basic</label>
                                <label><input type="radio" v-model="settings.auto_refresh_mode" value="cron"> Cron</label>
                            </div>
                        </div>

                        <div class="settings-section form-group-row" v-if="settings.auto_refresh_mode === 'basic'">
                            <label class="section-label">Refresh interval (minutes)</label>
                            <input type="number" class="settings-input" v-model="settings.basic_min" min="1" />
                        </div>

                        <div class="settings-section form-group-row align-start" v-if="settings.auto_refresh_mode === 'cron'">
                            <label class="section-label">Cron expressions</label>
                            <textarea class="settings-textarea" v-model="settings.cron_str" rows="4" placeholder="*/15 * * * 1-5"></textarea>
                        </div>

                        <div class="settings-actions">
                            <button class="btn primary" @click="saveSettings">Save Settings</button>
                        </div>

                        <!-- ── Register New Data Source (Accordions) ──── -->
                        <div class="ds-section-title">
                            <span>Register a New Data Source</span>
                            <span class="ds-section-sub">Expand a connector type to fill in the required fields</span>
                        </div>

                        <div class="accordions-container">
                            <div
                                v-for="type in connectorTypes"
                                :key="type"
                                class="accordion"
                                :class="{ 'accordion-open': expandedAccordion === type }"
                            >
                                <!-- Header -->
                                <div class="accordion-header" @click="toggleAccordion(type)" :id="'accordion-' + type">
                                    <span class="accordion-title">
                                        Add {{ getConnectorLabel(type) }} Data Source
                                    </span>
                                    <div class="accordion-icon-wrap">
                                        <i class="fa-solid" :class="expandedAccordion === type ? 'fa-minus' : 'fa-plus'"></i>
                                    </div>
                                </div>

                                <!-- Body -->
                                <div class="accordion-body" v-if="expandedAccordion === type">
                                    <div class="form-grid" v-if="forms[type]">
                                        <div class="form-row" v-for="field in CONNECTOR_FIELDS[type]" :key="field.key">
                                            <div class="form-label-wrap">
                                                <label :for="'field-' + type + '-' + field.key">
                                                    {{ field.label }}
                                                </label>
                                            </div>
                                            <div class="field-wrap">
                                                <textarea
                                                    v-if="field.type === 'textarea'"
                                                    :id="'field-' + type + '-' + field.key"
                                                    class="settings-input settings-textarea"
                                                    v-model="forms[type][field.key]"
                                                    rows="3"
                                                    :class="{ 'field-error': formErrors[type] && formErrors[type][field.key] }"
                                                ></textarea>
                                                <input
                                                    v-else-if="field.type === 'checkbox'"
                                                    :id="'field-' + type + '-' + field.key"
                                                    type="checkbox"
                                                    class="settings-checkbox"
                                                    v-model="forms[type][field.key]"
                                                />
                                                <input
                                                    v-else
                                                    :id="'field-' + type + '-' + field.key"
                                                    type="text"
                                                    class="settings-input"
                                                    v-model="forms[type][field.key]"
                                                    :placeholder="field.hint"
                                                    :class="{ 'field-error': formErrors[type] && formErrors[type][field.key] }"
                                                />
                                                <span class="field-error-msg" v-if="formErrors[type] && formErrors[type][field.key]">
                                                    {{ formErrors[type][field.key] }}
                                                </span>
                                            </div>
                                        </div>
                                    </div>

                                    <div class="accordion-actions">
                                        <button
                                            class="btn btn-submit-blue"
                                            :disabled="submitting[type]"
                                            @click="addDataSource(type)"
                                            :id="'btn-submit-' + type"
                                        >
                                            <i class="fa-solid fa-circle-notch fa-spin" v-if="submitting[type]"></i>
                                            <span v-else>Submit</span>
                                        </button>
                                    </div>
                                </div>
                            </div>

                        </div><!-- /accordions-container -->

                    </div><!-- /settings-left-col -->

                    <!-- Right Column: Messages Panel -->
                    <div class="settings-right-col">
                        <div class="message-panel">
                            <div class="message-placeholder" v-if="!message.visible">
                                <i class="fa-regular fa-bell message-placeholder-icon"></i>
                                <p class="placeholder-text">API responses and status messages will appear here.</p>
                            </div>

                            <div v-if="message.visible" :class="['message-box', message.type]">
                                <div class="message-box-header">
                                    <i class="fa-solid" :class="{
                                        'fa-circle-check': message.type === 'success',
                                        'fa-circle-xmark': message.type === 'error',
                                        'fa-triangle-exclamation': message.type === 'warning'
                                    }"></i>
                                    <strong>{{ message.type === 'error' ? 'Error' : message.type === 'warning' ? 'Warning' : 'Success' }}</strong>
                                    <button class="message-close-btn" @click="clearMessage" title="Dismiss">
                                        <i class="fa-solid fa-xmark"></i>
                                    </button>
                                </div>
                                <p class="message-text">{{ message.text }}</p>
                                <p class="message-detail" v-if="message.detail">{{ message.detail }}</p>
                            </div>
                        </div>
                    </div>

                </div><!-- /settings-main-area -->

                <!-- ── Registered Data Sources Table (full width) ───── -->
                <div class="ds-table-full-width">
                    <div class="ds-section-title ds-section-title--table">
                        <span>Registered Data Sources</span>
                        <button
                            class="btn btn-sm btn-outline"
                            :disabled="isRefreshing || isLoading"
                            @click="triggerGlobalRefresh"
                            id="btn-global-refresh"
                            title="Trigger a full reload of all data sources"
                        >
                            <i class="fa-solid fa-rotate-right" :class="{ 'fa-spin': isRefreshing }"></i>
                            Refresh All
                        </button>
                    </div>

                    <div class="table-container">
                        <!-- Loading overlay -->
                        <div class="table-loading" v-if="isLoading">
                            <i class="fa-solid fa-circle-notch fa-spin"></i>
                            <span>Loading…</span>
                        </div>

                        <table class="settings-table" v-if="!isLoading">
                            <thead>
                                <tr>
                                    <th>Name</th>
                                    <th>Title</th>
                                    <th>Type</th>
                                    <th>Description</th>
                                    <th>Filepath</th>
                                    <th>Status</th>
                                    <th>Last Updated</th>
                                    <th>Actions</th>
                                </tr>
                            </thead>
                            <tbody>
                                <!-- Empty state -->
                                <tr v-if="dataSources.length === 0">
                                    <td colspan="8" class="table-empty-state">
                                        <span>No data sources registered yet. Use the forms above to add one.</span>
                                    </td>
                                </tr>

                                <tr v-for="ds in dataSources" :key="ds.name">
                                    <td class="ds-name">
                                        <code>{{ ds.name }}</code>
                                    </td>
                                    <td>{{ ds.title }}</td>
                                    <td>
                                        <span class="type-badge">
                                            <i class="fa-solid" :class="getConnectorIcon(ds.source_type)"></i>
                                            {{ getConnectorLabel(ds.source_type) }}
                                        </span>
                                    </td>
                                    <td>{{ ds.description || '—' }}</td>
                                    <td><code>{{ ds.filepath || '—' }}</code></td>
                                    <td>
                                        <span :class="getStatusClass(ds)">{{ getStatusLabel(ds) }}</span>
                                    </td>
                                    <td class="ds-timestamp">{{ formatTimestamp(ds.last_updated) }}</td>
                                    <td class="action-cells">
                                        <button
                                            class="icon-btn delete-btn"
                                            title="Delete this data source"
                                            @click="deleteDataSource(ds.name)"
                                        >
                                            <i class="fa-regular fa-trash-can"></i>
                                        </button>
                                    </td>
                                </tr>
                            </tbody>
                        </table>
                    </div>
                </div><!-- /ds-table-full-width -->
            </div><!-- /settings-content -->

            <!-- ── Dashboards Tab ─────────────────────────────────────── -->
            <div class="settings-content" v-if="activeTab === 'dashboards'">
                <div class="settings-main-area" style="display: block;">
                    <div class="grid-stack dashboards-grid-stack">
                        <!-- Left Panel (Table) -->
                        <div class="grid-stack-item" gs-x="0" gs-y="0" gs-w="8" gs-h="14">
                            <div class="grid-stack-item-content" style="background: var(--bg-surface); border: 1px solid var(--border-color); display: flex; flex-direction: column;">
                                <div style="padding: 16px; border-bottom: 1px solid var(--border-color); display: flex; justify-content: center;">
                                    <button class="btn btn-outline" style="display: flex; align-items: center;" @click="reloadAllDashboards" :disabled="isDashboardsLoading">
                                        <i class="fa-solid fa-circle-notch fa-spin" style="margin-right: 8px;" v-if="isDashboardsLoading"></i>
                                        Reload all dashboard files in ./dashboards
                                    </button>
                                </div>

                                <div class="accordions-container" style="padding: 16px 16px 0 16px;">
                                    <div class="accordion" :class="{ 'accordion-open': isDashboardAccordionOpen }">
                                        <div class="accordion-header" @click="toggleDashboardAccordion">
                                            <span class="accordion-title">Register Dashboard</span>
                                            <div class="accordion-icon-wrap">
                                                <i class="fa-solid" :class="isDashboardAccordionOpen ? 'fa-minus' : 'fa-plus'"></i>
                                            </div>
                                        </div>
                                        <div class="accordion-body" v-if="isDashboardAccordionOpen">
                                            <div class="form-grid">
                                                <div class="form-row">
                                                    <div class="form-label-wrap">
                                                        <label>File Path</label>
                                                    </div>
                                                    <div class="field-wrap">
                                                        <input
                                                            type="text"
                                                            class="settings-input"
                                                            v-model="dashboardFilePath"
                                                        />
                                                    </div>
                                                </div>
                                            </div>
                                            <div class="accordion-actions">
                                                <button
                                                    class="btn btn-submit-blue"
                                                    :disabled="isRegisteringDashboard || !dashboardFilePath.trim()"
                                                    @click="registerDashboard"
                                                >
                                                    <i class="fa-solid fa-circle-notch fa-spin" v-if="isRegisteringDashboard"></i>
                                                    <span v-else>Submit</span>
                                                </button>
                                            </div>
                                        </div>
                                    </div>
                                </div>
                                <div style="padding: 16px; flex: 1; overflow-y: auto;">
                                    <table class="settings-table">
                                        <thead>
                                            <tr>
                                                <th>Id</th>
                                                <th>Title</th>
                                                <th>Description</th>
                                                <th>Filepath</th>
                                                <th>Status</th>
                                                <th></th>
                                            </tr>
                                        </thead>
                                        <tbody>
                                            <tr v-if="dashboardsList.length === 0">
                                                <td colspan="6" class="table-empty-state">
                                                    <span>No dashboards loaded.</span>
                                                </td>
                                            </tr>
                                            <tr v-for="dash in dashboardsList" :key="dash.id">
                                                <td><code>{{ dash.id }}</code></td>
                                                <td>{{ dash.name }}</td>
                                                <td>{{ dash.description || '—' }}</td>
                                                <td><code>{{ dash.filepath || '—' }}</code></td>
                                                <td>
                                                    <span v-if="dash._status === 'error'" style="color: var(--color-danger);">Error</span>
                                                    <span v-else>Loaded</span>
                                                </td>
                                                <td class="action-cells">
                                                    <button class="icon-btn" title="Reload Dashboard" @click="reloadDashboard(dash.id)" :disabled="dash._reloading || dash._deleting || isDashboardsLoading">
                                                        <i class="fa-solid fa-rotate-right" :class="{ 'fa-spin': dash._reloading }"></i>
                                                    </button>
                                                    <button class="icon-btn text-danger" title="Delete Dashboard" @click="deleteDashboard(dash.id)" :disabled="dash._reloading || dash._deleting || isDashboardsLoading">
                                                        <i class="fa-solid fa-trash-can" :class="{ 'fa-spin': dash._deleting }"></i>
                                                    </button>
                                                </td>
                                            </tr>
                                        </tbody>
                                    </table>
                                </div>
                            </div>
                        </div>

                        <!-- Right Panel (Messages) -->
                        <div class="grid-stack-item" gs-x="8" gs-y="0" gs-w="4" gs-h="14">
                            <div class="grid-stack-item-content" style="background: var(--bg-surface); border: 1px solid var(--border-color); border-radius: 20px; padding: 20px; display: flex; align-items: center; justify-content: center; text-align: center;">
                                <div class="message-panel" style="width: 100%; height: 100%; border: none;">
                                    <div class="message-placeholder" v-if="!dashboardMessage.visible">
                                        <i class="fa-regular fa-bell message-placeholder-icon"></i>
                                        <p class="placeholder-text">API responses and status messages will appear here.</p>
                                    </div>
                                    <div v-if="dashboardMessage.visible" :class="['message-box', dashboardMessage.type]" style="text-align: left;">
                                        <div class="message-box-header">
                                            <i class="fa-solid" :class="{
                                                'fa-circle-check': dashboardMessage.type === 'success',
                                                'fa-circle-xmark': dashboardMessage.type === 'error',
                                                'fa-triangle-exclamation': dashboardMessage.type === 'warning'
                                            }"></i>
                                            <strong>{{ dashboardMessage.type === 'error' ? 'Error' : dashboardMessage.type === 'warning' ? 'Warning' : 'Success' }}</strong>
                                            <button class="message-close-btn" @click="clearDashboardMessage" title="Dismiss">
                                                <i class="fa-solid fa-xmark"></i>
                                            </button>
                                        </div>
                                        <p class="message-text">{{ dashboardMessage.text }}</p>
                                        <p class="message-detail" v-if="dashboardMessage.detail">{{ dashboardMessage.detail }}</p>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
            </div><!-- /dashboards-content -->

            <!-- ── Widgets Tab ─────────────────────────────────────── -->
            <div class="settings-content" v-if="activeTab === 'widgets' && isAdministrator()">
                <div class="settings-main-area" style="display: block;">
                    <div class="grid-stack widgets-grid-stack">
                        <!-- Left Panel (Table) -->
                        <div class="grid-stack-item" gs-x="0" gs-y="0" gs-w="8" gs-h="14">
                            <div class="grid-stack-item-content" style="background: var(--bg-surface); border: 1px solid var(--border-color); display: flex; flex-direction: column;">
                                <div style="padding: 16px; border-bottom: 1px solid var(--border-color); display: flex; justify-content: center;">
                                    <button class="btn btn-outline" style="display: flex; align-items: center;" @click="reloadAllWidgetSets" :disabled="isWidgetSetsLoading">
                                        <i class="fa-solid fa-circle-notch fa-spin" style="margin-right: 8px;" v-if="isWidgetSetsLoading"></i>
                                        Reload all widget sets in ./widgets
                                    </button>
                                </div>

                                <div class="accordions-container" style="padding: 16px 16px 0 16px;">
                                    <div class="accordion" :class="{ 'accordion-open': isWidgetAccordionOpen }">
                                        <div class="accordion-header" @click="toggleWidgetAccordion">
                                            <span class="accordion-title">Register Widgets</span>
                                            <div class="accordion-icon-wrap">
                                                <i class="fa-solid" :class="isWidgetAccordionOpen ? 'fa-minus' : 'fa-plus'"></i>
                                            </div>
                                        </div>
                                        <div class="accordion-body" v-if="isWidgetAccordionOpen">
                                            <div class="form-grid">
                                                <div class="form-row">
                                                    <div class="form-label-wrap">
                                                        <label>Folder Path</label>
                                                    </div>
                                                    <div class="field-wrap">
                                                        <input
                                                            type="text"
                                                            class="settings-input"
                                                            v-model="widgetFolderPath"
                                                        />
                                                    </div>
                                                </div>
                                            </div>
                                            <div class="accordion-actions">
                                                <button
                                                    class="btn btn-submit-blue"
                                                    :disabled="isRegisteringWidgetSet || !widgetFolderPath.trim()"
                                                    @click="registerWidgetSet"
                                                >
                                                    <i class="fa-solid fa-circle-notch fa-spin" v-if="isRegisteringWidgetSet"></i>
                                                    <span v-else>Submit</span>
                                                </button>
                                            </div>
                                        </div>
                                    </div>
                                </div>
                                <div style="padding: 16px; flex: 1; overflow-y: auto;">
                                    <table class="settings-table">
                                        <thead>
                                            <tr>
                                                <th>Id</th>
                                                <th>Title</th>
                                                <th>Description</th>
                                                <th>Folder Path</th>
                                                <th>Status</th>
                                                <th></th>
                                            </tr>
                                        </thead>
                                        <tbody>
                                            <tr v-if="widgetSetsList.length === 0">
                                                <td colspan="6" class="table-empty-state">
                                                    <span>No widget sets loaded.</span>
                                                </td>
                                            </tr>
                                            <tr v-for="ws in widgetSetsList" :key="ws.id">
                                                <td><code>{{ ws.id }}</code></td>
                                                <td>{{ ws.title }}</td>
                                                <td>{{ ws.description || '—' }}</td>
                                                <td><code>{{ ws.folder_path || '—' }}</code></td>
                                                <td>
                                                    <span v-if="ws._status === 'error'" style="color: var(--danger);">Error</span>
                                                    <span v-else>Loaded</span>
                                                    <span v-if="ws.active" class="status-badge status-ok" style="margin-left: 6px;">Active</span>
                                                </td>
                                                <td class="action-cells">
                                                    <button class="icon-btn" :title="ws.active ? 'Currently in use' : 'Set as active — dashboards will use this widget style'" @click="activateWidgetSet(ws.id)" :disabled="ws.active || ws._activating || ws._reloading || ws._deleting || isWidgetSetsLoading">
                                                        <i class="fa-solid" :class="[ws.active ? 'fa-toggle-on' : 'fa-toggle-off', { 'fa-spin': ws._activating }]" :style="ws.active ? { color: 'var(--success)' } : {}"></i>
                                                    </button>
                                                    <button class="icon-btn" title="Reload Widget Set" @click="reloadWidgetSet(ws.id)" :disabled="ws._reloading || ws._deleting || isWidgetSetsLoading">
                                                        <i class="fa-solid fa-rotate-right" :class="{ 'fa-spin': ws._reloading }"></i>
                                                    </button>
                                                    <button class="icon-btn text-danger" :title="ws.active ? 'Activate a different widget set before deleting this one' : 'Delete Widget Set'" @click="deleteWidgetSet(ws.id)" :disabled="ws.active || ws._reloading || ws._deleting || isWidgetSetsLoading">
                                                        <i class="fa-solid fa-trash-can" :class="{ 'fa-spin': ws._deleting }"></i>
                                                    </button>
                                                </td>
                                            </tr>
                                        </tbody>
                                    </table>
                                </div>
                            </div>
                        </div>

                        <!-- Right Panel (Messages) -->
                        <div class="grid-stack-item" gs-x="8" gs-y="0" gs-w="4" gs-h="14">
                            <div class="grid-stack-item-content" style="background: var(--bg-surface); border: 1px solid var(--border-color); border-radius: 20px; padding: 20px; display: flex; align-items: center; justify-content: center; text-align: center;">
                                <div class="message-panel" style="width: 100%; height: 100%; border: none;">
                                    <div class="message-placeholder" v-if="!widgetMessage.visible">
                                        <i class="fa-regular fa-bell message-placeholder-icon"></i>
                                        <p class="placeholder-text">API responses and status messages will appear here.</p>
                                    </div>
                                    <div v-if="widgetMessage.visible" :class="['message-box', widgetMessage.type]" style="text-align: left;">
                                        <div class="message-box-header">
                                            <i class="fa-solid" :class="{
                                                'fa-circle-check': widgetMessage.type === 'success',
                                                'fa-circle-xmark': widgetMessage.type === 'error',
                                                'fa-triangle-exclamation': widgetMessage.type === 'warning'
                                            }"></i>
                                            <strong>{{ widgetMessage.type === 'error' ? 'Error' : widgetMessage.type === 'warning' ? 'Warning' : 'Success' }}</strong>
                                            <button class="message-close-btn" @click="clearWidgetMessage" title="Dismiss">
                                                <i class="fa-solid fa-xmark"></i>
                                            </button>
                                        </div>
                                        <p class="message-text">{{ widgetMessage.text }}</p>
                                        <p class="message-detail" v-if="widgetMessage.detail">{{ widgetMessage.detail }}</p>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
            </div><!-- /widgets-content -->

            <!-- ── General Tab ─────────────────────────────────────── -->
            <div class="settings-content" v-if="activeTab === 'general' && isAdministrator()">
                <div class="settings-main-area" style="display: block;">
                    
                    <div style="margin-bottom: 20px; padding: 0 20px; padding-top: 10px; display: flex; align-items: center;">
                        <span style="margin-right: 20px; font-weight: 500;">UI Theme</span>
                        <label style="margin-right: 15px; display: flex; align-items: center; gap: 5px;">
                            <input type="radio" v-model="uiTheme" value="light" @change="changeTheme"> Light
                        </label>
                        <label style="display: flex; align-items: center; gap: 5px;">
                            <input type="radio" v-model="uiTheme" value="dark" @change="changeTheme"> Dark
                        </label>
                    </div>

                    <div style="margin-bottom: 20px; padding: 0 20px;">
                        <button class="btn btn-submit-blue" @click="resetRedisCache" :disabled="isResettingCache">
                            <i class="fa-solid fa-circle-notch fa-spin" v-if="isResettingCache"></i>
                            <span v-else>Redis Cache Reset</span>
                        </button>
                    </div>

                    <div class="grid-stack general-grid-stack">
                        <!-- Redis Configuration -->
                        <div class="grid-stack-item" gs-x="0" gs-y="0" gs-w="7" gs-h="7">
                            <div class="grid-stack-item-content" style="background: var(--bg-surface); border: 1px solid var(--border-color); display: flex; flex-direction: column;">
                                <div style="padding: 12px 16px; border-bottom: 1px solid var(--border-color); font-weight: 500;">Redis Configuration</div>
                                <div style="padding: 16px; flex: 1; display: flex; flex-direction: column; gap: 15px;">
                                    
                                    <div style="display: flex; gap: 15px; align-items: center;">
                                        <div style="flex: 1; display: flex; align-items: center; gap: 10px;">
                                            <span style="width: 80px; color: var(--text-secondary);">Host</span>
                                            <input type="text" class="settings-input" v-model="settings.redis_host" style="flex: 1;" placeholder="redis">
                                        </div>
                                        <div style="width: 150px; display: flex; align-items: center; gap: 10px;">
                                            <span style="color: var(--text-secondary);">Port</span>
                                            <input type="number" class="settings-input" v-model="settings.redis_port" style="flex: 1;" placeholder="6379">
                                        </div>
                                    </div>

                                    <div style="display: flex; align-items: center; gap: 10px;">
                                        <span style="width: 80px; color: var(--text-secondary);">User</span>
                                        <input type="text" class="settings-input" v-model="settings.redis_user" style="flex: 1;" >
                                    </div>

                                    <div style="display: flex; align-items: center; gap: 10px;">
                                        <span style="width: 80px; color: var(--text-secondary);">Password</span>
                                        <input type="password" class="settings-input" v-model="settings.redis_password" style="flex: 1;" :placeholder="settings.redis_password_set ? 'Password is set — leave blank to keep it' : 'No password set'">
                                    </div>

                                    <div style="display: flex; align-items: center; gap: 10px;">
                                        <span style="width: 80px; color: var(--text-secondary);">Row Limit</span>
                                        <input type="number" class="settings-input" v-model="settings.row_limit" style="flex: 1;" placeholder="100">
                                    </div>

                                    <div style="display: flex; align-items: center; gap: 10px;">
                                        <span style="width: 80px; color: var(--text-secondary);">TTL seconds</span>
                                        <input type="number" class="settings-input" v-model="settings.redis_ttl_seconds" style="flex: 1;">
                                    </div>

                                    <div style="display: flex; align-items: center; gap: 10px;">
                                        <span style="width: 80px; color: var(--text-secondary);">TLS</span>
                                        <label style="display: flex; align-items: center; gap: 5px; margin-right: 10px;">
                                            <input type="radio" v-model="settings.redis_tls_enabled" value="enabled"> Enabled
                                        </label>
                                        <label style="display: flex; align-items: center; gap: 5px;">
                                            <input type="radio" v-model="settings.redis_tls_enabled" value="disabled"> Disabled
                                        </label>
                                    </div>
                                    
                                    <div style="display: flex; justify-content: flex-end; margin-top: auto;">
                                        <button class="btn btn-submit-blue" @click="saveRedisSettings" :disabled="isSavingRedis">
                                            <i class="fa-solid fa-circle-notch fa-spin" v-if="isSavingRedis"></i>
                                            <span v-else>Submit</span>
                                        </button>
                                    </div>
                                </div>
                            </div>
                        </div>

                        <!-- Syslog Export -->
                        <div class="grid-stack-item" gs-x="0" gs-y="7" gs-w="7" gs-h="7">
                            <div class="grid-stack-item-content" style="background: var(--bg-surface); border: 1px solid var(--border-color); display: flex; flex-direction: column;">
                                <div style="padding: 12px 16px; border-bottom: 1px solid var(--border-color); font-weight: 500;">Syslog Export</div>
                                <div style="padding: 16px; flex: 1; display: flex; flex-direction: column; gap: 15px;">
                                    
                                    <div style="display: flex; align-items: center; gap: 10px;">
                                        <span style="width: 80px; color: var(--text-secondary);">Export</span>
                                        <label style="display: flex; align-items: center; gap: 5px; margin-right: 10px;">
                                            <input type="radio" v-model="settings.syslog_enabled" value="enabled"> Enabled
                                        </label>
                                        <label style="display: flex; align-items: center; gap: 5px;">
                                            <input type="radio" v-model="settings.syslog_enabled" value="disabled"> Disabled
                                        </label>
                                    </div>

                                    <div style="display: flex; gap: 15px; align-items: center;">
                                        <div style="flex: 1; display: flex; align-items: center; gap: 10px;">
                                            <span style="width: 80px; color: var(--text-secondary);">Host</span>
                                            <input type="text" class="settings-input" v-model="settings.syslog_host" style="flex: 1;">
                                        </div>
                                        <div style="width: 150px; display: flex; align-items: center; gap: 10px;">
                                            <span style="color: var(--text-secondary);">Port</span>
                                            <input type="number" class="settings-input" v-model="settings.syslog_port" style="flex: 1;">
                                        </div>
                                    </div>

                                    <div style="display: flex; align-items: center; gap: 10px;">
                                        <span style="width: 80px; color: var(--text-secondary);">TLS</span>
                                        <label style="display: flex; align-items: center; gap: 5px; margin-right: 10px;">
                                            <input type="radio" v-model="settings.syslog_tls_enabled" value="enabled"> Enabled
                                        </label>
                                        <label style="display: flex; align-items: center; gap: 5px;">
                                            <input type="radio" v-model="settings.syslog_tls_enabled" value="disabled"> Disabled
                                        </label>
                                    </div>

                                    <div style="display: flex; align-items: center; gap: 10px;">
                                        <span style="width: 80px; color: var(--text-secondary);">Cert Path</span>
                                        <input type="text" class="settings-input" v-model="settings.syslog_cert_path" style="flex: 1;">
                                    </div>

                                    <div style="display: flex; align-items: center; gap: 10px;">
                                        <span style="width: 80px; color: var(--text-secondary);">Key Path</span>
                                        <input type="text" class="settings-input" v-model="settings.syslog_key_path" style="flex: 1;">
                                    </div>

                                    <div style="display: flex; align-items: center; gap: 10px;">
                                        <span style="width: 80px; color: var(--text-secondary);">CA Cert Path</span>
                                        <input type="text" class="settings-input" v-model="settings.syslog_ca_cert_path" style="flex: 1;">
                                    </div>
                                    
                                    <div style="display: flex; justify-content: flex-end; margin-top: auto;">
                                        <button class="btn btn-submit-blue" @click="saveSyslogSettings" :disabled="isSavingSyslog">
                                            <i class="fa-solid fa-circle-notch fa-spin" v-if="isSavingSyslog"></i>
                                            <span v-else>Submit</span>
                                        </button>
                                    </div>
                                </div>
                            </div>
                        </div>

                        <!-- Messages Panel -->
                        <div class="grid-stack-item" gs-x="7" gs-y="0" gs-w="5" gs-h="14">
                            <div class="grid-stack-item-content" style="background: var(--bg-surface); border: 1px solid var(--border-color); border-radius: 20px; padding: 20px; display: flex; align-items: center; justify-content: center; text-align: center;">
                                <div class="message-panel" style="width: 100%; height: 100%; border: none;">
                                    <div class="message-placeholder" v-if="!message.visible">
                                        <i class="fa-regular fa-bell message-placeholder-icon"></i>
                                        <p class="placeholder-text">API responses and status messages will appear here.</p>
                                    </div>

                                    <div v-if="message.visible" :class="['message-box', message.type]" style="text-align: left;">
                                        <div class="message-box-header">
                                            <i class="fa-solid" :class="{
                                                'fa-circle-check': message.type === 'success',
                                                'fa-circle-xmark': message.type === 'error',
                                                'fa-triangle-exclamation': message.type === 'warning'
                                            }"></i>
                                            <strong>{{ message.type === 'error' ? 'Error' : message.type === 'warning' ? 'Warning' : 'Success' }}</strong>
                                            <button class="message-close-btn" @click="clearMessage" title="Dismiss">
                                                <i class="fa-solid fa-xmark"></i>
                                            </button>
                                        </div>
                                        <p class="message-text">{{ message.text }}</p>
                                        <p class="message-detail" v-if="message.detail">{{ message.detail }}</p>
                                    </div>
                                </div>
                            </div>
                        </div>

                    </div>
                </div>
            </div>

            <!-- ── Access Tab ─────────────────────────────────────── -->
            <div class="settings-content" v-if="activeTab === 'access' && isAdministrator()">
                <div class="settings-main-area" style="display: block;">
                    <div class="grid-stack access-grid-stack" style="margin-top: 10px;">
                        <!-- Left Panel -->
                        <div class="grid-stack-item" gs-x="0" gs-y="0" gs-w="8" gs-h="14">
                            <div class="grid-stack-item-content" style="background: transparent; border: none; display: flex; flex-direction: column; padding-right: 20px;">
                                <div style="display: flex; align-items: center; gap: 20px; margin-bottom: 10px;">
                                    <span style="font-weight: 500; white-space: nowrap;">Allow Anonymous Access</span>
                                    <div class="radio-group">
                                        <label><input type="radio" :checked="accessAnonymousAccess" @change="setAnonymousAccess(true)" :disabled="isSavingAccess" name="anon-access"> On</label>
                                        <label><input type="radio" :checked="!accessAnonymousAccess" @change="setAnonymousAccess(false)" :disabled="isSavingAccess" name="anon-access"> Off</label>
                                    </div>
                                    <span style="color: var(--text-secondary); font-size: 0.85em;">
                                        {{ accessAnonymousAccess ? 'Everyone is treated as Administrator, no sign-in required.' : 'Sign-in via SSO is required.' }}
                                    </span>
                                </div>
                                <div style="color: var(--text-secondary); font-size: 0.85em; margin-bottom: 20px;">
                                    Users authenticated via SSO who match none of the mappings below are
                                    always denied — there is no configurable default role for unmapped users.
                                </div>
                                <!-- Add Mapping Section -->
                                <div style="background: var(--bg-surface); border: 1px solid var(--border-color); border-radius: 4px; display: flex; flex-direction: column; margin-bottom: 20px;">
                                    <div style="padding: 12px 16px; border-bottom: 1px solid var(--border-color); font-weight: 500; text-align: center;">Add Mapping</div>
                                    <div style="padding: 20px 40px; display: flex; flex-direction: column; gap: 15px;">
                                        <div style="display: flex; align-items: center; gap: 20px;">
                                            <span style="width: 150px; color: var(--text-secondary);">OIDC Claim</span>
                                            <input type="text" class="settings-input" v-model="accessNewMapping.claim" style="flex: 1;" placeholder="groups">
                                        </div>
                                        <div style="display: flex; align-items: center; gap: 20px;">
                                            <span style="width: 150px; color: var(--text-secondary);">Claim Value</span>
                                            <input type="text" class="settings-input" v-model="accessNewMapping.value" style="flex: 1;" placeholder="admins">
                                        </div>
                                        <div style="display: flex; align-items: center; gap: 20px;">
                                            <span style="width: 150px; color: var(--text-secondary);">System Role</span>
                                            <select class="settings-input" v-model="accessNewMapping.role" style="flex: 1;">
                                                <option v-for="r in VALID_ROLES" :key="r" :value="r">{{ r }}</option>
                                            </select>
                                        </div>
                                        <div style="display: flex; justify-content: flex-end; margin-top: 10px;">
                                            <button class="btn btn-submit-blue" @click="addAccessMapping" :disabled="isSavingAccess">Submit</button>
                                        </div>
                                    </div>
                                </div>

                                <!-- Mappings Table -->
                                <div style="flex: 1; overflow-y: auto;">
                                    <table class="settings-table" style="background: var(--bg-surface); border: 1px solid var(--border-color); border-radius: 4px; overflow: hidden; margin: 0;">
                                        <thead>
                                            <tr>
                                                <th style="text-align: center; border-right: 1px solid var(--border-color);">Claim</th>
                                                <th style="text-align: center; border-right: 1px solid var(--border-color);">Value</th>
                                                <th style="text-align: center; border-right: 1px solid var(--border-color);">System Role</th>
                                                <th style="width: 50px;"></th>
                                            </tr>
                                        </thead>
                                        <tbody>
                                            <tr v-if="accessMappings.length === 0">
                                                <td colspan="4" class="table-empty-state">
                                                    <span>No mappings configured. Every SSO user is denied until one is added.</span>
                                                </td>
                                            </tr>
                                            <tr v-for="(mapping, index) in accessMappings" :key="index" style="border-bottom: 1px dashed var(--border-color);">
                                                <td style="text-align: center; border-right: 1px solid var(--border-color);">{{ mapping.claim }}</td>
                                                <td style="text-align: center; border-right: 1px solid var(--border-color);">{{ mapping.value }}</td>
                                                <td style="text-align: center; border-right: 1px solid var(--border-color);">{{ mapping.role }}</td>
                                                <td class="action-cells" style="justify-content: center;">
                                                    <button class="icon-btn text-danger" title="Delete" @click="deleteAccessMapping(index)">
                                                        <i class="fa-regular fa-trash-can"></i>
                                                    </button>
                                                </td>
                                            </tr>
                                        </tbody>
                                    </table>
                                </div>
                            </div>
                        </div>

                        <!-- Right Panel (Messages) -->
                        <div class="grid-stack-item" gs-x="8" gs-y="0" gs-w="4" gs-h="14">
                            <div class="grid-stack-item-content" style="background: var(--bg-surface); border: 1px solid var(--border-color); border-radius: 20px; padding: 20px; display: flex; align-items: center; justify-content: center; text-align: center;">
                                <div class="message-panel" style="width: 100%; height: 100%; border: none;">
                                    <div class="message-placeholder" v-if="!accessMessage.visible">
                                        <i class="fa-regular fa-bell message-placeholder-icon"></i>
                                        <p class="placeholder-text">API responses and status messages will appear here.</p>
                                    </div>
                                    <div v-if="accessMessage.visible" :class="['message-box', accessMessage.type]" style="text-align: left;">
                                        <div class="message-box-header">
                                            <i class="fa-solid" :class="{
                                                'fa-circle-check': accessMessage.type === 'success',
                                                'fa-circle-xmark': accessMessage.type === 'error',
                                                'fa-triangle-exclamation': accessMessage.type === 'warning'
                                            }"></i>
                                            <strong>{{ accessMessage.type === 'error' ? 'Error' : accessMessage.type === 'warning' ? 'Warning' : 'Success' }}</strong>
                                            <button class="message-close-btn" @click="clearAccessMessage" title="Dismiss">
                                                <i class="fa-solid fa-xmark"></i>
                                            </button>
                                        </div>
                                        <p class="message-text">{{ accessMessage.text }}</p>
                                        <p class="message-detail" v-if="accessMessage.detail">{{ accessMessage.detail }}</p>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
            </div><!-- /access-content -->

            <!-- ── SSO Tab ─────────────────────────────────────── -->
            <div class="settings-content" v-if="activeTab === 'sso' && isAdministrator()">
                <div class="settings-main-area" style="display: block;">
                    <div class="grid-stack sso-grid-stack" style="margin-top: 10px;">
                        <!-- Left Panel -->
                        <div class="grid-stack-item" gs-x="0" gs-y="0" gs-w="8" gs-h="18">
                            <div class="grid-stack-item-content" style="background: transparent; border: none; display: flex; flex-direction: column; padding-right: 20px;">
                                <div style="display: flex; flex-direction: column; gap: 20px; max-width: 600px;">

                                    <div style="color: var(--text-secondary); font-size: 0.85em;">
                                        Generic OIDC connection — works with Keycloak, Entra ID, Okta, or any
                                        standards-compliant provider. The backend performs the login exchange
                                        itself; the browser never handles a token.
                                    </div>

                                    <!-- Issuer URL -->
                                    <div style="display: flex; align-items: center; gap: 20px;">
                                        <span style="width: 150px; color: var(--text-secondary);">Issuer URL</span>
                                        <input type="text" class="settings-input" v-model="ssoSettings.issuerUrl" style="flex: 1;" placeholder="https://keycloak.example.com/realms/buchimaker">
                                    </div>

                                    <!-- Client ID -->
                                    <div style="display: flex; align-items: center; gap: 20px;">
                                        <span style="width: 150px; color: var(--text-secondary);">Client ID</span>
                                        <input type="text" class="settings-input" v-model="ssoSettings.clientId" style="flex: 1;" placeholder="buchimaker">
                                    </div>

                                    <!-- Client Secret -->
                                    <div style="display: flex; align-items: center; gap: 20px;">
                                        <span style="width: 150px; color: var(--text-secondary);">Client Secret</span>
                                        <input type="password" class="settings-input" v-model="ssoSettings.clientSecret" style="flex: 1;"
                                               :placeholder="ssoSettings.clientSecretSet ? 'Configured — leave blank to keep it' : '****'">
                                    </div>

                                    <!-- Scopes -->
                                    <div style="display: flex; align-items: center; gap: 20px;">
                                        <span style="width: 150px; color: var(--text-secondary);">Scopes</span>
                                        <input type="text" class="settings-input" v-model="ssoSettings.scopes" style="flex: 1;" placeholder="openid profile email">
                                    </div>

                                    <!-- Redirect URL -->
                                    <div style="display: flex; align-items: center; gap: 20px;">
                                        <span style="width: 150px; color: var(--text-secondary);">Redirect URL</span>
                                        <input type="text" class="settings-input" v-model="ssoSettings.redirectUrl" style="flex: 1;" :placeholder="suggestedRedirectUrl">
                                    </div>
                                    <div style="color: var(--text-secondary); font-size: 0.8em; margin-top: -12px;">
                                        Must exactly match the redirect URI registered on the identity
                                        provider's client. Suggested: <code>{{ suggestedRedirectUrl }}</code>
                                    </div>

                                    <div style="display: flex; justify-content: center; gap: 15px; margin-top: 5px;">
                                        <button class="btn btn-outline" @click="testSsoConnection" :disabled="isTestingSso">
                                            {{ isTestingSso ? 'Testing…' : 'Test' }}
                                        </button>
                                        <button class="btn btn-submit-blue" @click="saveSsoSettings" :disabled="isSavingSso">
                                            {{ isSavingSso ? 'Saving…' : 'Apply' }}
                                        </button>
                                        <button class="btn btn-outline" @click="trySsoSignIn" :disabled="!ssoSettings.issuerUrl || !ssoSettings.clientId"
                                                title="Opens a real login round trip in a new tab — works regardless of the Allow Anonymous Access setting.">
                                            Sign In (test)
                                        </button>
                                    </div>
                                </div>
                            </div>
                        </div>

                        <!-- Right Panel (Messages) -->
                        <div class="grid-stack-item" gs-x="8" gs-y="0" gs-w="4" gs-h="18">
                            <div class="grid-stack-item-content" style="background: var(--bg-surface); border: 1px solid var(--border-color); border-radius: 20px; padding: 20px; display: flex; align-items: center; justify-content: center; text-align: center;">
                                <div class="message-panel" style="width: 100%; height: 100%; border: none;">
                                    <div class="message-placeholder" v-if="!ssoMessage.visible">
                                        <i class="fa-regular fa-bell message-placeholder-icon"></i>
                                        <p class="placeholder-text">API responses and status messages will appear here.</p>
                                    </div>
                                    <div v-if="ssoMessage.visible" :class="['message-box', ssoMessage.type]" style="text-align: left;">
                                        <div class="message-box-header">
                                            <i class="fa-solid" :class="{
                                                'fa-circle-check': ssoMessage.type === 'success',
                                                'fa-circle-xmark': ssoMessage.type === 'error',
                                                'fa-triangle-exclamation': ssoMessage.type === 'warning'
                                            }"></i>
                                            <strong>{{ ssoMessage.type === 'error' ? 'Error' : ssoMessage.type === 'warning' ? 'Warning' : 'Success' }}</strong>
                                            <button class="message-close-btn" @click="clearSsoMessage" title="Dismiss">
                                                <i class="fa-solid fa-xmark"></i>
                                            </button>
                                        </div>
                                        <p class="message-text">{{ ssoMessage.text }}</p>
                                        <p class="message-detail" v-if="ssoMessage.detail">{{ ssoMessage.detail }}</p>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
            </div><!-- /sso-content -->

            <!-- ── AI Tab ─────────────────────────────────────── -->
            <div class="settings-content" v-if="activeTab === 'ai' && isAdministrator()">
                <div class="settings-main-area" style="display: block;">
                    <div class="grid-stack ai-grid-stack">
                        <!-- Left Panel -->
                        <div class="grid-stack-item" gs-x="0" gs-y="0" gs-w="8" gs-h="14">
                            <div class="grid-stack-item-content" style="background: transparent; border: none; display: flex; flex-direction: column; padding-right: 20px; margin-top: 10px;">
                                <div style="display: flex; flex-direction: column; gap: 20px; max-width: 600px;">
                                    <div style="display: flex; align-items: center; gap: 20px; border-bottom: 1px solid var(--border-color); padding-bottom: 20px;">
                                        <span style="width: 150px; color: var(--text-secondary);">Provider</span>
                                        <select class="settings-input" v-model="aiProvider" style="flex: 1;">
                                            <option value="Gemini">Gemini</option>
                                            <option value="ChatGPT">ChatGPT</option>
                                            <option value="Claude">Claude</option>
                                            <option value="ollama">ollama</option>
                                            <option value="vllm">vllm</option>
                                        </select>
                                    </div>

                                    <!-- If ollama or vllm -->
                                    <template v-if="aiProvider === 'ollama' || aiProvider === 'vllm'">
                                        <div style="display: flex; align-items: center; gap: 20px;">
                                            <span style="width: 150px; color: var(--text-secondary);">Base URL</span>
                                            <input type="text" class="settings-input" v-model="aiSettings.baseUrl" style="flex: 1;" placeholder="****">
                                        </div>
                                        <div style="display: flex; align-items: center; gap: 20px;">
                                            <span style="width: 150px; color: var(--text-secondary);">Model</span>
                                            <input type="text" class="settings-input" v-model="aiSettings.model" style="flex: 1;" placeholder="ab2312-23ns-...">
                                        </div>
                                        <div style="display: flex; align-items: center; gap: 20px; border-bottom: 1px solid var(--border-color); padding-bottom: 20px;">
                                            <span style="width: 150px; color: var(--text-secondary);">API Key</span>
                                            <input type="password" class="settings-input" v-model="aiSettings.apiKey" style="flex: 1;" placeholder="****">
                                        </div>
                                    </template>

                                    <!-- Otherwise -->
                                    <template v-else>
                                        <div style="display: flex; align-items: center; gap: 20px;">
                                            <span style="width: 150px; color: var(--text-secondary);">API Key</span>
                                            <input type="password" class="settings-input" v-model="aiSettings.apiKey" style="flex: 1;" placeholder="****">
                                        </div>
                                        <div style="display: flex; align-items: center; gap: 20px;">
                                            <span style="width: 150px; color: var(--text-secondary);">Model</span>
                                            <input type="text" class="settings-input" v-model="aiSettings.model" style="flex: 1;" placeholder="ab2312-23ns-...">
                                        </div>
                                        <div style="display: flex; align-items: center; gap: 20px; border-bottom: 1px solid var(--border-color); padding-bottom: 20px;">
                                            <span style="width: 150px; color: var(--text-secondary);">Organization / Project ID</span>
                                            <input type="text" class="settings-input" v-model="aiSettings.organizationId" style="flex: 1;" placeholder="ab2312-23ns-...">
                                        </div>
                                    </template>

                                    <div style="display: flex; justify-content: center; gap: 15px; margin-top: 10px;">
                                        <button class="btn btn-submit-blue">Test</button>
                                        <button class="btn btn-submit-blue">Apply</button>
                                    </div>
                                </div>
                            </div>
                        </div>

                        <!-- Right Panel (Messages) -->
                        <div class="grid-stack-item" gs-x="8" gs-y="0" gs-w="4" gs-h="14">
                            <div class="grid-stack-item-content" style="background: var(--bg-surface); border: 1px solid var(--border-color); border-radius: 20px; padding: 20px; display: flex; align-items: center; justify-content: center; text-align: center;">
                                <div class="message-panel" style="width: 100%; height: 100%; border: none;">
                                    <div class="message-placeholder" v-if="!aiMessage.visible">
                                        <i class="fa-regular fa-bell message-placeholder-icon"></i>
                                        <p class="placeholder-text">API responses and status messages will appear here.</p>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
            </div><!-- /ai-content -->

            <!-- ── DB SQL Tab ─────────────────────────────────────── -->
            <div class="settings-content" v-if="activeTab === 'db_sql'">
                <div class="settings-main-area" style="display: block;">
                    <p style="margin-bottom: 20px; color: var(--text-muted); padding: 0 20px; padding-top: 10px;">This tab allows to test SQL queries. Only SELECT queries are allowed.</p>
                    <div class="grid-stack sql-grid-stack">
                        <div class="grid-stack-item" gs-x="0" gs-y="0" gs-w="12" gs-h="5">
                            <div class="grid-stack-item-content" style="background: var(--bg-surface); border: 1px solid var(--border-color); border-radius: 4px; padding: 16px; display: flex; flex-direction: column;">
                                <label style="margin-bottom: 8px; font-weight: 500; font-size: 0.9rem; color: var(--text-secondary);">SQL Query</label>
                                <textarea v-model="sqlQuery" class="settings-textarea" style="flex: 1; resize: none; font-family: monospace;"></textarea>
                                <div style="display: flex; justify-content: flex-end; margin-top: 12px;">
                                    <button class="btn-submit-blue" @click="runSql" :disabled="isSqlLoading">
                                        <i class="fa-solid fa-circle-notch fa-spin" v-if="isSqlLoading"></i>
                                        <span v-else>Submit</span>
                                    </button>
                                </div>
                            </div>
                        </div>
                        <div class="grid-stack-item" gs-x="0" gs-y="5" gs-w="12" gs-h="8">
                            <div class="grid-stack-item-content" style="background: var(--bg-surface); border: 1px solid var(--border-color); border-radius: 4px; padding: 16px; display: flex; flex-direction: column; position: relative;">
                                <button v-if="sqlResults" @click="copySqlResults" class="icon-btn" title="Copy to clipboard" style="position: absolute; top: 22px; right: 26px; z-index: 10; background: var(--bg-surface); border: 1px solid var(--border-color); border-radius: 4px; padding: 4px 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1);">
                                    <i class="fa-regular fa-copy"></i>
                                </button>
                                <pre style="flex: 1; margin: 0; padding: 12px; font-family: monospace; font-size: 0.85rem; white-space: pre; overflow: auto; background-color: var(--bg-main); border: 1px solid var(--border-color); border-radius: 4px; color: var(--text-primary); box-sizing: border-box;">{{ sqlResults }}</pre>
                            </div>
                        </div>
                    </div>
                </div>
            </div><!-- /db-sql-content -->

        </div>
    `
};
