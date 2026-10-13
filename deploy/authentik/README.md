# Authentik

Self-hosted Authentik instance for ConvStack, configured declaratively from a blueprint so the whole thing is reproducible from files.

## Start it

```bash
cp .env.example .env
# edit .env and set real secrets
docker compose up -d --build
```

Rebuild after pulling changes: the blueprint, the mail templates and the avatar-init script are baked into the image.

Authentik comes up at `http://localhost:9000`. The `convstack` application and its OAuth2 provider are created automatically from `blueprints/convstack.yaml` on first boot - no manual setup through the UI.

Verify it applied:

```bash
curl -s http://localhost:9000/application/o/convstack/.well-known/openid-configuration
```

If that 404s, check `docker compose logs worker | grep -i blueprint` - a blueprint that fails to parse is logged there and silently skipped.

## Avatars

The tenant's avatar mode isn't a blueprint-managed model, so a one-shot `avatar-init` container sets `AUTHENTIK_AVATARS` from `.env` on every `docker compose up`. It runs a script in Authentik's own shell (`ak shell`) with the worker's database settings, so it needs no API token. Without this, Authentik keeps its actual default (`gravatar,initials`), which embeds base64 SVGs in tokens - see lanyard's profile write-through work for why that matters. It also re-applies the ConvStack blueprint each time it runs, which is every `docker compose up` (not a reboot or a Docker restart), so edits to `.env` (social login credentials, the SCIM or logout URLs) take effect once the worker has applied it, about a minute later; a failed apply shows as an error on the `convstack` blueprint under Customization, Blueprints in the admin interface. Each re-apply resets whatever the blueprint sets, the default login flows included, so change those through the blueprint, not the admin interface.

The same container also verifies the ConvStack provider has all five property mappings attached, and reapplies our blueprint to repair it if a truly empty-volume boot let our blueprint's `!Find` lookups race Authentik's own default scope mappings and resolve to nothing - failing loudly if that still doesn't fix it. It queues our blueprint only once Authentik's own are applied and match their files, because on a first boot and after an upgrade those run as separate tasks and would put the default login flow's title back.

## Back-channel logout

When a user's Authentik session ends - they log out, an admin ends the session, or the user is deactivated or deleted - the worker posts a signed logout token to the dashboard at `DASHBOARD_INTERNAL_URL/api/auth/backchannel-logout`, which at once ends the dashboard sessions signed in through that Authentik session. Authentik only sends it while the dashboard holds an unexpired access token from that session, and a message can be lost. Either way the dashboard session still ends at its next refresh: Authentik refuses the refresh for a deactivated or deleted user, and otherwise answers with tokens that no longer carry the session id, which the dashboard refuses.

## Sign-in methods

- **Email and password** as usual. Accounts come from Lanyard Admin, a social login, or the sign-up below.
- **Sign up** and **Forgot username or password?** appear on the login screen once a mail host is set (see Mail). Sign-up asks for a name and an email address and mails a link. The link opens a page with a Continue button, which keeps mail scanners that open links from confirming anything, and then asks for a password; only someone who can read the mailbox ever sets it. The new account is a plain user with no staff or admin role. The reset mails a link the same way, and its link also confirms an address whose sign-up mail was lost. Both links last 30 minutes and work once. Passwords need at least 10 characters and must not be easy to guess. A reset asks for the account's TOTP code or passkey first, if it has one, so a mailbox alone does not open an account with a second factor; whoever lost that too needs an admin. Neither link switches a deactivated account back on; it ends at "This account is deactivated."
- **Discord, Google and GitHub** appear once their `*_CLIENT_ID` and `*_CLIENT_SECRET` are set in `.env`. Create an OAuth app with each provider (Discord Developer Portal, Google Cloud console, GitHub developer settings) and register the callback `<public Authentik URL>/source/oauth/callback/<discord|google|github>/`. A first social sign-in creates an account. It is refused if the provider hasn't verified the email address. One whose email already belongs to an account is refused rather than linked, because providers such as Discord hand out unverified addresses; the owner signs in another way and connects the social login in their settings. Enabling Google or GitHub makes Authentik fetch that provider's signing keys when it applies the blueprint, so the worker needs internet access then.
- **Passkeys** sign in without a password, offered through the browser's autofill, and the device must verify the user with a PIN or biometric. After a password, Authentik asks for a TOTP code or a passkey if the user has one. Users add both themselves in Authentik's settings page (`/if/user/`), which ConvStack users can open because they are internal users. Discord, Google and GitHub sign-ins rely on that provider's own protection and are not asked for TOTP.
- **Remember me:** after signing in, Authentik asks whether to stay signed in. Yes keeps the login for 30 days; otherwise it ends when the browser closes.

## Look

Every screen a visitor passes (sign-in, sign-up, reset, consent, sign-out, second-factor setup) uses one design: the form in a panel on the left, a quiet drawing in the palette on the right, folding into a single column below 1120 px. It is the default brand's custom CSS in `blueprints/convstack.yaml`, and it only restyles Authentik's own components, so every step Authentik can show keeps working. Authentik adopts a brand's CSS into every component on every page, admin and user interfaces included, so each rule is scoped: generic elements are only reached inside the flow's own components (`:host(:is([part="challenge"], ak-flow-card))`), the page rules need the flow executor on the page (`body:has(ak-flow-executor)`), and `check-registration.py` fails if a selector reaches further. Authentik's "Powered by" footer entry is hidden; a convention's own footer links stay.

- The block between `/* convstack:branding */` and `/* /convstack:branding */` at the top of the stylesheet holds the values meant to change: the name shown as the wordmark and the accent shades. Each shade has to keep 4.5:1 against what it sits on (white text on the button, the link on the panel of each theme). When an admin changes a theme's six colours (background, panel, text, muted text, border, accent) in Lanyard Admin, Branding, the block also carries that theme's palette as overrides: `html:root { ... }` for dark and `html:root[data-theme="light"] { ... }` for light. A theme left at the default colours writes nothing. A light-theme logo, when one is set, is written the same way: the light rule hides Authentik's own logo (the main one) and the stylesheet draws the light one in its place (`--cs-logo-light`), because Authentik's brand has a single logo field. The block comes before the base palette in the stylesheet, so a plain `html` rule would lose; the extra `:root` is what lets the override win, and `check-registration.py` fails if a palette variable ends up on a bare `html` rule.
- The blueprint also sets the dark theme on the brand and the `sidebar_left` layout and a heading on each of those flows. The headings leave the product name out, because the wordmark already shows it.
- The stylesheet depends on Authentik's markup. After a version bump, look at sign-in, sign-up and reset at phone and desktop width in both themes before shipping; a selector that no longer matches falls back to Authentik's own styling.

The dashboard's `/login` leads straight here. It only shows a page of its own when a sign-in came back with an error.

### Branding

An admin changes the name, logo, favicon, accent, theme, layout, background, heading and the line under it under Lanyard Admin, Branding. Lanyard keeps the values and writes them to Authentik through its API, with the service account it already uses for users:

- **What it writes:** the default brand's title, logo, favicon and theme; the block between the two markers of the stylesheet, and nothing else in it; the layout on every flow listed in the blueprint and the heading on the sign-in ones; the subjects of the two mail steps. Images are uploads kept in lanyard's file storage, which has to be set up (`S3_*` in lanyard's `.env`); without it the page says so and everything else still works. The storage address (`S3_PUBLIC_URL`) has to be reachable from the visitor's browser, and be https wherever Authentik is.
- **When:** on every save, and after every `docker compose up`, because applying our blueprint puts the defaults back. The `avatar-init` container waits for the blueprint to finish and then calls lanyard (`POST /api/idp/branding/apply` with `LANYARD_SCIM_TOKEN`, which it takes from `LANYARD_SCIM_URL`). The call does not follow redirects (it would carry the token along) and only counts an answer of `{"success": true}`. With lanyard down, redirecting or answering something else it says `branding not applied` and exits normally; Authentik then shows the default look until the next save or `up`.
- **Rights:** the blueprint grants lanyard's role view and change on the default brand, the visitor flows and the two mail steps, object by object (`scripts/check-branding.py` checks that it can change those and nothing else). It cannot create or delete a brand, flow or stage. A change right covers every field of that object, not just the ones lanyard writes: a leaked `IDP_API_TOKEN` could, for example, turn off sign-in on the default flow or point the reset mail step at another SMTP host, on top of the user rights the token already has. Authentik's permission model cannot narrow this to single fields.
- **Safety:** an admin's text and colours reach the stylesheet as validated fields, never as CSS. Text is written as CSS escapes, so no name or line can end a string, a rule or a comment; the accent is turned into shades that stay readable (4.5:1) in both themes; image addresses are accepted only from lanyard's own storage.

## Mail

Sign-up and password reset work by mailing a link, so both stay closed, on the login screen and at their own addresses, until `AUTHENTIK_EMAIL__HOST` is set in `.env`. The other `AUTHENTIK_EMAIL__*` keys are the port, the login if the server needs one, `USE_TLS` or `USE_SSL`, and the from address. They take effect on the next `docker compose up`.

Both mails are ours, in `templates/convstack/`: `confirm` and `reset`, each as `.html` and `.txt`, sharing `layout.html`. Authentik's own templates name Authentik and carry its logo. Change the wording there and rebuild.

A mail that cannot be sent does not show on the page: the visitor is told to check their inbox either way. The worker retries it and logs `Failed to process message authentik.stages.email.tasks.send_mail` (`docker compose logs worker`). That log line holds the whole mail, its link included.

A sign-up nobody has confirmed stays in Authentik as an inactive account and is not sent to lanyard. Signing up again with the same address replaces it. Nothing else removes them.

Set `TURNSTILE_SITE_KEY` and `TURNSTILE_SECRET_KEY` (Cloudflare Turnstile) on any deployment the public can reach: with both, sign-up and reset start with a captcha. Without them a script can request mails for addresses of its choosing without limit: Authentik's five mails per address in five minutes only counts the "Resend Email" button.

`scripts/check-registration.py` checks both flows from inside Authentik:

    docker compose exec -T server python -m manage shell < scripts/check-registration.py

It needs a mail host to be set but no mailbox, creates only `check-...@example.invalid` accounts and deletes them again.

## User sync (SCIM)

Authentik pushes every user change to lanyard at `LANYARD_SCIM_URL` using `LANYARD_SCIM_TOKEN`, which must match lanyard's `IDP_SCIM_TOKEN`. Changes are sent from the worker as they happen, and a full sync runs every 4 hours. Each `docker compose up` sends a full sync too, so start lanyard first; pushes that fail are retried a few times and then wait for the next full sync.

Service accounts, users without an email and sign-ups nobody has confirmed are not pushed, and groups are skipped on purpose - departments live in lanyard.

The default `LANYARD_SCIM_URL` reaches a lanyard dev server on the host through Docker Desktop's `host.docker.internal`; on Linux Docker Engine, set `LANYARD_SCIM_URL` to an address the worker can reach, since the dev server listens on `127.0.0.1`.

## Lanyard's service token

`LANYARD_API_TOKEN` can view, create, change and delete every Authentik user, admins included. Lanyard needs that to manage accounts it didn't create itself, like Discord sign-ups, so keep the token as secret as an admin login.

Authentik only checks which group a user is added to through the user API when that group is a superuser group, so the same token could add users to any group that carries a role. The image drops Authentik's two default groups that do (`authentik Read-only` and `authentik Agent-Users`), and the blueprint deletes them from instances set up before that. Don't attach roles to groups on a ConvStack instance.

## Version pin

The image is pinned to `ghcr.io/goauthentik/server:2026.8.1`, not `latest`. Authentik forbids skipping major versions on upgrade, so bumping this tag has to be a deliberate, one-version-at-a-time decision, not something that drifts on its own.

After a bump, run both check scripts and look at the sign-in, sign-up and reset screens in both themes. The blueprint refers to Authentik's own default flows, stages and mail templates by name; if a new version removes or renames one of them, the whole convstack blueprint is refused and nothing of ours is applied until the entry is fixed.

## Local image

`docker-compose.yml` builds a local image (see `Dockerfile`) that layers `blueprints/convstack.yaml`, `templates/` and the `avatar-init` script on top of the upstream Authentik image, rather than bind-mounting them in. This is a workaround for Docker Desktop's file-sharing allowlist on this host, not a design choice - a straight bind mount of `./blueprints` (and `./scripts`) is the simpler setup and works fine wherever the host directory is already shared with Docker.

## Redistribution

If you're self-hosting ConvStack yourself, pull the upstream `ghcr.io/goauthentik/server` image directly rather than any ConvStack-built one. The `enterprise/` directory in that image is proprietary and must not be redistributed.
