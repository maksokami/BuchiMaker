# Keycloak OIDC Integration & Testing Guide

A step-by-step guide to setting up a local Keycloak instance, configuring realms and clients, provisioning test users and groups, and injecting custom group claims into OIDC tokens.

---

## 1. Quick Start with Docker

`host.docker.internal` needs to resolve to Keycloak from **two different
places** — your browser (running on the host) and the `buchi-backend`
container (running on the `buchi-net` Docker network) — and they need to
reach it by two different paths. Getting this wrong looks like:
`Could not fetch OIDC discovery document ... All connection attempts failed`
or a connection that just times out.

**For your browser:** make `host.docker.internal` resolve to your own machine:
```sh
echo "127.0.0.1 host.docker.internal" | sudo tee -a /etc/hosts
```

**For the backend container:** join Keycloak directly onto BuchiMaker's
own Docker network with a `host.docker.internal` alias, so backend↔Keycloak
traffic stays on one bridge network instead of hopping through the host's
default `docker0` gateway (which reliably works from the host itself, but
can silently time out from a *different* bridge network depending on your
firewall — that's the trap; don't use `--add-host`/`extra_hosts` pointing
at the gateway IP, it's the wrong fix here).

Run Keycloak in development mode (`start-dev` disables mandatory HTTPS and
uses an embedded H2 database; the `keycloak_data` volume persists your
realm/client/users across container recreations):

```bash
docker rm -f keycloak-test

docker run -d --name keycloak-test \
  -p 8080:8080 \
  -e KEYCLOAK_ADMIN=admin \
  -e KEYCLOAK_ADMIN_PASSWORD=admin \
  -e KC_HOSTNAME=host.docker.internal \
  -v keycloak_data:/opt/keycloak/data \
  quay.io/keycloak/keycloak:latest start-dev

# Docker Compose prefixes network names with the project directory name —
# check the real name with `docker network ls` if this doesn't match.
docker network connect llm-databoom-v03_buchi-net keycloak-test --alias host.docker.internal
```

Verify the backend can actually reach it before touching the BuchiMaker UI:
```sh
docker exec buchi-backend python3 -c "
import urllib.request
print(urllib.request.urlopen('http://host.docker.internal:8080/realms/master/.well-known/openid-configuration', timeout=5).status)
"
```
Should print `200`. If it hangs/times out, `docker exec buchi-backend getent hosts host.docker.internal`
should show Keycloak's IP on `buchi-net` (e.g. `172.21.0.x`) — if it shows
`172.17.0.1` (the default-bridge gateway) instead, something is still
statically overriding it (check `docker inspect buchi-backend --format
'{{.HostConfig.ExtraHosts}}'` — it should be empty).

* **Admin Console URL:** `http://localhost:8080`
* **Username:** `admin`
* **Password:** `admin`

---

## 2. Realm & OIDC Client Setup

### Step 2.1: Create Realm
1. Open `http://localhost:8080` and log in to the **Admin Console**.
2. Click the realm dropdown in the top-left corner (defaults to `master`).
3. Click **Create Realm**.
4. Set **Realm name**: `test`.
5. Click **Create**.

### Step 2.2: Create Client
1. In `test`, navigate to **Clients** > **Create client**.
2. Configure **General Settings**:
   * **Client type:** `OpenID Connect`
   * **Client ID:** `buchimaker`
   * Click **Next**.
3. Configure **Capability config**:
   * **Client authentication:** `On`
   * **Authentication flow:** Check **Standard flow** (Authorization Code Flow).
   * Click **Next**.
4. Configure **Login settings**:
   * **Valid redirect URIs:** `http://localhost:3000/*` (or your application callback URL).
   * **Web origins:** `*` or `http://localhost:3000`.
   * Click **Save**.
5. *(If Client authentication is ON)* Go to the **Credentials** tab and copy the **Client Secret**.

---

## 3. Configure Groups & Users

### Step 3.1: Create Groups
1. Navigate to **Groups** > **Create group**.
2. Create two groups:
   * Group 1: `buchi-admins`
   * Group 2: `buchi-data-admins`
   * Group 3: `buchi-users`

### Step 3.2: Create Users & Set Passwords
1. Navigate to **Users** > **Add user**.
2. Create four users:
   * `user-adm`. Add to group `buchi-admins`
   * `user-data`. Add to group `buchi-data-admins`
   * `user-regular`. Add to group `buchi-users`
   * `user-test` (no group)
3. For **each user**:
   * Open the user record and click the **Credentials** tab.
   * Click **Set password**.
   * Enter a password and toggle **Temporary** to **Off**.
   * Click **Save**.
---

## 4. Map Group Membership Claim to Tokens

By default, Keycloak does not include group memberships inside ID or Access tokens. Follow these steps to map groups to the token payload:

1. Navigate to **Clients** > click `buchimaker` > **Client scopes** tab.
2. Click the dedicated scope link: `buchimaker-dedicated`.
3. Click **Add mapper** > **By configuration**.
4. Select **Group Membership**.
5. Configure the mapper parameters:
   * **Name:** `group-membership`
   * **Token Claim Name:** `groups`
   * **Add to ID token:** `On`
   * **Add to access token:** `On`
   * **Add to userinfo:** `On`
   * **Full group path:** `Off` *(emits `["Engineers"]` instead of `["/Engineers"]`)*
6. Click **Save**.

---

## 5. OIDC Endpoints & Verification

### Standard OIDC Endpoints

Both hostnames below resolve to the same Keycloak instance (see §1) —
`localhost` from your browser, `host.docker.internal` from anywhere inside
the Docker network. Because `KC_HOSTNAME=host.docker.internal` is fixed,
Keycloak always *advertises* its endpoints (in the discovery document, and
as the token `iss` claim) using `host.docker.internal`, regardless of which
name you used to reach it.

| Endpoint | URL |
| :--- | :--- |
| **Well-Known Discovery** | `http://host.docker.internal:8080/realms/test/.well-known/openid-configuration` (or `localhost` from a browser) |
| **Issuer URL** | `http://host.docker.internal:8080/realms/test` |
| **Authorization Endpoint** | `http://host.docker.internal:8080/realms/test/protocol/openid-connect/auth` |
| **Token Endpoint** | `http://host.docker.internal:8080/realms/test/protocol/openid-connect/token` |
| **JWKS URI** | `http://host.docker.internal:8080/realms/test/protocol/openid-connect/certs` |
| **Userinfo Endpoint** | `http://host.docker.internal:8080/realms/test/protocol/openid-connect/userinfo` |

### Sample ID Token Payload

When `user-regular"` authenticates, the decoded ID/Access token will contain the `groups` array:

```json
{
  "exp": 1700000300,
  "iat": 1700000000,
  "auth_time": 1700000000,
  "jti": "d5351608-8f1b-4171-80a5-f86a7d1883c7",
  "iss": "http://host.docker.internal:8080/realms/test",
  "aud": "buchimaker",
  "sub": "b2f6fa72-e1cb-4e9b-b6d3-2f2f3d69b910",
  "typ": "ID",
  "azp": "buchimaker",
  "session_state": "63fcf100-349c-486d-a19f-d3b2c6ceba71",
  "preferred_username": "user-regular",
  "email_verified": false,
  "groups": [
    "buchi-users"
  ]
}
```

## 6. Configure Buchi Maker

**Settings -> Access**
- Claim: groups, `buchi-admins`, App role: Administrator
- Claim: groups, `buchi-data-admins`, App role: Data Admin
- Claim: groups, `buchi-users`, App role: Viewer

**SSO**
- Issuer URL: http://host.docker.internal:8080/realms/test — **must** be `host.docker.internal`, not `localhost`: the backend container fetches this itself (server-to-server), it isn't just a browser redirect target.
- Client ID: buchimaker
- Client Secret: from Keycloak's Clients > buchimaker > Credentials tab
- Scope: openid profile email
- Redirect URL: http://localhost:3000/auth/callback — this one stays `localhost` since it's where the *browser* gets sent back to (BuchiMaker's own host-published frontend port), which is unaffected by the container-networking issue above.