# Engineering Rules

## Mission

Rebuild V1 with a completely new architecture.

Behavior compatibility with V1 is mandatory.

---

## Never

- Never redesign the trading strategy.
- Never optimize trading behavior.
- Never change scoring.
- Never change thresholds.
- Never change lifecycle.
- Never invent new features.
- Never introduce unnecessary abstractions.
- Never rewrite completed modules without reason.

- Never infer legacy behavior from assumptions.
- Never treat an unexplained V1↔V2 divergence as acceptable.
- Never introduce implicit cross-module contracts.
- Never "fix" legacy behavior during migration.
- Never add abstractions for hypothetical future use.

---

## Always

- Follow the Software Architecture Specification.
- Preserve V1 behavior.
- Keep modules cohesive.
- One responsibility per module.
- Pure analysis before state mutation.

- Observe legacy behavior instead of guessing.
- Treat every V1↔V2 divergence as a defect until proven behaviorally equivalent.
- Express every cross-module dependency through explicit data contracts.
- Preserve questionable legacy behavior first, document concerns separately, and improve only after migration.
- Introduce abstractions only when a second real implementation justifies them (unless explicitly defined by the Architecture).
- Freeze completed, validated modules unless a new requirement genuinely requires reopening them.
- Revalidate any reopened module against the golden dataset before continuing.
- One milestone at a time.

---

When these rules conflict with convenience, the rules win.

The purpose of these rules is not to maximize implementation speed, but to maximize behavioral correctness and long-term maintainability.