package codingcontract

import (
	"errors"
	"github.com/google/uuid"
)

const HostedHarnessVersion = 2

// HostedSeedRequest and HostedRunRequest preserve the bounded miner projection,
// but select v2 explicitly. Legacy request validators continue to accept only v1.
type HostedSeedRequest SeedRequest
type HostedRunRequest RunRequest

func (request HostedSeedRequest) Validate() error {
	if err := ValidateHostedHarnessIdentity(request.TicketID, request.CaseID); err != nil {
		return err
	}
	return SeedRequest(request).validateVersion(HostedHarnessVersion)
}

func (request HostedRunRequest) Validate() error {
	if err := ValidateHostedHarnessIdentity(request.TicketID, request.CaseID); err != nil {
		return err
	}
	if request.Budgets.WallTimeSeconds > 3600 {
		return errors.New("hosted run exceeds its maximum lifetime")
	}
	return RunRequest(request).validateVersion(HostedHarnessVersion)
}

// ValidateHostedHarnessIdentity validates opaque identifiers, not permission to run.
func ValidateHostedHarnessIdentity(evaluation, attempt string) error {
	for _, value := range []string{evaluation, attempt} {
		id, err := uuid.Parse(value)
		if err != nil || id == uuid.Nil || id.String() != value {
			return errors.New("hosted harness identity is invalid")
		}
	}
	return nil
}
