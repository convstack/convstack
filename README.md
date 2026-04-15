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

---

Made and maintained with 🧡 by [Headpat](https://headpat.space)
