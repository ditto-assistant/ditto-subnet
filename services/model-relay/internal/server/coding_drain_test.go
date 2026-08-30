package server

import (
	"testing"
	"time"

	"github.com/ditto-assistant/model-relay/internal/config"
)

func TestCodingInferenceExtendsGracefulDrainOnlyWhenEnabled(t *testing.T) {
	instance := &Server{cfg: &config.Config{Inference: config.InferenceProxyConfig{TimeoutSeconds: 90}}}
	if got := instance.drainTimeout(); got != 95*time.Second {
		t.Fatalf("ordinary drain=%s", got)
	}
	instance.cfg.Inference.CodingEnabled = true
	if got := instance.drainTimeout(); got != 305*time.Second {
		t.Fatalf("coding drain=%s", got)
	}
}
