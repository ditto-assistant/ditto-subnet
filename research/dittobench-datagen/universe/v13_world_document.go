package universe

import (
	"context"
	"fmt"
	"sort"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

type v13WorldDocument struct {
	request V13FactDocumentRequest
	ids     []string
}

// Ordinary documents use only typed world fields. The compiler intentionally
// does not accept source prose, computed answers or validator distractors.
func (w World) v13OrdinaryDocuments() ([]v13WorldDocument, error) {
	var docs []v13WorldDocument
	var current v13WorldDocument
	reset := func(domain string) {
		current = v13WorldDocument{request: V13FactDocumentRequest{Revision: V13FactDocumentRevision, Domain: domain, Bindings: map[string]string{}}}
	}
	add := func(id, kind, relation string, values map[string]string) {
		roles := make([]string, 0, len(values))
		for role := range values {
			roles = append(roles, role)
		}
		sort.Strings(roles)
		args := map[string]string{}
		for _, role := range roles {
			token := fmt.Sprintf("{{value%d}}", len(current.request.Bindings))
			current.request.Bindings[token] = values[role]
			args[role] = token
		}
		current.ids = append(current.ids, id)
		current.request.Records = append(current.request.Records, V13FactDocumentRecord{MinBytes: 1, MaxBytes: 1400, Assertions: []V13DocumentAssertion{{Kind: kind, Relation: relation, Arguments: args}}})
	}
	finish := func() error {
		if err := ValidateV13FactDocumentRequest(current.request); err != nil {
			return err
		}
		docs = append(docs, current)
		return nil
	}
	for _, p := range w.People {
		reset("person")
		add(p.IdentityPairID, "identity", "The named person has this relationship to the narrator and is also known by this nickname. These identify the same person.", map[string]string{"name": p.Name, "nickname": p.Nickname, "relationship": p.Relation})
		add(p.WorkPairID, "employment", "Previous employer is historical. Current employer, role and city describe current employment; event_context explains the work connection. Do not invent addresses or nicknames.", map[string]string{"name": p.Name, "previous_employer": p.PreviousEmployer, "current_employer": p.Employer, "role": p.Role, "city": p.City, "event_context": p.Context})
		add(p.EmailPairID, "original_contact", "Historical work email at the previous employer. It is not necessarily current. Preserve the employer-only join; do not add person name, nickname, relationship or event context.", map[string]string{"previous_employer": p.PreviousEmployer, "previous_email": p.PreviousEmail})
		add(p.CorrectionPairID, "contact_replacement", "After the nickname's move to the current employer, this is their new work email, superseding the earlier employer's address. Do not copy the old address into this record.", map[string]string{"nickname": p.Nickname, "current_employer": p.Employer, "current_email": p.Email})
		add(p.ToolNotePairID, "disposable_receipt", "A completed contact-maintenance receipt after event_context: stale address reconciliation is finished. This receipt may be deleted once reviewed; canonical evidence must not be deleted.", map[string]string{"nickname": p.Nickname, "event_context": p.Context})
		if err := finish(); err != nil {
			return nil, err
		}
	}
	for _, p := range w.Projects {
		if p.Lead < 0 || p.Lead >= len(w.People) {
			return nil, fmt.Errorf("fact world: invalid project owner")
		}
		reset("project")
		add(p.ContextPairID, "project_identity", "Alias and formal name identify the same project for the client. Owner is the internal person responsible, vendor is a separate entity, and reference identifies its accounts-payable record. Preserve these distinct roles.", map[string]string{"alias": p.Alias, "formal_name": p.Name, "client": p.Client, "owner": w.People[p.Lead].Name, "vendor": p.Vendor, "reference": p.RecordID})
		add(p.LedgerPairID, "original_invoice", "At this accounts-payable reference, original_invoice is the original billed amount and paid is already paid against it. Do not calculate an outstanding total or add project aliases.", map[string]string{"reference": p.RecordID, "original_invoice": money(p.OriginalCents), "paid": money(p.PaidCents)})
		add(p.CorrectionPairID, "invoice_replacement", "Approved invoice replaces original_invoice for this reference. The partial payment is unchanged. This replaces the billed total, not the amount outstanding or an extra payment. Do not calculate a balance.", map[string]string{"reference": p.RecordID, "approved_invoice": money(p.CorrectedCents), "original_invoice": money(p.OriginalCents)})
		add(p.ToolNotePairID, "scheduling_scratchpad", "This is a disposable handoff scratchpad for scheduling details about the project alias at the client. It is distinct from canonical identity and ownership evidence; updates belong here, not in that evidence.", map[string]string{"alias": p.Alias, "client": p.Client})
		if err := finish(); err != nil {
			return nil, err
		}
	}
	for _, trip := range w.Trips {
		if trip.Companion < 0 || trip.Companion >= len(w.People) {
			return nil, fmt.Errorf("fact world: invalid trip companion")
		}
		companion := w.People[trip.Companion]
		reset("trip")
		add(trip.ContextPairID, "trip_identity", "The informal alias refers to the trip with purpose and time period, planned together with this companion and relationship. The three countries are successive legs, not three separate trips.", map[string]string{"alias": trip.Alias, "purpose": trip.Purpose, "period": trip.When, "companion": companion.Nickname, "relationship": companion.Relation, "country_first": trip.Countries[0], "country_second": trip.Countries[1], "country_third": trip.Countries[2]})
		add(trip.PlanPairID, "original_itinerary", "Original plan made with the companion: first, second and third legs occur in that order, with each days value belonging only to its corresponding country. Never calculate the total duration.", map[string]string{"companion": companion.Nickname, "country_first": trip.Countries[0], "days_first": fmt.Sprint(trip.OldLegDays[0]), "country_second": trip.Countries[1], "days_second": fmt.Sprint(trip.OldLegDays[1]), "country_third": trip.Countries[2], "days_third": fmt.Sprint(trip.OldLegDays[2])})
		changed := -1
		for i := range trip.LegDays {
			if trip.LegDays[i] != trip.OldLegDays[i] {
				if changed >= 0 {
					return nil, fmt.Errorf("fact world: unsupported multi-leg correction")
				}
				changed = i
			}
		}
		if changed < 0 {
			return nil, fmt.Errorf("fact world: missing itinerary correction")
		}
		delta := trip.LegDays[changed] - trip.OldLegDays[changed]
		relation := "Current plan adds the specified number of days to the stated country's original stay. The other two stays are unchanged. Do not calculate the new stay or whole-trip total."
		if delta < 0 {
			delta = -delta
			relation = "Current plan cuts the specified number of days from the stated country's original stay. The other two stays are unchanged. Do not calculate the new stay or whole-trip total."
		}
		add(trip.CorrectionPairID, "itinerary_delta", relation, map[string]string{"companion": companion.Nickname, "country": trip.Countries[changed], "days_delta": fmt.Sprint(delta)})
		if err := finish(); err != nil {
			return nil, err
		}
	}
	for _, p := range w.Preferences {
		reset("preference")
		add(p.PairID, "user_preference", "The narrator's own Ditto interface preference, not client branding. Client palettes, fonts or project materials must not override it. Do not introduce rejected alternatives or historical preferences.", map[string]string{"setting": p.Domain, "preferred_value": p.Value})
		if err := finish(); err != nil {
			return nil, err
		}
	}
	if len(w.People) < 2 {
		return nil, fmt.Errorf("fact world: canary owners missing")
	}
	reset("registration_ownership")
	add(w.Integrity.CanaryPairIDs[0], "narrator_registration", "This code belongs to the narrator's OWN attendee registration for the event. It is not a colleague's crew code or vendor badge.", map[string]string{"event_context": w.People[0].Context, "attendee_code": w.Integrity.CanaryNonce})
	add(w.Integrity.CanaryPairIDs[1], "colleague_registration", "This crew check-in code belongs to the named colleague at the same event, not to the narrator. The narrator is only keeping it for them.", map[string]string{"colleague": w.People[0].Name, "crew_code": w.Integrity.CanaryBaits[0]})
	add(w.Integrity.CanaryPairIDs[2], "vendor_registration", "This vendor badge code belongs to the named person, not the narrator or either attendee registration. Preserve ownership and badge role.", map[string]string{"badge_owner": w.People[1].Name, "badge_code": w.Integrity.CanaryBaits[1]})
	if err := finish(); err != nil {
		return nil, err
	}
	if w.Probes == nil {
		return nil, fmt.Errorf("fact world: probe state required")
	}
	personFor := func(index int) (Person, error) {
		if index < 0 || index >= len(w.People) {
			return Person{}, fmt.Errorf("fact world: invalid probe person")
		}
		return w.People[index], nil
	}
	for _, probe := range w.Probes.NearMiss {
		p, err := personFor(probe.Person)
		if err != nil {
			return nil, err
		}
		reset("colleague_introduction")
		add(probe.MentionPairID, "distinct_colleague", "The narrator met the colleague through the known person's nickname at their shared employer, in the event context. These are two distinct people; no address or contact identifier is given for the colleague. Do not imply they share contact details or explicitly answer a later contact question.", map[string]string{"known_person": p.Nickname, "colleague": probe.Name, "employer": p.Employer, "event_context": p.Context})
		if err := finish(); err != nil {
			return nil, err
		}
	}
	for _, probe := range w.Probes.Handles {
		p, err := personFor(probe.Person)
		if err != nil {
			return nil, err
		}
		reset("messaging_contact")
		add(probe.HandlePairID, "signal_identity", "The person identified by nickname goes by the Signal identifier. Messaging there gets a quick response. Do not mention the event context or use the question's words handle or reach; express the Signal identity naturally. This is the originally supplied contact, not a claim it survives every subsequent update.", map[string]string{"nickname": p.Nickname, "signal_identifier": probe.Handle})
		if probe.Removed {
			add(probe.RemovalPairID, "withdrawn_account", "Later completed withdrawal: this person's Signal account was closed and the previously saved identifier must be removed, not handed out again. No replacement contact is supplied. Refer to the earlier account without repeating its identifier.", map[string]string{"nickname": p.Nickname})
		} else if probe.RemovalPairID != "" {
			return nil, fmt.Errorf("fact world: unexpected handle removal")
		}
		if err := finish(); err != nil {
			return nil, err
		}
	}
	for _, probe := range w.Probes.Threads {
		reset("incomplete_payment")
		add(probe.ApprovalPairID, "approved_invoice", "Invoice for this thread and vendor is approved at the stated amount. Payment details are expected from a bank export but not yet supplied. This is an approved invoice, not a balance or payment.", map[string]string{"alias": probe.Alias, "vendor": probe.Vendor, "invoice": probe.InvoiceID, "approved_amount": money(probe.ApprovedCents)})
		add(probe.PaymentPairID, "payment_without_amount", "A partial payment was sent last week against the invoice, but its amount exists only in a bank export not supplied here. The invoice is not settled. Do not invent a number, fraction, remaining balance or payment date.", map[string]string{"alias": probe.Alias, "vendor": probe.Vendor, "invoice": probe.InvoiceID})
		if err := finish(); err != nil {
			return nil, err
		}
	}
	return docs, nil
}

// RenderV13FactOrdinaryWorld preserves pair identity and stages, and commits
// no prompt changes unless all generated documents pass independent checks.
func (w *World) RenderV13FactOrdinaryWorld(ctx context.Context, renderer V13FactDocumentRenderer) error {
	if w == nil || w.BenchVersion != protocol.BenchVersionV13 || renderer == nil {
		return fmt.Errorf("fact world: V13 world and renderer required")
	}
	docs, err := w.v13OrdinaryDocuments()
	if err != nil {
		return err
	}
	indices := map[string]int{}
	for i, pair := range w.Pairs {
		if _, ok := indices[pair.PairID]; ok {
			return fmt.Errorf("fact world: duplicate pair identity")
		}
		indices[pair.PairID] = i
	}
	seen := map[string]bool{}
	for _, doc := range docs {
		for _, id := range doc.ids {
			if _, ok := indices[id]; !ok || seen[id] {
				return fmt.Errorf("fact world: missing or repeated pair identity")
			}
			seen[id] = true
		}
	}
	probeIDs := map[string]bool{}
	for _, p := range w.Probes.Pairs {
		if _, ok := indices[p.PairID]; !ok || !seen[p.PairID] || probeIDs[p.PairID] {
			return fmt.Errorf("fact world: invalid probe projection")
		}
		probeIDs[p.PairID] = true
	}
	pairs := append([]protocol.MemoryPair(nil), w.Pairs...)
	for _, doc := range docs {
		p, err := RenderV13FactDocument(ctx, doc.request, renderer)
		if err != nil {
			return err
		}
		for i, id := range doc.ids {
			pairs[indices[id]].Prompt = p.Records[i]
		}
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	// Keep the secondary probe projection aligned without mutating shared
	// state until the entire operation has succeeded.
	probes := *w.Probes
	probes.Pairs = append([]protocol.MemoryPair(nil), probes.Pairs...)
	for i, p := range probes.Pairs {
		index, ok := indices[p.PairID]
		if !ok {
			return fmt.Errorf("fact world: missing probe projection")
		}
		probes.Pairs[i] = pairs[index]
	}
	w.Pairs = pairs
	w.Probes = &probes
	return nil
}
