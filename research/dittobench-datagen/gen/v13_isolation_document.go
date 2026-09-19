package gen

import "github.com/ditto-assistant/dittobench-datagen/universe"

// Compile the projected secondary person's state, not the original person's
// prose: projection can change both identity and address. Keep the three-row
// join and user scope intact, without importing facts from the primary graph.
func isolationFactDocument(p universe.Person) (universe.V13FactDocumentRequest, error) {
	r := universe.V13FactDocumentRequest{Revision: universe.V13FactDocumentRevision, Domain: "contact", Bindings: map[string]string{
		"{{name0}}": p.Name, "{{nickname0}}": p.Nickname, "{{relation0}}": p.Relation,
		"{{previous0}}": p.PreviousEmployer, "{{employer0}}": p.Employer, "{{role0}}": p.Role,
		"{{city0}}": p.City, "{{event0}}": p.Context, "{{email0}}": p.Email,
	}, Records: []universe.V13FactDocumentRecord{
		{MinBytes: 1, MaxBytes: 1400, Assertions: []universe.V13DocumentAssertion{{Kind: "identity", Relation: "Name and nickname identify the same person, with the stated relationship to the narrator.", Arguments: map[string]string{"name": "{{name0}}", "nickname": "{{nickname0}}", "relationship": "{{relation0}}"}}}},
		{MinBytes: 1, MaxBytes: 1400, Assertions: []universe.V13DocumentAssertion{{Kind: "employment", Relation: "Name previously worked for previous_employer. Their current employer, role and city are stated. Event_context identifies the work connection. Do not invent contact details or nicknames.", Arguments: map[string]string{"name": "{{name0}}", "previous_employer": "{{previous0}}", "current_employer": "{{employer0}}", "role": "{{role0}}", "city": "{{city0}}", "event_context": "{{event0}}"}}}},
		{MinBytes: 1, MaxBytes: 1400, Assertions: []universe.V13DocumentAssertion{{Kind: "contact_replacement", Relation: "Following nickname's move to current_employer, current_email is their new work address. It replaces their old work contact. Do not invent or repeat a previous email, name or event context.", Arguments: map[string]string{"nickname": "{{nickname0}}", "current_employer": "{{employer0}}", "current_email": "{{email0}}"}}}},
	}}
	return r, universe.ValidateV13FactDocumentRequest(r)
}
