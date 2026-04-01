# Manual Testing

This directory holds internal manual-testing guidance for roadmap work that is
too integration-heavy to rely only on unit tests.

The intent is not to replace automated coverage. The intent is to keep a small,
repeatable set of real command executions that can be run:

- before a migration phase starts
- during parity work between two engines
- before changing the public engine for a feature
- after cleanup removes the old path

## Files

- `trace-pipeline.md`
  Manual testing plan for `emend trace`, including self-hosting runs against
  `emend` itself and recommended external comparison targets.

## Usage

When a roadmap phase says to do manual testing, link to the relevant file in
this directory and update that file with:

- exact commands
- expected outcomes
- notable performance caveats
- known acceptable divergences
- real findings or failure modes observed during the phase
