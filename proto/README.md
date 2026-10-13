# Vendored protobuf schemas

These `.proto` files are vendored unmodified from
[tronikos/nest_legacy](https://github.com/tronikos/nest_legacy) (`protobuf/`),
licensed under the Apache License 2.0 (see `LICENSE` in this directory).

- Upstream commit: `17b98a8c6930a619ea06b5e302052bea2c374d71` (2026-09-29)
- Modifications: none.

They describe the protobuf API used by the home.nest.com web app, which belongs
to Google. They were chosen over this project's own reverse-engineered schemas
after decoding a real captured Observe stream and write round-trip with both;
see the project plan for the comparison.

## Generation notes

`scripts/gen_protos.py` generates Python code from this tree, with two deliberate
exceptions:

- `google/rpc/status.proto` is used only to resolve imports. It is **not**
  generated, because Home Assistant already ships `googleapis-common-protos`,
  which registers the same file in protobuf's global descriptor pool. A second
  registration risks an import-time conflict.
- `google/rpc/stream_body.proto` **is** generated. No installed package provides it.

To update: replace this tree with a newer upstream `protobuf/`, update the
commit above, and rerun `scripts/gen_protos.py` and the tests.
