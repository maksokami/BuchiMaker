// Shown in place of the dashboard shell when the caller's resolved role is
// "Deny" — either no OIDC mapping matched their claims, or an admin mapped
// them to Deny explicitly (see Settings > Access, backend/app/core/roles.py).
export default {
    props: {
        email: { type: String, default: null },
    },
    template: `
        <div class="settings-page" style="display:flex; align-items:center; justify-content:center; height:100%; text-align:center;">
            <div>
                <i class="fa-solid fa-ban" style="font-size:2.5rem; color:var(--danger); margin-bottom:16px;"></i>
                <h1 style="margin:0 0 8px;">Access Denied</h1>
                <p style="color:var(--text-secondary); max-width:420px; margin:0 auto;">
                    <span v-if="email">{{ email }} is</span><span v-else>Your account is</span>
                    not authorized to use this application. Contact your administrator to be
                    added to an access group.
                </p>
            </div>
        </div>
    `,
};
