package longmemeval

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"hash/crc32"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"strconv"
	"strings"
)

const (
	defaultMetadataTokenURL = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
	defaultSecretManagerURL = "https://secretmanager.googleapis.com"
	maxMetadataResponse     = 4 << 10
	defaultMaxSecretBytes   = 16 << 10
)

var (
	gcpProjectID = regexp.MustCompile(`^(?:[a-z][a-z0-9-]{4,28}[a-z0-9]|[0-9]{6,})$`)
	gcpSecretID  = regexp.MustCompile(`^[A-Za-z0-9_-]{1,255}$`)
	gcpVersion   = regexp.MustCompile(`^[0-9]+$`)
)

// GCPSecretManagerAuthorizerConfig contains only trusted service endpoints and
// size bounds. Provider credential values are never accepted through config,
// flags, or environment variables.
type GCPSecretManagerAuthorizerConfig struct {
	MetadataTokenURL     string
	SecretManagerBaseURL string
	HTTPClient           *http.Client
	MaxSecretBytes       int64
}

// SecretBytesResolver resolves server-owned, immutable Secret Manager values
// for trusted confirmation-runtime keys. The returned slice is owned by the
// caller, must never be logged or serialized, and must be zeroed as soon as the
// runtime has copied or finished using it.
type SecretBytesResolver interface {
	Resolve(context.Context, SecretManagerReference) ([]byte, error)
}

// GCPSecretManagerAuthorizer resolves a server-owned Secret Manager reference
// with the scorer's GCP identity and applies it directly to one provider
// request. The request header holds the credential only until its transport
// completes; the authorizer and runtime configuration never retain a copy.
type GCPSecretManagerAuthorizer struct {
	metadataTokenURL     string
	secretManagerBaseURL string
	client               *http.Client
	maxSecretBytes       int64
}

var _ SecretBytesResolver = (*GCPSecretManagerAuthorizer)(nil)

func NewGCPSecretManagerAuthorizer(config GCPSecretManagerAuthorizerConfig) (*GCPSecretManagerAuthorizer, error) {
	metadataURL := strings.TrimSpace(config.MetadataTokenURL)
	if metadataURL == "" {
		metadataURL = defaultMetadataTokenURL
	}
	secretURL := strings.TrimSpace(config.SecretManagerBaseURL)
	if secretURL == "" {
		secretURL = defaultSecretManagerURL
	}
	if err := validateMetadataEndpoint(metadataURL); err != nil {
		return nil, err
	}
	if err := validateSecretManagerEndpoint(secretURL); err != nil {
		return nil, err
	}
	maximum := config.MaxSecretBytes
	if maximum == 0 {
		maximum = defaultMaxSecretBytes
	}
	if maximum < 1 || maximum > 1<<20 {
		return nil, errors.New("Secret Manager credential size bound is invalid")
	}
	client := &http.Client{}
	if config.HTTPClient != nil {
		*client = *config.HTTPClient
	}
	client.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
	return &GCPSecretManagerAuthorizer{
		metadataTokenURL: metadataURL, secretManagerBaseURL: strings.TrimRight(secretURL, "/"),
		client: client, maxSecretBytes: maximum,
	}, nil
}

func validateMetadataEndpoint(raw string) error {
	parsed, err := url.Parse(raw)
	if err != nil || parsed.User != nil || parsed.RawQuery != "" || parsed.Fragment != "" ||
		parsed.Path != "/computeMetadata/v1/instance/service-accounts/default/token" {
		return errors.New("GCP metadata token endpoint is invalid")
	}
	if parsed.Scheme == "http" && (strings.EqualFold(parsed.Hostname(), "metadata.google.internal") || isLoopbackHost(parsed.Hostname())) {
		return nil
	}
	return errors.New("GCP metadata token endpoint has an untrusted origin")
}

func validateSecretManagerEndpoint(raw string) error {
	parsed, err := url.Parse(raw)
	if err != nil || parsed.User != nil || parsed.RawQuery != "" || parsed.Fragment != "" ||
		(parsed.Path != "" && parsed.Path != "/") {
		return errors.New("Secret Manager endpoint is invalid")
	}
	if parsed.Scheme == "https" && strings.EqualFold(parsed.Hostname(), "secretmanager.googleapis.com") {
		return nil
	}
	if parsed.Scheme == "http" && isLoopbackHost(parsed.Hostname()) {
		return nil
	}
	return errors.New("Secret Manager endpoint has an untrusted origin")
}

func validateSecretReference(reference SecretManagerReference) error {
	if err := reference.validate(); err != nil {
		return err
	}
	version, versionErr := strconv.ParseUint(reference.Version, 10, 64)
	if !gcpProjectID.MatchString(reference.ProjectID) || !gcpSecretID.MatchString(reference.SecretID) ||
		!gcpVersion.MatchString(reference.Version) {
		return errors.New("provider credential reference must name an immutable Secret Manager version")
	}
	if versionErr != nil || version == 0 {
		return errors.New("provider credential reference must name a positive Secret Manager version")
	}
	return nil
}

// ValidateSecretManagerReference performs the same immutable-reference check
// used by Resolve without contacting metadata or Secret Manager. The generic
// error intentionally omits every reference component.
func ValidateSecretManagerReference(reference SecretManagerReference) error {
	if err := validateSecretReference(reference); err != nil {
		return errors.New("Secret Manager reference is invalid")
	}
	return nil
}

func (a *GCPSecretManagerAuthorizer) Authorize(
	ctx context.Context,
	reference SecretManagerReference,
	request *http.Request,
) error {
	if request == nil {
		return errors.New("Secret Manager authorizer is unconfigured")
	}
	credential, err := a.Resolve(ctx, reference)
	if err != nil {
		return errors.New("provider credential is unavailable from Secret Manager")
	}
	if request.Header == nil {
		request.Header = make(http.Header)
	}
	request.Header.Set("Authorization", "Bearer "+string(credential))
	zeroBytes(credential)
	return nil
}

// Resolve implements SecretBytesResolver. It deliberately masks validation,
// identity, transport, and CRC failures behind one message so neither secret
// references nor upstream bodies can escape through caller logs. It performs
// no caching; each successful call returns a fresh caller-owned slice.
func (a *GCPSecretManagerAuthorizer) Resolve(
	ctx context.Context,
	reference SecretManagerReference,
) ([]byte, error) {
	if a == nil || ctx == nil {
		return nil, errors.New("Secret Manager value is unavailable")
	}
	if err := ValidateSecretManagerReference(reference); err != nil {
		return nil, errors.New("Secret Manager value is unavailable")
	}
	accessToken, err := a.metadataToken(ctx)
	if err != nil {
		return nil, errors.New("Secret Manager value is unavailable")
	}
	value, err := a.accessSecret(ctx, reference, accessToken)
	if err != nil {
		return nil, errors.New("Secret Manager value is unavailable")
	}
	return value, nil
}

// ZeroSecretBytes overwrites caller-owned secret material in place. Callers
// should defer it immediately after a successful Resolve.
func ZeroSecretBytes(value []byte) {
	zeroBytes(value)
}

func (a *GCPSecretManagerAuthorizer) metadataToken(ctx context.Context) (string, error) {
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, a.metadataTokenURL, nil)
	if err != nil {
		return "", err
	}
	request.Header.Set("Metadata-Flavor", "Google")
	request.Header.Set("Accept", "application/json")
	response, err := a.client.Do(request)
	if err != nil {
		return "", err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK || response.Header.Get("Metadata-Flavor") != "Google" {
		_, _ = io.Copy(io.Discard, io.LimitReader(response.Body, maxMetadataResponse))
		return "", errors.New("metadata service rejected token request")
	}
	raw, err := io.ReadAll(io.LimitReader(response.Body, maxMetadataResponse+1))
	if err != nil || len(raw) == 0 || len(raw) > maxMetadataResponse {
		return "", errors.New("metadata token response is invalid")
	}
	defer zeroBytes(raw)
	var wire struct {
		AccessToken string `json:"access_token"`
		ExpiresIn   int64  `json:"expires_in"`
		TokenType   string `json:"token_type"`
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&wire); err != nil || strings.TrimSpace(wire.AccessToken) == "" ||
		wire.ExpiresIn <= 0 || !strings.EqualFold(wire.TokenType, "Bearer") ||
		strings.ContainsAny(wire.AccessToken, "\r\n\x00") {
		return "", errors.New("metadata token response is incomplete")
	}
	if err := decoder.Decode(&struct{}{}); err != io.EOF {
		return "", errors.New("metadata token response contains trailing JSON")
	}
	return wire.AccessToken, nil
}

func (a *GCPSecretManagerAuthorizer) accessSecret(
	ctx context.Context,
	reference SecretManagerReference,
	accessToken string,
) ([]byte, error) {
	resource := fmt.Sprintf("projects/%s/secrets/%s/versions/%s",
		url.PathEscape(reference.ProjectID), url.PathEscape(reference.SecretID), url.PathEscape(reference.Version))
	endpoint := a.secretManagerBaseURL + "/v1/" + resource + ":access"
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
	if err != nil {
		return nil, err
	}
	request.Header.Set("Authorization", "Bearer "+accessToken)
	request.Header.Set("Accept", "application/json")
	response, err := a.client.Do(request)
	if err != nil {
		return nil, err
	}
	defer response.Body.Close()
	maximumEncoded := base64.StdEncoding.EncodedLen(int(a.maxSecretBytes))
	maximumBody := int64(maximumEncoded + 2048)
	if response.StatusCode != http.StatusOK {
		_, _ = io.Copy(io.Discard, io.LimitReader(response.Body, maximumBody))
		return nil, errors.New("Secret Manager rejected access request")
	}
	raw, err := io.ReadAll(io.LimitReader(response.Body, maximumBody+1))
	if err != nil || len(raw) == 0 || int64(len(raw)) > maximumBody {
		return nil, errors.New("Secret Manager response is invalid")
	}
	defer zeroBytes(raw)
	var wire struct {
		Name    string `json:"name"`
		Payload *struct {
			Data       string `json:"data"`
			DataCRC32C string `json:"dataCrc32c"`
		} `json:"payload"`
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&wire); err != nil || !matchesSecretVersionName(wire.Name, reference) || wire.Payload == nil ||
		wire.Payload.Data == "" || wire.Payload.DataCRC32C == "" {
		return nil, errors.New("Secret Manager response identity or payload is incomplete")
	}
	if err := decoder.Decode(&struct{}{}); err != io.EOF {
		return nil, errors.New("Secret Manager response contains trailing JSON")
	}
	credential, err := base64.StdEncoding.Strict().DecodeString(wire.Payload.Data)
	if err != nil || len(credential) == 0 || int64(len(credential)) > a.maxSecretBytes ||
		bytes.IndexAny(credential, "\r\n\x00") >= 0 {
		zeroBytes(credential)
		return nil, errors.New("Secret Manager credential payload is invalid")
	}
	wantCRC, err := strconv.ParseUint(wire.Payload.DataCRC32C, 10, 32)
	if err != nil || uint64(crc32.Checksum(credential, crc32.MakeTable(crc32.Castagnoli))) != wantCRC {
		zeroBytes(credential)
		return nil, errors.New("Secret Manager credential payload failed CRC32C validation")
	}
	return credential, nil
}

func matchesSecretVersionName(name string, reference SecretManagerReference) bool {
	parts := strings.Split(name, "/")
	if len(parts) != 6 || parts[0] != "projects" || parts[2] != "secrets" || parts[4] != "versions" ||
		parts[3] != reference.SecretID || parts[5] != reference.Version {
		return false
	}
	if parts[1] == reference.ProjectID {
		return true
	}
	// Secret Manager may canonicalize a textual project ID to its numeric
	// project number in AccessSecretVersion responses. Numeric input remains
	// exact; only textual input accepts an all-digit canonical project number.
	return !gcpVersion.MatchString(reference.ProjectID) && gcpVersion.MatchString(parts[1])
}

func zeroBytes(value []byte) {
	for index := range value {
		value[index] = 0
	}
}
