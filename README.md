<!-- Generated from private documentation source. Do not edit directly. Source SHA256: 32bee05c77f3a5dbce7e6bc9e1c96d81e9bfcdc96976b8637151fed7467190bf -->

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

CLI publication separates archive signing, product acceptance, evidence signing and publication. Product checks run in a fresh job with read-only permissions. Signing and publication jobs run fixed provider code against closed artifact inventories; they do not execute application checks or contributor hooks.
