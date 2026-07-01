# Local Keycloak (P1 dev login)

`realm-swarm-local.json` is a **LOCAL DEV** realm import — **not** the org's real realm. It mirrors
the org's OIDC shape (confidential client `web_channel` + a groups mapper) so the channel config is
**swap-able to a real deployment with config only**: point `OIDC_ISSUER` at the org's real Keycloak
realm and set the real client secret (operator; real secrets live in the org's vault, never here).

- Image `${DHI_REGISTRY}/keycloak:26.6.1-debian13` (mirrors the org's Keycloak version), `start-dev
  --import-realm`. Served on host `:${KEYCLOAK_PORT:-8081}`.
- **Dev passwords** in the realm JSON (`alice`/`bob`/`groot`) are **local-throwaway, not secrets** —
  the realm exists only on this dev box. The KC admin password + the client secret come from env
  (dev defaults in compose; real values via `secrets.env` in prod).
- Users: `alice` ∈ group `confluence` (→ scope `group`), `bob` ∈ no group (→ `public` only),
  `groot` ∈ realm role `groot` (web_channel admin). group→scope map is `GROUP_SCOPE_MAP` on the channel.

Realm import is strict (no unknown top-level fields — keep notes here, not in the JSON).
