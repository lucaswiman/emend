# Open Questions

## Analysis Precision

- How much field sensitivity is needed before results are credible?
- How should dynamic dispatch be approximated when type information is partial?
- What is the right default treatment for containers, `*args`, and `**kwargs`?

## Performance

- What runtime budget is acceptable for CI on medium and large projects?
- Which facts should be cached in `parse.db`, and at what granularity?

## Explainability

- What witness format is best for agents and humans?
- How should confidence or precision levels be reported?

## Query Surface

- When does YAML become too limiting?
- Should the expert query syntax be Datalog, egglog, or something compiled from a smaller DSL?

## Rewrite Engine

- Is expression-level saturation enough to be useful?
- How should extraction preserve comments, imports, and formatting?
- What cost functions actually match developer expectations?

## Product Strategy

- Does a unified backend produce real leverage, or just conceptual elegance?
- Which features would still be worth building if saturation/rewrite work never ships?
