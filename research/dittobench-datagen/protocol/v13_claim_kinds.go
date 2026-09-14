package protocol

// Claim.Kind values populated by the v13 case-family generators (business and
// personal programs, family compiler v2, the injection tail). Defined once
// here so the generators and the v13 grader branch name each matcher
// identically.
const (
	ClaimKindPerson       = "person"
	ClaimKindStatus       = "status"
	ClaimKindEvent        = "event"
	ClaimKindOrganisation = "organisation"
	ClaimKindAction       = "action"
	ClaimKindChannel      = "channel"
	ClaimKindDate         = "date"
	ClaimKindTime         = "time"
	ClaimKindSetMember    = "set_member"
	ClaimKindConflict     = "conflict"
	ClaimKindQuantity     = "quantity"
	ClaimKindDirection    = "direction"
	// ClaimKindValue is an opaque identifier quoted from the evidence (a
	// statement reference, a code): graded by exact canonical match, never by
	// a synonym cluster.
	ClaimKindValue = "value"
)

// Metamorphic relations carried by V10CaseProvenance.Relation (universe) and,
// for v13, mirrored onto CaseScore.Relation by the scorer post-pass. Defined
// once here so every layer names them identically.
const (
	RelationBase                 = "base"
	RelationRendererInvariant    = "renderer_invariant"
	RelationDistractorInvariant  = "distractor_invariant"
	RelationCausalCounterfactual = "causal_counterfactual"
)
