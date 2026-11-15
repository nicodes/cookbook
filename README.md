<!-- Generated from private documentation source. Do not edit directly. Source SHA256: fbc5e4704056fab3285af8ab0a7fa4f1a4ddccac31cb7167e74cb0dc5dc9be35 -->

# Cookbook

Cookbook is a public recipe SDK for consistent build, test and automation interfaces. It provides four recipes: `godot-addon`, `rust-cli`, `godot-game` and `react-native`.

Applications keep their native code, assets, assertions and policy. A bounded `cookbook.toml` declares recipe data; Cookbook generates the root Mise tasks and workflow callers. Recipes follow one contract without per-application bypasses.

## Use a generated repository

Use Mise, Git, Bash and Python 3.11 or newer for bootstrap. The repository declares the exact SDK versions its native programs need.

```sh
mise run help
mise run setup
mise run build
mise run test
```

Help works offline. Setup explicitly prepares the pinned recipe source, generic tools and dependencies. Build creates native outputs; test consumes prepared inputs and runs the declared native assertions. Tests do not build the application or implicitly install missing SDKs.

The common tasks are `help`, `setup`, `fmt`, `build`, `test`, `test:lint`, `test:unit`, `test:security` and `clean`. Applicable recipes also expose their declared integration, end-to-end, development and database tasks. Run `mise run help` for the repository's actual interface.

## Generate and check callers

Use an exact clean Cookbook checkout at an immutable commit, with its declared Tools dependency prepared. `COOKBOOK_ROOT` names that checkout and `COOKBOOK_REF` names its full commit.

```sh
python3 "$COOKBOOK_ROOT/cookbook/generate.py" \
  --root . --revision "$COOKBOOK_REF" --workflows
python3 "$COOKBOOK_ROOT/cookbook/generate.py" \
  --root . --revision "$COOKBOOK_REF" --workflows --check
```

The second command checks canonical source without running contributor tasks. Edit the native recipe declaration and regenerate the entrypoint and callers together.

Generated `mise.toml` binds the Cookbook revision and recipe data. Cookbook's own `mise.toml` pins its dependency on [Tools](https://github.com/nicodes/tools), a collection of independent reusable tools and Actions. Consumers cannot substitute a different Tools revision. Receipts bind both actual sources.

Action dependencies use immutable commits inline in YAML and generators. Cookbook does not require a separate action-pin configuration file. Deployment declarations retain their own deployment authority.

## Godot display profile

Every `godot-game` project keeps one display profile in `project.godot`, checked when the recipe
validates its configuration:

| | Portrait | Landscape |
| --- | --- | --- |
| Design canvas | 1080×1920 | 1920×1080 |
| Stretch | `canvas_items`, `expand`, fractional scale | `canvas_items`, `expand`, fractional scale |
| Desktop window override | 540×960 | 1280×720 |
| Handheld orientation | portrait or sensor portrait | landscape or sensor landscape |

One canvas unit is one pixel on the most common phone width and monitor, so art drawn at canvas
size never needs upscaling. The window override fits a 1080p monitor with its title bar and
taskbar. Phones run fullscreen, so the window only sets desktop launches.

## Runtime public configuration

React applications can fetch their Clerk publishable key from the backend at runtime. Declare the supported public bootstrap DTO:

```toml
[components.app.runtime_public_bootstrap]
path = "/app/v1/public-config"
field = "clerk_publishable_key"
```

This mode rejects compiler public-key carriers, including inherited compiler inputs. Preview requires the protected `Preview` environment, frontend API base `/api`, one explicit app-host `/api/*` route that strips the prefix, and a backend with protected environment-file aliases and no inline Clerk keys. Development-tenant, issuer/JWKS, origin and secret checks also apply. The application owns the anonymous one-field JSON response, frontend bootstrap and behavioral tests; declaration admission alone does not prove the endpoint or a deployment.

## Contribute

The provider's `mise.toml` declares contributor SDKs and its generic Tools dependency. Run `mise run help`, then `mise run setup`, `mise run build` and `mise run test`. The provider also exposes `test:lint`, `test:unit`, `test:security` and `clean`.

Keep reusable recipe behavior here and generic independent primitives in Tools. Project applications, proprietary policy and fleet inventories belong in their owning repositories.

Application delivery uses native Podman builds and one immutable `komizo.release` descriptor. Keep the workload in `deploy/application.toml`, build inputs in `cookbook.toml`, and delivery policy in `deploy/delivery.toml`. Static assets are deterministic authenticated archives; services and one-shot jobs use exact OCI manifests. The closed `.artifacts/release` packet includes `release.json`, `inventory.json`, and only the declared `static/`, `images/`, and `metadata/` members.

Rust CLI recipes can publish independent native companions with `package.extra_file_sources`. Each companion selects a declared component and platform; its exact native bytes and source receipt join the complete matrix proof, checksums and release attestations. The reviewed `caddy` provider also binds the Tools source, Go compiler, module locks and builder before producing a proxy binary.

Godot addon publication selects `registry_auth = "api-key"` or `"oidc"` in its source policy. Generated callers use the matching reusable workflow. API-key publication receives its configured registry key without an OIDC token grant; OIDC publication requests `id-token: write` without an API-key fallback. Both paths retain exact-source validation and immutable publication proofs.

An authentication policy for a Clerk API declares a tracked `native_checker` alongside `provider = "clerk"` and `api = true`. Cookbook runs that product-owned checker with the caller-pinned Bun and fixed `--native` mode, validates its bounded JSON findings and exit status, and composes the shared checks. The checker retains the product's native authentication assertions. Missing or invalid native checkers refuse verification; findings are never filtered by message text.

CLI publication separates archive signing, product acceptance, evidence signing and publication. Product checks run in a fresh job with read-only permissions. Signing and publication jobs run fixed provider code against closed artifact inventories; they do not execute application checks or contributor hooks.

## License

Cookbook is available under the [MIT License](LICENSE). Bundled third-party code retains its own license notices.
