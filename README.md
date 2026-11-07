<!-- Generated from private documentation source. Do not edit directly. Source SHA256: a2d13db3f875e4c66d5574737f2774d8d094efed0aa9300cddc747f33780758a -->

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

## Contribute

The provider's `mise.toml` declares contributor SDKs and its generic Tools dependency. Run `mise run help`, then `mise run setup`, `mise run build` and `mise run test`. The provider also exposes `test:lint`, `test:unit`, `test:security` and `clean`.

Keep reusable recipe behavior here and generic independent primitives in Tools. Project applications, proprietary policy and fleet inventories belong in their owning repositories.
