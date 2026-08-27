# ADR-16 item 2, step 6b.6 — no-lockout migration: existing local users + groot
# (and, optionally, known SSO test users) into the kernel Identity store, BEFORE
# the :strict cutover (step 6b.7). Idempotent — safe to re-run (upsert_from_claims
# and seed_wheel are both ON CONFLICT-safe); adds rows, never removes a
# channel credential, so nobody is locked out by running this.
#
# Workspace ADR-20: there is NO standing superadmin any more. groot (and any other
# `is_groot` local account) becomes a member of the fixed groups `wheel` (may elevate,
# local-only) and `admins` (daily `admin` role). Visibility is NOT granted here — it
# comes from Project membership (`staff` is the default cohort every internal account
# joins on provisioning).
#
# Inputs:
#   arg 1 — path to a JSON export of web_channel's local users, produced from
#           INSIDE the running web_channel container (it owns that sqlite store):
#
#             docker exec hive-web_channel-1 python -c \
#               "import json; from web_channel import localusers; \
#                print(json.dumps(localusers.list_users()))" > /tmp/local_users.json
#
#   arg 2 — OPTIONAL path to a JSON list of known SSO users to pre-link, each
#           {"login": "...", "sub": "...", "is_groot": bool} (`is_groot` ⇒ `admins`
#           only — Wheel is local-only, an SSO account can never elevate). `sub` is the
#           Keycloak user id (GET /admin/realms/<realm>/users — the `id` field
#           IS the OIDC `sub`); without this, an SSO user's identity_link is
#           only created once they actually log in AFTER 6b.6's local path is
#           live for a channel that also signs (a NEW SSO subject specifically
#           cannot be JIT-provisioned over the wire yet — board/todo/jit-provision-rpc
#           — so pre-linking here is what unblocks the FIRST post-cutover login).
#
# Run from swarm/kernel (mirrors hive/scripts/er_validate.exs's convention):
#
#   SWARM_ENV=staging mise exec -- mix run --no-start \
#     ../../hive/scripts/migrate_identity.exs /tmp/local_users.json
#
# ⚠️ REAL INCIDENT (2026-07-02): `login != "groot"` guards this script from CRASHING
# on a collision, but a skipped SSO entry can be a REAL, actually-used account
# hiding behind the same login as a different provider's identity — silently
# skipping it left the genuine Keycloak `groot` unprovisioned, and it locked
# out the moment :strict shipped (nobody noticed until the operator asked
# "is groot in Keycloak or local?" after the cutover). Before skipping ANY
# collision here, check by hand which identity is the one people actually log
# in with (`docker exec <web_channel> python -c "...localusers.list_users()"`
# tells you the LOCAL side) and resolve the conflict explicitly (rename one,
# delete the unused one, or use a different login) — never let a skip ride
# quietly into a cutover.
#
# Verify (the no-lockout check): after running, every migrated login must still
# resolve via ResolveActor (the channel signs {sub: login, provider: "local"| \
# "keycloak"} and gets back CALL_OK, not CALL_UNAUTHENTICATED).

require Logger
Logger.configure(level: :warning)
alias Swarm.Identity

{:ok, _} = Application.ensure_all_started(:ecto_sql)
{:ok, _} = Application.ensure_all_started(:postgrex)
{:ok, _} = Swarm.Repo.start_link()

# --- groot: the vanity break-glass account (hive-local config; the concrete id/login
# is deployment config, never a literal name in swarm — Identity.seed_wheel/1
# moduledoc). Fixed here so re-runs are idempotent and every session migrates to
# the SAME uuid. seed_wheel puts it in `wheel` ∧ `admins` ∧ `staff` — no standing
# superadmin (ADR-20 D9): it ELEVATES per session, with re-auth + reason, when needed.
groot_id = "01920000-0000-7000-8000-00000000da7a"
{:ok, groot} = Identity.seed_wheel(%{id: groot_id, login: "groot"})
IO.puts("groot -> #{groot.id} (#{groot.status}), wheel + admins")

# --- existing LOCAL users (web_channel's own credential store) -------------
# `login != "groot"` guards against double-provisioning: seed_wheel above
# already claims identity_link(local, "groot") — if the channel ALSO has a local
# user literally named "groot", upsert_from_claims for it would collide on the
# (provider, subject) unique constraint. Any OTHER is_groot local user joins
# `wheel` + `admins` instead (they were channel-side groot-equivalent before the
# kernel existed; the memberships make that real kernel-side — elevation on demand).
local_users_path = System.argv() |> Enum.at(0)

local_users =
  case local_users_path && File.read(local_users_path) do
    {:ok, json} ->
      Jason.decode!(json)

    _ ->
      Logger.warning("no local-users JSON given (arg 1) — skipping local-user migration")
      []
  end

for %{"username" => login, "is_groot" => is_groot} <- local_users, login != "groot" do
  {:ok, u} = Identity.upsert_from_claims(%{provider: "local", subject: login, login: login})

  if is_groot do
    :ok = Identity.add_to_group(u.id, "wheel")
    :ok = Identity.add_to_group(u.id, "admins")
  end

  IO.puts("local:#{login} -> #{u.id} (#{u.status})#{if is_groot, do: " [wheel+admins]", else: ""}")
end

# --- optional: known SSO users, pre-linked so their FIRST post-cutover login
# resolves (see the header note on jit-provision-rpc). `login != "groot"`:
# `app_user.login` is GLOBALLY unique (not just per-(provider,subject)) — a
# Keycloak account also named "groot" would collide with the local vanity
# superadmin seeded above (real incident, 2026-07-02: this crashed a live run
# mid-loop; alice/bob/carol before it in iteration order were already
# committed since each upsert is its own transaction, so no partial-user
# damage — but re-running blind on the SAME list would hit the same crash).
sso_users_path = System.argv() |> Enum.at(1)

sso_users =
  case sso_users_path && File.read(sso_users_path) do
    {:ok, json} -> Jason.decode!(json)
    _ -> []
  end

for %{"login" => login, "sub" => sub} = entry <- sso_users, login != "groot" do
  {:ok, u} = Identity.upsert_from_claims(%{provider: "keycloak", subject: sub, login: login})

  # an SSO account can be an admin but never Wheel (local-only — ADR-20 D9)
  if Map.get(entry, "is_groot", false) do
    :ok = Identity.add_to_group(u.id, "admins")
  end

  IO.puts("sso:#{login} -> #{u.id} (#{u.status})")
end

IO.puts(
  "done — #{length(local_users)} local + #{length(sso_users)} SSO user(s) processed " <>
    "(groot handled separately, above)"
)
