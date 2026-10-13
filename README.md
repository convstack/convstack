<img width="1400" height="400" alt="1400x400" src="https://github.com/user-attachments/assets/a6b3c707-9f02-4365-a0cc-63d8edb58720" />

> **Warning**
> This project is under active development and not yet ready for production use.

# ConvStack

Umbrella repository for the Convention Apps platform. This repo contains git submodules pointing to all services in the stack.

## Services

| Service | Description |
|---|---|
| [lanyard](../../../lanyard/) | Identity provider, OIDC/OAuth2 server, and service catalog |
| [dashboard](../../../dashboard/) | Auto-discovering dynamic UI shell |
| [guidebook](../../../guidebook/) | Convention guide and documentation |
| [ledger](../../../ledger/) | Invoicing and billing service |
| [schedule](../../../schedule/) | Scheduling, shifts, approvals, and kanban boards |

## Shared libraries

| Package | Description |
|---|---|
| [service-sdk](../../../service-sdk/) | `@convstack/service-sdk` — shared manifest schemas, handlers, permissions, and dev tooling consumed by every service |

## Clone with submodules

```bash
git clone --recurse-submodules https://github.com/convstack/convstack.git
```

Or if already cloned:

```bash
git submodule update --init --recursive
```

## Update all submodules to latest

```bash
git submodule update --remote --merge
```

## Run the whole stack locally

Give each clone a `.env` copied from its `.env.example`, then start the pieces in this order, each from its own clone:

1. lanyard: `bun run db:migrate`, then `bun run dev` (port 3000). Each `docker compose up` in `deploy/authentik` sends it a full user sync.
2. Authentik: `docker compose up -d --build` in `deploy/authentik` (port 9000, see its README).
3. dashboard: set `SESSION_SECRET` in its `.env` (for example from `openssl rand -base64 48`; the example value is refused), then `bun run dev` (port 4000).
4. guidebook, ledger and schedule: `bun run db:migrate`, then `bun --bun run dev` (ports 5000, 5002 and 5050). Plain `bun run dev` fails on Node 24, which won't load the service SDK's TypeScript from `node_modules`.

Values that must match: the dashboard's `DASHBOARD_CLIENT_ID` and `DASHBOARD_CLIENT_SECRET` against Authentik's `CONVSTACK_CLIENT_ID` and `CONVSTACK_CLIENT_SECRET` (the client id is also lanyard's `IDP_AUDIENCE`), and lanyard's `IDP_API_TOKEN` and `IDP_SCIM_TOKEN` against Authentik's `LANYARD_API_TOKEN` and `LANYARD_SCIM_TOKEN`.

Sign in once with Authentik's bootstrap account (`AUTHENTIK_BOOTSTRAP_EMAIL`), run `bun run lanyard:setup <that email>` in lanyard to make it admin, then register guidebook, ledger and schedule under Lanyard Admin, Services, with health check path `/api/health`, and put each key into that service's `LANYARD_SERVICE_KEY`. Restart each service after its key is in `.env`, since it reads the key and sends its pages to lanyard at start. Being lanyard admin grants no access to the services. Access comes from departments: create one under Lanyard Admin, Departments, add people, then map its roles to each service's permissions with Manage Permissions on that service under Lanyard Admin, Services. People can also sign up themselves once Authentik's `.env` has a mail host (`AUTHENTIK_EMAIL__HOST`, see its README); without one the login screen offers no sign-up or password reset. The dashboard has no login page of its own: `/login` goes straight to Authentik's sign-in screen, whose look is described in Authentik's README.

Open `http://localhost:4000`, not `127.0.0.1:4000`: sign-in state and the proxy's origin check are per origin.

To run lanyard and the dashboard from `dashboard/deploy/docker` instead, stop their dev servers first: the compose publishes ports 3000 and 4000. It needs five values, best written to `dashboard/deploy/docker/.env`, which compose reads by itself and git ignores, since every later compose command there (`exec`, `down`) needs them too: `SESSION_SECRET` (at least 32 random characters), `DB_PASSWORD` (for its own Postgres; keep it URL-safe, for example from `openssl rand -hex 24`), `DASHBOARD_CLIENT_SECRET` (Authentik's `CONVSTACK_CLIENT_SECRET`), and `IDP_API_TOKEN` and `IDP_SCIM_TOKEN` (Authentik's `LANYARD_API_TOKEN` and `LANYARD_SCIM_TOKEN`). Then run `docker compose up -d --build` there. The compose brings its own lanyard with its own Postgres, which starts with no users and keeps the password it was created with, so after changing `DB_PASSWORD` run `docker compose down` and `docker volume rm convstack-dashboard_pgdata` first. After signing in once, make yourself admin there with `docker compose exec lanyard bun run lanyard:setup <email>`. Register the services in that lanyard at `http://host.docker.internal:<port>`, put its keys in their `.env` files and restart them with their production server (`bun --bun run build`, then `bun run start`), which listens on all interfaces; the dev server only answers on localhost.

---

Made and maintained with 🧡 by [Headpat](https://headpat.space)
