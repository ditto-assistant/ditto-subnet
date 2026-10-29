package inference

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"strconv"
)

type openRouterSessionContextKey struct{}

// withOpenRouterSession binds optional cache affinity to a trusted lease
// generation. It never consumes a caller's session ID or reuses a completion.
func withOpenRouterSession(ctx context.Context, enabled bool, grantID string, generation int64, model string) context.Context {
	if !enabled {
		return ctx
	}
	digest := sha256.Sum256([]byte("ditto-inference-session/v1\x00" + grantID + "\x00" + strconv.FormatInt(generation, 10) + "\x00" + model))
	return context.WithValue(ctx, openRouterSessionContextKey{}, "ditto-"+hex.EncodeToString(digest[:]))
}

func attachOpenRouterSessionHeader(ctx context.Context, headers map[string]string) {
	if session, ok := ctx.Value(openRouterSessionContextKey{}).(string); ok {
		headers["X-Session-Id"] = session
	}
}
