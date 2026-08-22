#!/bin/sh
set -e

# Provide defaults if not set — relative paths so the frontend talks to the
# backend through nginx's own /api/ and /auth/ proxy (see nginx.conf),
# keeping frontend and backend same-origin for the SSO session cookie.
export API_BASE_URL=${API_BASE_URL:-/api/v1}
export AUTH_BASE_URL=${AUTH_BASE_URL:-/auth}

envsubst < /usr/share/nginx/html/config.js.template > /usr/share/nginx/html/config.js

exec "$@"
