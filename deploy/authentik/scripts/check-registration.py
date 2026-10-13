# Checks ConvStack's sign-up and password reset flows from inside Authentik:
#   docker compose exec -T server python -m manage shell < scripts/check-registration.py
# It reads mailed links from Authentik's own tokens, so it needs no mailbox.
# Every account it makes is check-...@example.invalid and is deleted again;
# KEEP=1 leaves this run's accounts for a look at lanyard, CHECK_SYNC=0 skips
# the checks that need lanyard to be running.
import json
import os
import re
import secrets
import sys
import time
from urllib.parse import urlencode

from django.conf import settings
from django.test import Client
from django.urls import reverse
from django.utils.timezone import now, timedelta

from authentik.core.expression.exceptions import SkipObjectException
from authentik.brands.models import Brand
from authentik.core.models import User
from authentik.flows.models import Flow, FlowToken
from authentik.providers.scim.models import SCIMMapping, SCIMProviderUser
from authentik.root.middleware import SessionMiddleware
from authentik.stages.authenticator_static.models import StaticDevice, StaticToken
from authentik.stages.email.models import EmailStage
from authentik.stages.email.utils import TemplateEmailMessage
from authentik.tasks.models import Task

MAIL = bool(os.environ.get("AUTHENTIK_EMAIL__HOST"))
CAPTCHA = bool(os.environ.get("TURNSTILE_SITE_KEY") and os.environ.get("TURNSTILE_SECRET_KEY"))
SYNC = os.environ.get("CHECK_SYNC", "1") == "1"
KEEP = os.environ.get("KEEP") == "1"
RUN = secrets.token_hex(4)
SIGN_UP = "convstack-enrollment"
RESET = "convstack-recovery"
TAKEN = "That email already has an account. Sign in or reset your password."
DEACTIVATED = "This account is deactivated."
SPENT = "This link has expired or was already used. Request a new one."
FIRST_PASSWORD = f"lantern-{RUN}-Harbour7"
SECOND_PASSWORD = f"orchard-{RUN}-Gable4"
failed = []


def check(label, got, want):
    ok = got == want
    if not ok:
        failed.append(label)
    print(f"{'PASS' if ok else 'FAIL'} {label}" + ("" if ok else f": got {got!r}, want {want!r}"), flush=True)


def address(name):
    return f"check-{name}-{RUN}@example.invalid"


def account(name):
    return User.objects.filter(email__iexact=address(name)).first()


def leftovers():
    return User.objects.filter(email__startswith="check-", email__endswith="@example.invalid")


class Visitor:
    """One browser working through one flow."""

    def __init__(self, slug):
        self.client = Client(HTTP_HOST="localhost:9000")
        self.url = reverse("authentik_api:flow-executor", kwargs={"flow_slug": slug})
        self.query = urlencode({"query": ""})

    def sign_in_as(self, user):
        self.client.force_login(user)
        cookie = settings.SESSION_COOKIE_NAME
        # outside tests Authentik ignores a raw session key; it wants the cookie a browser holds
        self.client.cookies[cookie] = SessionMiddleware.encode_session(self.client.cookies[cookie].value, user)

    def _read(self, res):
        for _ in range(10):
            if res.status_code != 302 or not res["Location"].startswith(self.url):
                break
            res = self.client.get(res["Location"], HTTP_ACCEPT="application/json")
        if "json" not in res.get("Content-Type", ""):
            return {"component": f"http-{res.status_code}"}
        return res.json()

    def open(self, token=None):
        if token:
            self.query = urlencode({"query": urlencode({"flow_token": token})})
        return self._read(self.client.get(f"{self.url}?{self.query}", HTTP_ACCEPT="application/json"))

    def send(self, step, **fields):
        return self.client.post(
            f"{self.url}?{self.query}",
            data=json.dumps({"component": step["component"], **fields}),
            content_type="application/json",
            HTTP_ACCEPT="application/json",
        )

    def answer(self, step, **fields):
        return self._read(self.send(step, **fields))

    def move_to(self, slug):
        """The same browser, now at another flow."""
        other = Visitor(slug)
        other.client = self.client
        return other


def asks_for(step):
    return sorted(f["field_key"] for f in step.get("fields", []))


def errors(step):
    return " ".join(e["string"] for group in (step.get("response_errors") or {}).values() for e in group)


def mailed_token(user, slug):
    token = FlowToken.objects.filter(user=user, flow__slug=slug).order_by("-expires").first()
    return token.key if token else None


def follow_link(slug, token, visitor=None):
    """Open a mailed link the way its owner would: the page first asks to continue."""
    visitor = visitor or Visitor(slug)
    step = visitor.open(token)
    if step["component"] == "ak-stage-consent":
        step = visitor.answer(step, token=step["token"])
    return visitor, step


def choose_password(visitor, step, password):
    step = visitor.answer(step, password=password, password_repeat=password)
    if step["component"] == "ak-stage-user-login":
        step = visitor.answer(step, remember_me=False)
    return step


def start_sign_up(name, email=None):
    visitor = Visitor(SIGN_UP)
    return visitor, visitor.answer(visitor.open(), name=f"Check {name}", email=email or address(name))


def ask_reset(email):
    visitor = Visitor(RESET)
    return visitor.answer(visitor.open(), uid_field=email)


def settle(seconds=300):
    """Wait for the worker to catch up. After a restart it re-applies every
    blueprint first, and a push to lanyard would wait behind that."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if not Task.objects.filter(state="queued").exists():
            return True
        time.sleep(2)
    return False


def pushed_as(user):
    """What the blueprint's sync rule sends to lanyard for this account, None when it skips it."""
    try:
        return SCIMMapping.objects.get(name="ConvStack: Lanyard SCIM User").evaluate(user=user, request=None)
    except SkipObjectException:
        return None


def contrast(a, b):
    def luminance(colour):
        channels = [int(colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        channels = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]

    lighter, darker = sorted((luminance(a), luminance(b)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def has_no_password(user):
    return user.password == "" and not user.check_password("")


def synced(user, seconds):
    """Whether Authentik sent this account to lanyard within the given time."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if SCIMProviderUser.objects.filter(user=user).exists():
            return True
        time.sleep(0.5)
    return False


def login_screen():
    step = Visitor("default-authentication-flow").open()
    check("the login screen offers sign-up exactly when mail is configured", bool(step.get("enroll_url")), MAIL)
    check("the login screen offers a reset exactly when mail is configured", bool(step.get("recovery_url")), MAIL)
    heading = Flow.objects.get(slug="default-authentication-flow").title
    check("the login screen shows its flow's heading, which names neither Authentik nor a default", (step.get("flow_info", {}).get("title"), "authentik" in heading.lower() or heading.startswith("Default")), (heading, False))


# every screen a visitor passes on the way in or out
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


def the_look():
    brand = Brand.objects.get(default=True)
    sheet = brand.branding_custom_css
    opening, closing = "/* convstack:branding */", "/* /convstack:branding */"
    check("the stylesheet has its branding block once", (sheet.count(opening), sheet.count(closing)), (1, 1))
    block = sheet.split(opening)[-1].split(closing)[0]
    check("the branding block names the product", re.search(r'--cs-name: ".+";', block) is not None, True)
    # a form field's edge needs 3:1 against what it sits on (WCAG 1.4.11), in both themes
    for selector, theme in (("html", "dark"), ('html[data-theme="light"]', "light")):
        palette = {}
        for found_selector, body in re.findall(r"(html(?:\[data-theme=\"light\"\])?)\s*\{([^}]*--cs-panel[^}]*)\}", sheet):
            if found_selector == selector:
                palette = dict(re.findall(r"--cs-([a-z-]+):\s*(#[0-9a-fA-F]{6})", body))
        edge, panel, field = palette.get("field-border"), palette.get("panel"), palette.get("field")
        check(f"the {theme} theme defines a field edge, a panel and a field", all((edge, panel, field)), True)
        if edge and panel and field:
            check(f"a field's edge stands out from the {theme} panel by 3:1", contrast(edge, panel) >= 3, True)
            check(f"a field's edge stands out from the {theme} field by 3:1", contrast(edge, field) >= 3, True)
    # Authentik adopts the sheet into every component on every page, admin and user interfaces included
    rules = re.findall(r"([^{}]+)\{", re.sub(r"/\*.*?\*/", "", sheet, flags=re.S))
    reaches = (":host(", "ak-flow-executor", "ak-brand-links", "body:has(ak-flow-executor)", "html", ".pf-c-login")
    loose = set()
    for rule in rules:
        if rule.strip().startswith("@"):
            continue
        depth, start, parts = 0, 0, []
        for i, char in enumerate(rule):
            depth += char in "([" ; depth -= char in ")]"
            if char == "," and depth == 0:
                parts.append(rule[start:i]); start = i + 1
        parts.append(rule[start:])
        loose |= {part.strip() for part in parts if part.strip() and not part.strip().startswith(reaches)}
    check("every rule of the stylesheet reaches only the sign-in screens", sorted(loose), [])
    allowed = {"html {", "html:root {", 'html:root[data-theme="light"] {', "}"}
    stray = [line.strip() for line in block.strip().splitlines() if not line.strip().startswith("--cs-") and line.strip() not in allowed]
    check("the branding block holds nothing but variables on html and its two theme rules", stray, [])
    # the base palette follows the block in the sheet, so a palette variable on a bare html rule would lose to it
    first_rule = re.search(r"html \{([^}]*)\}", block).group(1)
    palette_names = ("canvas", "panel", "field", "border", "border-strong", "field-border", "text", "muted", "shade")
    check("the branding block puts no palette variable on a bare html rule", [n for n in palette_names if re.search(rf"--cs-{n}:", first_rule)], [])
    check("a light logo is only declared on the light rule", "--cs-logo-light" not in block.split('html:root[data-theme="light"] {')[0], True)
    check("the sign-in screens have a theme", brand.attributes.get("settings", {}).get("theme", {}).get("base") in ("dark", "light", "automatic"), True)
    flows = {flow.slug: flow for flow in Flow.objects.filter(slug__in=VISITOR_FLOWS)}
    check("every visitor flow exists", sorted(flows), sorted(VISITOR_FLOWS))
    layouts = {flow.layout for flow in flows.values()}
    check("every visitor flow has the same layout, one of ours", (len(layouts), layouts <= {"sidebar_left", "sidebar_right", "stacked"}), (1, True))
    check("no visitor flow is titled after Authentik or a default", sorted(f"{slug}: {flow.title}" for slug, flow in flows.items() if "authentik" in flow.title.lower() or flow.title.startswith("Default")), [])


def mail_wording():
    someone = User(username=address("reader"), email=address("reader"), name="Check reader")
    link = "http://localhost:9000/if/flow/some-flow/?flow_token=abc123"
    for stage in ("convstack-enrollment-confirm", "convstack-recovery-email"):
        mail = TemplateEmailMessage(
            to=[(someone.name, someone.email)],
            template_name=EmailStage.objects.get(name=stage).template,
            template_context={"url": link, "user": someone, "expires": now() + timedelta(minutes=31), "token": "abc123"},
        )
        page, text = mail.alternatives[0][0], mail.body
        check(f"the {stage} mail keeps the product name out of its body", "ConvStack" in page + text, False)
        check(f"the {stage} mail carries the product name in its subject", Brand.objects.get(default=True).branding_title in EmailStage.objects.get(name=stage).subject, True)
        check(f"the {stage} mail does not mention Authentik", "authentik" in (page + text).lower(), False)
        check(f"the {stage} mail carries its link in both", (link in page, link in text), (True, True))
        check(f"the {stage} mail says how long the link lasts", ("30\xa0minutes" in page, "30\xa0minutes" in text), (True, True))


def closed_without_mail():
    for slug in (SIGN_UP, RESET):
        check(f"{slug} is closed without mail", Visitor(slug).open()["component"], "ak-stage-access-denied")
    # the form sent anyway, the way a script would
    Visitor(SIGN_UP).answer({"component": "ak-stage-prompt"}, name="Check closed", email=address("closed"))
    check("a sign-up form sent to the closed flow makes no account", account("closed"), None)


def captcha_in_front():
    check("sign-up starts with a captcha", Visitor(SIGN_UP).open()["component"], "ak-stage-captcha")
    check("reset starts with a captcha", Visitor(RESET).open()["component"], "ak-stage-captcha")


def no_captcha():
    first = Visitor(SIGN_UP).open()
    check("sign-up starts with the form, no captcha", first["component"], "ak-stage-prompt")
    check("the form asks for a name and an email, not a password", {"name", "email"} <= set(asks_for(first)) and "password" not in asks_for(first), True)
    check("reset starts by asking for the address, no captcha", Visitor(RESET).open()["component"], "ak-stage-identification")


def sign_up():
    _, step = start_sign_up("fresh")
    check("sign-up says to look in the mailbox", step["component"], "ak-stage-email")
    fresh = account("fresh")
    check("the new account is inactive", fresh.is_active, False)
    check("its username is the email", fresh.username, address("fresh"))
    check("it is an internal user", fresh.type, "internal")
    check("it is marked as an unconfirmed sign-up", bool(fresh.attributes.get("convstack_pending")), True)
    check("it has no password yet", has_no_password(fresh), True)
    check("the sync rule skips an unconfirmed account", pushed_as(fresh), None)
    if SYNC:
        check("an unconfirmed account is not sent to lanyard", synced(fresh, 10), False)

    link = mailed_token(fresh, SIGN_UP)
    visitor, step = follow_link(SIGN_UP, link)
    check("the mailed link leads to choosing a password", asks_for(step), ["password", "password_repeat"])
    fresh.refresh_from_db()
    check("following the link activates the account", fresh.is_active, True)
    check("a confirmed account is no longer marked unconfirmed", "convstack_pending" in fresh.attributes, False)

    check("a 7 character password is refused", bool(errors(visitor.answer(step, password="Ab3!xy9", password_repeat="Ab3!xy9"))), True)
    check("a long but guessable password is refused", bool(errors(visitor.answer(step, password="passwordpassword", password_repeat="passwordpassword"))), True)
    check("two different passwords are refused", bool(errors(visitor.answer(step, password=FIRST_PASSWORD, password_repeat=SECOND_PASSWORD))), True)
    fresh.refresh_from_db()
    check("a refused password is not saved", has_no_password(fresh), True)

    check("a good password signs the person in", choose_password(visitor, step, FIRST_PASSWORD)["component"], "xak-flow-redirect")
    fresh.refresh_from_db()
    check("the password was saved", fresh.check_password(FIRST_PASSWORD), True)
    check("the account has signed in", fresh.last_login is not None, True)
    check("saving the password does not mark it unconfirmed again", "convstack_pending" in fresh.attributes, False)
    if SYNC:
        check("a confirmed account is sent to lanyard", synced(fresh, 30), True)

    _, again = follow_link(SIGN_UP, link)
    check("the link works once", "password" in asks_for(again), False)


def duplicates():
    _, step = start_sign_up("same", address("fresh"))
    check("sign-up refuses a confirmed address", TAKEN in errors(step), True)
    _, step = start_sign_up("same", address("fresh").upper())
    check("sign-up refuses a confirmed address in another case", TAKEN in errors(step), True)
    check("there is still one account for it", User.objects.filter(email__iexact=address("fresh")).count(), 1)


def retry():
    start_sign_up("retry")
    first = account("retry").pk
    _, step = start_sign_up("retry")
    check("signing up again with an unconfirmed address goes through", step["component"], "ak-stage-email")
    check("it leaves exactly one account", User.objects.filter(email__iexact=address("retry")).count(), 1)
    check("the stale account was replaced", account("retry").pk != first, True)


def signed_in():
    visitor = Visitor(SIGN_UP)
    visitor.sign_in_as(account("fresh"))
    me = visitor.client.get("/api/v3/core/users/me/", HTTP_ACCEPT="application/json").json()
    check("the browser of this check is signed in", me.get("user", {}).get("username"), address("fresh"))
    check("a signed-in person is refused the sign-up flow", visitor.open()["component"], "ak-stage-access-denied")


def reset():
    known = ask_reset(address("fresh"))
    unknown = ask_reset(address("nobody"))
    check("a reset request says to look in the mailbox", known["component"], "ak-stage-email")
    check("it answers the same for an unknown address", (unknown["component"], errors(unknown)), (known["component"], errors(known)))
    check("no account is made for the unknown address", account("nobody"), None)

    fresh = account("fresh")
    visitor, step = follow_link(RESET, mailed_token(fresh, RESET))
    check("the reset link leads to choosing a password", asks_for(step), ["password", "password_repeat"])
    check("the reset holds the same password rule", bool(errors(visitor.answer(step, password="Ab3!xy9", password_repeat="Ab3!xy9"))), True)
    check("a new password signs the person in", choose_password(visitor, step, SECOND_PASSWORD)["component"], "xak-flow-redirect")
    fresh.refresh_from_db()
    check("the new password works", fresh.check_password(SECOND_PASSWORD), True)
    check("the old password stopped working", fresh.check_password(FIRST_PASSWORD), False)


def reset_confirms():
    start_sign_up("lost")
    lost = account("lost")
    check("the lost sign-up starts inactive", lost.is_active, False)
    ask_reset(address("lost"))
    _, step = follow_link(RESET, mailed_token(lost, RESET))
    lost.refresh_from_db()
    check("a reset link activates an unconfirmed account", (lost.is_active, "password" in asks_for(step)), (True, True))
    check("and it is no longer marked unconfirmed", "convstack_pending" in lost.attributes, False)


def deactivated():
    fresh = account("fresh")
    fresh.is_active = False
    fresh.save()
    check("the sync rule sends a deactivated account as inactive", (pushed_as(fresh) or {}).get("active"), False)
    if SYNC:
        check("it stays known to lanyard", SCIMProviderUser.objects.filter(user=fresh).exists(), True)


def finish_reset(email, user):
    """Ask for a reset and follow its link as far as the flow lets this account go."""
    ask_reset(email)
    visitor, step = follow_link(RESET, mailed_token(user, RESET))
    if "password" in asks_for(step):
        return choose_password(visitor, step, FIRST_PASSWORD)
    return step


def banned_stays_banned():
    fresh = account("fresh")
    check("the account is deactivated before the reset", fresh.is_active, False)
    step = finish_reset(address("fresh"), fresh)
    fresh.refresh_from_db()
    check("a reset does not switch a deactivated account back on", fresh.is_active, False)
    check("its reset link says the account is deactivated", (step["component"], step.get("error_message")), ("ak-stage-access-denied", DEACTIVATED))


def banned_before_first_sign_in():
    # confirmed by the reset link above, but never chose a password or signed in
    lost = account("lost")
    lost.is_active = False
    lost.save()
    step = finish_reset(address("lost"), lost)
    lost.refresh_from_db()
    check("a reset does not switch on an account deactivated before its first sign-in", lost.is_active, False)
    check("its reset link says so too", step["component"], "ak-stage-access-denied")
    _, step = start_sign_up("again", address("lost"))
    check("a new sign-up does not replace it", (TAKEN in errors(step), account("lost").pk), (True, lost.pk))


def banned_while_choosing():
    start_sign_up("late")
    visitor, step = follow_link(SIGN_UP, mailed_token(account("late"), SIGN_UP))
    # read again, the way an admin's change starts from the account as it is now
    late = account("late")
    late.is_active = False
    late.save()
    step = choose_password(visitor, step, FIRST_PASSWORD)
    late.refresh_from_db()
    check("a sign-up deactivated while choosing its password stays off", (late.is_active, step["component"]), (False, "ak-stage-access-denied"))

    start_sign_up("slow")
    slow = account("slow")
    choose_password(*follow_link(SIGN_UP, mailed_token(slow, SIGN_UP)), FIRST_PASSWORD)
    ask_reset(address("slow"))
    visitor, step = follow_link(RESET, mailed_token(slow, RESET))
    slow = account("slow")
    slow.is_active = False
    slow.save()
    step = choose_password(visitor, step, SECOND_PASSWORD)
    slow.refresh_from_db()
    check("an account deactivated while resetting its password stays off", (slow.is_active, step["component"]), (False, "ak-stage-access-denied"))
    check("and keeps its old password", slow.check_password(FIRST_PASSWORD), True)


def someone_elses_address():
    # a sign-up form for another address, left half done in the browser that then opens a link of its own
    for flow in (SIGN_UP, RESET):
        start_sign_up("mine")
        mine = account("mine")
        if flow == RESET:
            choose_password(*follow_link(SIGN_UP, mailed_token(mine, SIGN_UP)), FIRST_PASSWORD)
            ask_reset(address("mine"))
        browser = Visitor(SIGN_UP)
        browser.send(browser.open(), name="Somebody Else", email=address("theirs"))
        choose_password(*follow_link(flow, mailed_token(mine, flow), browser.move_to(flow)), SECOND_PASSWORD)
        mine.refresh_from_db()
        check(
            f"a {flow} link writes nothing but the password",
            (mine.email, mine.username, mine.name, "convstack_pending" in mine.attributes, account("theirs")),
            (address("mine"), address("mine"), "Check mine", False, None),
        )
        mine.delete()


def resend(visitor):
    """Press "Resend Email" on the page that says to look in the mailbox."""
    return visitor.answer({"component": "ak-stage-email"})


def a_second_link():
    form, _ = start_sign_up("twice")
    choose_password(*follow_link(SIGN_UP, mailed_token(account("twice"), SIGN_UP)), FIRST_PASSWORD)
    resend(form)
    choose_password(*follow_link(SIGN_UP, mailed_token(account("twice"), SIGN_UP)), SECOND_PASSWORD)
    check("a second link for one sign-up does not mark the account unconfirmed again", "convstack_pending" in account("twice").attributes, False)

    form, _ = start_sign_up("parked")
    choose_password(*follow_link(SIGN_UP, mailed_token(account("parked"), SIGN_UP)), FIRST_PASSWORD)
    parked = account("parked")
    parked.is_active = False
    parked.save()
    resend(form)
    visitor, step = follow_link(SIGN_UP, mailed_token(parked, SIGN_UP))
    if "password" in asks_for(step):
        step = choose_password(visitor, step, SECOND_PASSWORD)
    parked.refresh_from_db()
    check(
        "a sign-up link mailed after a deactivation does not switch the account back on",
        (parked.is_active, step["component"], step.get("error_message")),
        (False, "ak-stage-access-denied", DEACTIVATED),
    )


def one_link_one_browser():
    start_sign_up("shared")
    shared = account("shared")
    for flow in (SIGN_UP, RESET):
        if flow == RESET:
            ask_reset(address("shared"))
        link = mailed_token(shared, flow)
        first, second = Visitor(flow), Visitor(flow)
        waiting = first.open(link), second.open(link)
        choose_password(first, first.answer(waiting[0], token=waiting[0]["token"]), FIRST_PASSWORD)
        step = second.answer(waiting[1], token=waiting[1]["token"])
        check(
            f"a {flow} link already continued in another browser is refused",
            (step["component"], step.get("error_message")),
            ("ak-stage-access-denied", SPENT),
        )

    start_sign_up("slowpoke")
    token = FlowToken.objects.filter(user=account("slowpoke"), flow__slug=SIGN_UP).first()
    token.expires = now() + timedelta(seconds=3)
    token.save()
    visitor = Visitor(SIGN_UP)
    waiting = visitor.open(token.key)
    time.sleep(4)
    step = visitor.answer(waiting, token=waiting["token"])
    check(
        "a Continue page left open past the link's time is refused",
        (step["component"], step.get("error_message"), account("slowpoke").is_active),
        ("ak-stage-access-denied", SPENT, False),
    )


def deleted_while_choosing():
    start_sign_up("gone")
    visitor, step = follow_link(SIGN_UP, mailed_token(account("gone"), SIGN_UP))
    account("gone").delete()
    step = choose_password(visitor, step, FIRST_PASSWORD)
    check("an account deleted while choosing its password stays deleted", (account("gone"), step["component"]), (None, "ak-stage-access-denied"))


def reset_asks_for_the_second_factor():
    start_sign_up("keyed")
    keyed = account("keyed")
    choose_password(*follow_link(SIGN_UP, mailed_token(keyed, SIGN_UP)), FIRST_PASSWORD)
    device = StaticDevice.objects.create(user=keyed, name="check", confirmed=True)
    StaticToken.objects.create(device=device, token=f"check{RUN}")
    ask_reset(address("keyed"))
    visitor, step = follow_link(RESET, mailed_token(keyed, RESET))
    check("a reset asks for the second factor before the password", step["component"], "ak-stage-authenticator-validate")
    if step["component"] == "ak-stage-authenticator-validate":
        step = visitor.answer(step, code=f"check{RUN}")
    check("and goes on to the password once it is given", asks_for(step), ["password", "password_repeat"])


def run(group):
    try:
        group()
    except Exception as exc:  # a broken flow must fail its group, not hide the rest
        check(f"{group.__name__} ran to the end", f"{type(exc).__name__}: {exc}", None)


leftovers().delete()
if SYNC:
    check("the worker has caught up", settle(), True)
run(login_screen)
run(the_look)
run(mail_wording)
if not MAIL:
    run(closed_without_mail)
elif CAPTCHA:
    run(captcha_in_front)
else:
    for group in (
        no_captcha, sign_up, duplicates, retry, signed_in, reset, reset_confirms, deactivated,
        banned_stays_banned, banned_before_first_sign_in, banned_while_choosing,
        someone_elses_address, a_second_link, one_link_one_browser, deleted_while_choosing, reset_asks_for_the_second_factor,
    ):
        run(group)

if KEEP:
    print(f"KEPT {address('fresh')}")
else:
    leftovers().delete()
print("all checks passed" if not failed else f"FAILED {len(failed)}: {failed}")
sys.exit(1 if failed else 0)
