> **Warning**
> This project is under active development and not yet ready for production use.

# ConvStack

Umbrella repository for the Convention Apps platform. This repo contains git submodules pointing to all services in the stack.

## Services

| Service | Description |
|---|---|
| [lanyard](lanyard/) | Identity provider, OIDC server, and service catalog |
| [dashboard](dashboard/) | Auto-discovering dynamic UI shell |

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
