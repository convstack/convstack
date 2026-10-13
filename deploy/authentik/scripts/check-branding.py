# Checks what lanyard's service account may change in Authentik for the
# sign-in branding, and what it may not:
#   docker compose exec -T server python -m manage shell < scripts/check-branding.py
# Every change it sends sets a value to what it already is.
import ast
import contextlib
import http.client
import http.server
import io
import json
import os
import re
import socketserver
import sys
import threading
import urllib.error
import urllib.request

from django.test import Client

from authentik.brands.models import Brand
from guardian.shortcuts import get_perms

from authentik.core.models import Token
from authentik.flows.models import Flow
from authentik.rbac.models import Role
from authentik.stages.email.models import EmailStage
from authentik.stages.prompt.models import PromptStage

VISITOR_FLOWS = (
    "default-authentication-flow",
    "convstack-enrollment",
    "convstack-recovery",
    "default-provider-authorization-explicit-consent",
    "default-provider-authorization-implicit-consent",
    "default-invalidation-flow",
    "default-provider-invalidation-flow",
    "default-source-authentication",
    "default-source-enrollment",
    "default-source-pre-authentication",
    "default-password-change",
    "default-authenticator-static-setup",
    "default-authenticator-totp-setup",
    "default-authenticator-webauthn-setup",
)
MAIL_STEPS = ("convstack-enrollment-confirm", "convstack-recovery-email")
failed = []


def check(label, got, want):
    ok = got == want
    if not ok:
        failed.append(label)
    print(f"{'PASS' if ok else 'FAIL'} {label}" + ("" if ok else f": got {got!r}, want {want!r}"), flush=True)


token = Token.objects.get(user__username="lanyard-service", intent="api")
client = Client(HTTP_HOST="localhost:9000")


def api(method, path, body=None):
    res = getattr(client, method)(
        path,
        data=json.dumps(body) if body is not None else None,
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {token.key}",
        HTTP_ACCEPT="application/json",
    )
    return res


brand = Brand.objects.get(default=True)
found = api("get", "/api/v3/core/brands/?default=true")
check("lanyard finds the default brand", (found.status_code, [b["brand_uuid"] for b in found.json().get("results", [])]), (200, [str(brand.pk)]))
check(
    "lanyard may change the default brand",
    api("patch", f"/api/v3/core/brands/{brand.pk}/", {"branding_title": brand.branding_title, "branding_custom_css": brand.branding_custom_css, "attributes": brand.attributes}).status_code,
    200,
)

css = brand.branding_custom_css
opening, closing = "/* convstack:branding */", "/* /convstack:branding */"
override = f'{opening}\nhtml {{\n  --cs-name: "Check";\n}}\nhtml:root {{\n  --cs-canvas: #101820;\n}}\n{closing}'
patched = css[: css.index(opening)] + override + css[css.index(closing) + len(closing) :]
check("lanyard may write a palette override into the sign-in stylesheet", api("patch", f"/api/v3/core/brands/{brand.pk}/", {"branding_custom_css": patched}).status_code, 200)
brand.refresh_from_db()
served = brand.branding_custom_css
base = re.search(r"html \{\s*--cs-canvas", served)
check("the override is stored", "html:root {" in served, True)
check("Authentik's own palette still comes after it, which is why the override needs :root", base is not None and base.start() > served.index("html:root {"), True)
Brand.objects.filter(pk=brand.pk).update(branding_custom_css=css)
brand.refresh_from_db()
for slug in VISITOR_FLOWS:
    flow = Flow.objects.get(slug=slug)
    check(f"lanyard may set layout and heading of {slug}", api("patch", f"/api/v3/flows/instances/{slug}/", {"layout": flow.layout, "title": flow.title}).status_code, 200)
for name in MAIL_STEPS:
    stage = EmailStage.objects.get(name=name)
    listed = api("get", f"/api/v3/stages/email/?name={name}")
    check(f"lanyard finds the mail step {name}", (listed.status_code, [s["pk"] for s in listed.json().get("results", [])]), (200, [str(stage.pk)]))
    check(f"lanyard may set the subject of {name}", api("patch", f"/api/v3/stages/email/{stage.pk}/", {"subject": stage.subject}).status_code, 200)

# a refused object is either forbidden or not visible at all (404); what matters is that it did not change
REFUSED = (403, 404)
other = Flow.objects.get(slug="default-user-settings-flow")
check("lanyard may not change another flow", api("patch", f"/api/v3/flows/instances/{other.slug}/", {"title": "changed by the check"}).status_code in REFUSED, True)
other.refresh_from_db()
check("and that flow is as it was", other.title != "changed by the check", True)
check("lanyard may not create a brand", api("post", "/api/v3/core/brands/", {"domain": "check.invalid", "branding_title": "x"}).status_code in REFUSED, True)
leftover = Brand.objects.filter(domain="check.invalid").count()
Brand.objects.filter(domain="check.invalid").delete()

# deleting is checked in the permissions, not by trying it on a real flow
role = Role.objects.get(name="ConvStack Lanyard Service")
everywhere = get_perms(role)


def only_view_and_change(obj, app, model):
    return sorted(get_perms(role, obj) - everywhere) == [f"{app}.change_{model}", f"{app}.view_{model}"]


check("the role holds no right on brands, flows or mail steps in general", sorted(p for p in everywhere if p.split(".")[0] in ("authentik_brands", "authentik_flows", "authentik_stages_email")), [])
check("it may only view and change the default brand, not delete it", only_view_and_change(brand, "authentik_brands", "brand"), True)
check("it may only view and change each visitor flow, not delete one", [slug for slug in VISITOR_FLOWS if not only_view_and_change(Flow.objects.get(slug=slug), "authentik_flows", "flow")], [])
check("it may only view and change each mail step", [name for name in MAIL_STEPS if not only_view_and_change(EmailStage.objects.get(name=name), "authentik_stages_email", "emailstage")], [])
prompt = PromptStage.objects.get(name="convstack-set-password")
check("lanyard may not change another stage", api("patch", f"/api/v3/stages/prompt/stages/{prompt.pk}/", {"name": "changed by the check"}).status_code in REFUSED, True)
prompt.refresh_from_db()
check("and that stage is as it was", prompt.name != "changed by the check", True)
check("nothing was created or removed", (Brand.objects.count(), Flow.objects.filter(slug="convstack-recovery").count(), leftover), (1, 1, 0))

# the init container's call, run the way it is written in the image
source = open("/avatar-init/set-avatars.py", encoding="utf-8").read()
function = [n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == "apply_branding"]
namespace = {"urllib": urllib, "json": json, "http": http, "BRANDING_TIMEOUT_SECONDS": 5}
exec(compile(ast.Module(body=function, type_ignores=[]), "set-avatars.py", "exec"), namespace)


def apply_branding(url, token="secret-token"):
    namespace.update(LANYARD_SCIM_URL=url, LANYARD_SCIM_TOKEN=token)
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            namespace["apply_branding"]()
    except Exception as exc:  # the container must not die on anything lanyard does
        return f"RAISED {type(exc).__name__}: {exc}"
    return out.getvalue().strip()


def lanyard_like(status, body=b"", headers=()):
    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append((self.command, self.path, self.headers.get("Authorization")))
            self.send_response(status)
            for name, value in headers:
                self.send_header(name, value() if callable(value) else value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, seen


def scim(server, suffix="/scim/v2"):
    return f"http://127.0.0.1:{server.server_address[1]}{suffix}"


NOTICE = "branding not applied"
OK = "sign-in branding applied by lanyard"

good, good_seen = lanyard_like(200, b'{"success": true}', [("Content-Type", "application/json")])
check("the init call says so when lanyard confirms", apply_branding(scim(good)), OK)
check("and sends a POST to the apply route with the bearer token", good_seen, [("POST", "/api/idp/branding/apply", "Bearer secret-token")])
apply_branding(scim(good, "/scim/v2/"))
check("a trailing slash on the address changes nothing", good_seen[-1][1], "/api/idp/branding/apply")

elsewhere, elsewhere_seen = lanyard_like(200, b'{"success": true}')
moved, moved_seen = lanyard_like(302, headers=[("Location", lambda: f"http://127.0.0.1:{elsewhere.server_address[1]}/api/idp/branding/apply")])
check("a redirect is reported, not followed", apply_branding(scim(moved)).startswith(NOTICE), True)
check("so the token never goes to the address it names", (len(moved_seen), elsewhere_seen), (1, []))

page, _ = lanyard_like(200, b"<!doctype html><title>app</title>", [("Content-Type", "text/html")])
check("a page that is not lanyard's answer is not a success", apply_branding(scim(page)).startswith(NOTICE), True)
check("an answer without success: true is not one either", apply_branding(scim(lanyard_like(200, b'{"error": "x"}')[0])).startswith(NOTICE), True)
refused, _ = lanyard_like(401, b'{"error": "Unauthorized"}')
check("a refusal is reported with its status", apply_branding(scim(refused)).startswith(f"{NOTICE}: lanyard answered 401"), True)
check("an address without a scheme is reported, not raised", apply_branding("host.docker.internal:3000/scim/v2").startswith(NOTICE), True)


class Garbage(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.recv(1024)
        self.request.sendall(b"this is not http\r\n\r\n")


noise = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Garbage)
threading.Thread(target=noise.serve_forever, daemon=True).start()
check("something that does not speak HTTP is reported, not raised", apply_branding(scim(noise)).startswith(NOTICE), True)
check("nothing listening is reported", apply_branding("http://127.0.0.1:9/scim/v2").startswith(NOTICE), True)
check("a missing token is reported", apply_branding(scim(good), token=""), f"{NOTICE}: LANYARD_SCIM_URL or LANYARD_SCIM_TOKEN is not set")

if os.environ.get("AFTER_INIT") == "1":
    brand.refresh_from_db()
    check("the init container put the saved branding back", (brand.branding_title, '--cs-name: "ConvStack";' in brand.branding_custom_css), (os.environ["EXPECT_NAME"], False))

print("all checks passed" if not failed else f"FAILED {len(failed)}: {failed}")
sys.exit(1 if failed else 0)
