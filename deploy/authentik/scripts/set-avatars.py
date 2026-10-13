"""Sets the tenant avatar mode and re-applies the convstack blueprint.

Runs in Authentik's shell (ak shell) with the worker's database settings, so
it needs no API token. Tenant.avatars isn't blueprint-manageable, and Authentik
only re-applies a blueprint when its file changes, so without this an .env edit
(new social login credentials, say) would never reach it.

Also checks that the convstack provider's property mappings got attached: on
an empty-volume boot, the blueprint's !Find lookups can race Authentik's own
blueprint that creates the default scope mappings and leave the provider
without name/email claims. If that happened, wait for the defaults, then queue
the apply again until the mappings are there.
"""

import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.request

from authentik.blueprints.models import BlueprintInstance, BlueprintInstanceStatus
from authentik.blueprints.v1.labels import LABEL_AUTHENTIK_INSTANTIATE
from authentik.blueprints.v1.tasks import apply_blueprint, blueprints_find
from authentik.providers.oauth2.models import OAuth2Provider, ScopeMapping
from authentik.tasks.models import Task
from authentik.tenants.models import Tenant

MODE = os.environ.get("AUTHENTIK_AVATARS", "attributes.avatar_url").strip()
PROVIDER_NAME = "ConvStack Dashboard"
BLUEPRINT_NAME = "convstack"
BLUEPRINT_TASKS = "authentik.blueprints.v1.tasks."
CUSTOM_MAPPING_NAME = "ConvStack: Lanyard User ID"
DEFAULT_SCOPE_MAPPINGS = {
    "openid": "goauthentik.io/providers/oauth2/scope-openid",
    "profile": "goauthentik.io/providers/oauth2/scope-profile",
    "email": "goauthentik.io/providers/oauth2/scope-email",
    "offline_access": "goauthentik.io/providers/oauth2/scope-offline_access",
}
LANYARD_SCIM_URL = os.environ.get("LANYARD_SCIM_URL", "")
LANYARD_SCIM_TOKEN = os.environ.get("LANYARD_SCIM_TOKEN", "")
BRANDING_TIMEOUT_SECONDS = 60
FIRST_APPLY_SECONDS = 300
MAPPING_WAIT_SECONDS = 120
REAPPLY_RETRY_SECONDS = 300
REAPPLY_INTERVAL_SECONDS = 15


def fail(message):
    print(message, file=sys.stderr)
    sys.exit(1)


def wait_for(find, seconds):
    deadline = time.monotonic() + seconds
    while True:
        found = find()
        if found is not None or time.monotonic() >= deadline:
            return found
        time.sleep(5)


def expected_mappings():
    """Display name -> pk for each mapping the provider needs, or None if it doesn't exist yet."""
    expected = {}
    for name, managed in DEFAULT_SCOPE_MAPPINGS.items():
        mapping = ScopeMapping.objects.filter(managed=managed).first()
        expected[name] = mapping.pk if mapping else None
    custom = ScopeMapping.objects.filter(name=CUSTOM_MAPPING_NAME).first()
    expected[CUSTOM_MAPPING_NAME] = custom.pk if custom else None
    return expected


def missing_mappings():
    """Names of mappings the convstack provider needs but doesn't have attached."""
    provider = OAuth2Provider.objects.filter(name=PROVIDER_NAME).first()
    attached = set(provider.property_mappings.values_list("pk", flat=True)) if provider else set()
    return [name for name, pk in expected_mappings().items() if pk is None or pk not in attached]


def unfinished_blueprint_tasks():
    return Task.objects.filter(actor_name__startswith=BLUEPRINT_TASKS).exclude(state__in=("done", "rejected"))


def apply_branding():
    """Ask lanyard to write its saved sign-in branding again, since applying our blueprint reset it.

    Lanyard being away is not this container's failure: Authentik then shows the
    default look until the next save or the next `up`.
    """
    if not LANYARD_SCIM_URL or not LANYARD_SCIM_TOKEN:
        print("branding not applied: LANYARD_SCIM_URL or LANYARD_SCIM_TOKEN is not set")
        return
    url = LANYARD_SCIM_URL.rstrip("/").removesuffix("/scim/v2") + "/api/idp/branding/apply"

    class Stay(urllib.request.HTTPRedirectHandler):
        """A redirect would carry the token to another address, and its answer would confirm nothing."""

        def redirect_request(self, *args, **kwargs):
            return None

    try:
        request = urllib.request.Request(url, method="POST", headers={"Authorization": f"Bearer {LANYARD_SCIM_TOKEN}"})
        with urllib.request.build_opener(Stay).open(request, timeout=BRANDING_TIMEOUT_SECONDS) as response:
            answer = json.load(response)
    except urllib.error.HTTPError as exc:
        print(f"branding not applied: lanyard answered {exc.code}: {exc.read().decode(errors='replace')[:200]}")
    except (OSError, ValueError, http.client.HTTPException) as exc:
        print(f"branding not applied: lanyard could not be reached or understood ({exc})")
    else:
        if isinstance(answer, dict) and answer.get("success") is True:
            print("sign-in branding applied by lanyard")
        else:
            print("branding not applied: lanyard did not confirm it")


def own_blueprints_pending():
    """Whether Authentik still has one of its own blueprints to apply.

    Its files are compared the way its own discovery does it, because right
    after a start the worker may not have queued anything yet.
    """
    if unfinished_blueprint_tasks().exists():
        return True
    for found in blueprints_find():
        if found.meta and found.meta.labels.get(LABEL_AUTHENTIK_INSTANTIATE, "").lower() == "false":
            continue
        applied = BlueprintInstance.objects.filter(path=found.path).first()
        if applied is None:
            return True
        # a failed one is retried by Authentik in its own time
        if applied.name == BLUEPRINT_NAME or not applied.enabled or applied.status == BlueprintInstanceStatus.ERROR:
            continue
        if applied.last_applied_hash != found.hash:
            return True
    return False


if not MODE:
    fail("AUTHENTIK_AVATARS is empty")

tenant = Tenant.objects.get(schema_name="public")
tenant.avatars = MODE
tenant.save()
print(f"avatars set to {MODE!r}")

with tenant:
    instance = wait_for(
        lambda: BlueprintInstance.objects.filter(name=BLUEPRINT_NAME).first(),
        FIRST_APPLY_SECONDS,
    )
    if instance is None:
        fail("convstack blueprint instance not found")

    # Authentik's own blueprints run as separate tasks on an empty volume and
    # after an upgrade, and reset what ours changes on the default login flow
    # (its title), so ours has to be applied after them
    settled = wait_for(lambda: None if own_blueprints_pending() else True, FIRST_APPLY_SECONDS)
    if settled is None:
        fail("Authentik's own blueprints are still being applied")
    apply_blueprint.send_with_options(args=(instance.pk,), rel_obj=instance)
    print("convstack blueprint queued for the worker")

    # on an empty volume the worker hasn't applied the blueprint yet
    provider = wait_for(
        lambda: OAuth2Provider.objects.filter(name=PROVIDER_NAME).first(),
        FIRST_APPLY_SECONDS,
    )
    if provider is None:
        fail("convstack provider never appeared")

    missing = missing_mappings()
    if missing:
        print(f"convstack provider missing property mappings: {missing}; waiting for defaults")
        exp = expected_mappings()
        wait_deadline = time.monotonic() + MAPPING_WAIT_SECONDS
        while any(pk is None for pk in exp.values()) and time.monotonic() < wait_deadline:
            time.sleep(2)
            exp = expected_mappings()
        unresolved = [name for name, pk in exp.items() if pk is None]
        if unresolved:
            fail(f"scope mappings never appeared: {', '.join(unresolved)}")

        print("defaults present, reapplying blueprint")
        reapply_deadline = time.monotonic() + REAPPLY_RETRY_SECONDS
        # a queued apply can sit behind other startup tasks for over a minute,
        # so re-queue on an interval instead of every poll
        next_reapply = time.monotonic()
        while missing and time.monotonic() < reapply_deadline:
            if time.monotonic() >= next_reapply:
                apply_blueprint.send_with_options(args=(instance.pk,), rel_obj=instance)
                next_reapply = time.monotonic() + REAPPLY_INTERVAL_SECONDS
            time.sleep(2)
            missing = missing_mappings()

    if missing:
        fail(f"convstack provider still missing property mappings after reapply: {', '.join(missing)}")

    if wait_for(lambda: None if unfinished_blueprint_tasks().exists() else True, FIRST_APPLY_SECONDS):
        apply_branding()
    else:
        print("branding not applied: the blueprint is still being applied")
