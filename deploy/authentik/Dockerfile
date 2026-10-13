FROM ghcr.io/goauthentik/server:2026.8.1

# Both create a non-superuser group that carries a role. Lanyard's change_user
# can add any user to such a group, which would hand them that role.
USER root
RUN rm /blueprints/default/rbac-role-read-only.yaml /blueprints/default/rbac-role-agents.yaml
USER 1000

COPY blueprints/convstack.yaml /blueprints/custom/convstack.yaml
COPY scripts/set-avatars.py /avatar-init/set-avatars.py
COPY templates /templates
