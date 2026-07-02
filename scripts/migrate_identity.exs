# ADR-16 item 2, step 6b.6 — no-lockout migration: existing local users + groot
# (and, optionally, known SSO test users) into the kernel Identity store, BEFORE
# the :strict cutover (step 6b.7). Idempotent — safe to re-run (upsert_from_claims
# and seed_superadmin are both ON CONFLICT-safe); adds rows, never removes a
# channel credential, so nobody is locked out by running this.
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
#           {"login": "...", "sub": "...", "is_groot": bool}. `sub` is the
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
# Verify (the no-lockout check): after running, every migrated login must still
# resolve via ResolveActor (the channel signs {sub: login, provider: "local"| \
# "keycloak"} and gets back CALL_OK, not CALL_UNAUTHENTICATED).

require Logger
Logger.configure(level: :warning)
alias Swarm.Identity

{:ok, _} = Application.ensure_all_started(:ecto_sql)
{:ok, _} = Application.ensure_all_started(:postgrex)
{:ok, _} = Swarm.Repo.start_link()

# --- groot: the vanity superadmin (hive-local config; the concrete id/login
# is deployment config, never a literal name in swarm — Identity.seed_superadmin/1
# moduledoc). Fixed here so re-runs are idempotent and every session migrates to
# the SAME uuid.
groot_id = "01920000-0000-7000-8000-00000000da7a"
{:ok, groot} = Identity.seed_superadmin(%{id: groot_id, login: "groot"})
IO.puts("groot -> #{groot.id} (#{groot.status}), superadmin")

# --- existing LOCAL users (web_channel's own credential store) -------------
# `login != "groot"` guards against double-provisioning: seed_superadmin above
# already claims identity_link(local, "groot") — if the channel ALSO has a local
# user literally named "groot", upsert_from_claims for it would collide on the
# (provider, subject) unique constraint. Any OTHER is_groot local user gets an
# explicit superadmin grant instead (they were channel-side groot-equivalent
# before the kernel existed; the grant makes that real kernel-side).
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
    :ok = Identity.grant_role(u.id, "superadmin", "direct")
  end

  IO.puts("local:#{login} -> #{u.id} (#{u.status})#{if is_groot, do: " [superadmin]", else: ""}")
end

# --- optional: known SSO users, pre-linked so their FIRST post-cutover login
# resolves (see the header note on jit-provision-rpc). -----------------------
sso_users_path = System.argv() |> Enum.at(1)

sso_users =
  case sso_users_path && File.read(sso_users_path) do
    {:ok, json} -> Jason.decode!(json)
    _ -> []
  end

for %{"login" => login, "sub" => sub} = entry <- sso_users do
  {:ok, u} = Identity.upsert_from_claims(%{provider: "keycloak", subject: sub, login: login})

  if Map.get(entry, "is_groot", false) do
    :ok = Identity.grant_role(u.id, "superadmin", "direct")
  end

  IO.puts("sso:#{login} -> #{u.id} (#{u.status})")
end

IO.puts(
  "done — #{length(local_users)} local + #{length(sso_users)} SSO user(s) processed " <>
    "(groot handled separately, above)"
)
