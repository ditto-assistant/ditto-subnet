package postgres

import (
	"os"
	"strings"
	"testing"
)

func TestSchemaGeneratorNeverAdmitsBootstrapSocketServer(t *testing.T) {
	body, err := os.ReadFile("../../scripts/gen-schema.sh")
	if err != nil {
		t.Fatal(err)
	}
	script := string(body)
	if !strings.Contains(script, `ADMIN_HOST="127.0.0.1"`) {
		t.Fatal("schema generator does not pin the final TCP listener")
	}
	if count := strings.Count(script, `-h "${ADMIN_HOST}"`); count != 3 {
		t.Fatalf("TCP-pinned readiness/admin commands = %d, want 3", count)
	}
	if strings.Contains(script, `pg_isready -U`) || strings.Contains(script, `psql -v ON_ERROR_STOP=1 \
    -U`) {
		t.Fatal("schema generator can still connect to the temporary socket postmaster")
	}
}
