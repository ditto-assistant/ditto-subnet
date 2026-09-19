# Personal fact-world migration

`GenerateV13PersonalFactPrograms(worldSeed, presentationSeed, count)` covers
all seven personal domains using the shared typed-fact evaluator. It remains
opt-in while the rest of the private pipeline is being migrated.

- Household responsibility and hobby organisers use explicit histories.
- Appointments and travel preserve day/minute grading units and calendar
  anchors. A changed booking is an update to a date/time value, not a change
  in record arrival order.
- Milestone status and subscription actions retain their associated original
  date/renewal facts without letting those unrelated values answer the query.
- School lists are initial members plus one validated removal and addition.
  Removing an absent member, adding an existing member, duplicate initial
  membership, or ambiguous edit ordering is rejected. Prose states these are
  completed updates and explicitly retains unaffected items.
- Every domain includes a typed unrelated household fact, and its distractor
  belongs to a different entity. Record and presentation order cannot change
  the answer.

Tests compare all seven domains over 100 seeds and six presentations against
the seeded answers, list items, claim kinds/units/weights, query programs and
distractors. Each causal mutation changes exactly one fact and one record.
New wording does not change default V13 generation or any older version.

This is not full private qualification. Next must include independent
semantic/honest/adversary evaluation on exact rendered slices, then migration
of remaining surfaces and atomic producer/decoder integration. A finite
template family is not, by itself, a shortcut-resistance guarantee.
