# ServerFS public product contracts

The [Platform Independence ADR](../docs/architecture/v0.27.0-platform-independence.md) and [development plan](../docs/plans/dev_plan_v0.27.0.md) govern this directory.

This is a product-contract and acceptance boundary, not a runtime package. Platform implementations must build and run without importing or copying code from here. Cross-platform conformance tests may consume these data and interact with a platform only through its public interfaces; do not import platform internals in a common test runner.

- [v0.26.0 baseline](v0.26.0/README.md): frozen behavior, capability surface and provenance for the split-first migration.
- [v0.27.0 acceptance and API deltas](v0.27.0/acceptance-and-api-deltas.md): Gate A matrix and additive feature contract draft; finalize before independent feature streams.
- [Maintainer invariants](../docs/maintainers/invariants.md): durable security and behavior rules, including existing deployment limits.
- [Phase 0 inventory](../docs/diagnostics/v0270-phase0-baseline.md): current source/dependency/migration evidence and remaining gates.

Add compatible v0.27.0 contract changes separately from the immutable v0.26.0 evidence. Review public schema, security and behavior changes centrally before independent implementation; never equate a schema match with native acceptance.
