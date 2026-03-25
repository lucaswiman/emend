# Compliance-Sensitive Value Tracking

## Goal

Reuse the taint engine for domain-specific labels such as:

- `pii`
- `phi`
- `credit_card`
- `auth_token`

## Position In Roadmap

This should follow basic taint analysis, not precede it. Policy tracking is
mostly a higher-level rule layer once labeled flow works well.

## Model

Treat compliance as:

- labeled sources
- forbidden sinks
- required sanitizers or transforms
- optional audit-only reporting

## Configuration Shape

Add policy-focused configuration on top of `taint`:

- `policies.<label>.forbidden_sinks`
- `policies.<label>.required_sanitizers`
- `policies.<label>.required_transforms`
- `policies.<label>.audit`

## Commands

```bash
emend taint --policy pii
emend taint --policy pii,phi
emend taint --audit --label phi
```

## Important Constraint

Do not let policy syntax grow into a second independent rule engine. The same
underlying labeled-flow model should drive taint, compliance, and path queries.

## Deferred

- specialized compliance report templates
- framework-specific regulated-data presets
- policy packs distributed separately from core emend
