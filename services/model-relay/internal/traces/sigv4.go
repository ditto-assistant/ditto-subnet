package traces

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"net/url"
	"sort"
	"strconv"
	"strings"
	"time"
)

// presignCredentials is what query-string SigV4 needs to sign a URL.
type presignCredentials struct {
	AccessKeyID     string
	SecretAccessKey string
	Region          string
}

// presignURL returns a SigV4 query-authenticated URL for method on target,
// valid for expires, using UNSIGNED-PAYLOAD and signing only the Host header.
//
// Query-string auth rather than header auth on purpose: Hippius rejects
// SDK-direct (header-signed) PutObject/GetObject with SignatureDoesNotMatch
// while accepting presigned URLs from the same credentials (apps/platform
// hippius.py, ditto-assistant/backend#1078/#1088). Backblaze B2 and AWS
// accept both, so presign is the one shape that works everywhere.
//
// Only the host is signed so the PUT may carry Content-Type and
// Content-Length without them participating in the signature.
func presignURL(method string, target *url.URL, creds presignCredentials, now time.Time, expires time.Duration) string {
	now = now.UTC()
	amzDate := now.Format("20060102T150405Z")
	dateStamp := now.Format("20060102")
	scope := dateStamp + "/" + creds.Region + "/s3/aws4_request"

	query := url.Values{}
	for k, vs := range target.Query() {
		for _, v := range vs {
			query.Add(k, v)
		}
	}
	query.Set("X-Amz-Algorithm", "AWS4-HMAC-SHA256")
	query.Set("X-Amz-Credential", creds.AccessKeyID+"/"+scope)
	query.Set("X-Amz-Date", amzDate)
	query.Set("X-Amz-Expires", strconv.Itoa(int(expires/time.Second)))
	query.Set("X-Amz-SignedHeaders", "host")

	canonicalQuery := canonicalQueryString(query)
	canonicalURI := canonicalPath(target.EscapedPath())
	host := strings.ToLower(target.Host)
	canonicalRequest := strings.Join([]string{
		method,
		canonicalURI,
		canonicalQuery,
		"host:" + host + "\n",
		"host",
		"UNSIGNED-PAYLOAD",
	}, "\n")
	stringToSign := strings.Join([]string{
		"AWS4-HMAC-SHA256",
		amzDate,
		scope,
		hexSHA256([]byte(canonicalRequest)),
	}, "\n")
	signingKey := hmacSHA256(hmacSHA256(hmacSHA256(hmacSHA256(
		[]byte("AWS4"+creds.SecretAccessKey), []byte(dateStamp)),
		[]byte(creds.Region)), []byte("s3")), []byte("aws4_request"))
	signature := hex.EncodeToString(hmacSHA256(signingKey, []byte(stringToSign)))

	signed := *target
	signed.RawQuery = canonicalQuery + "&X-Amz-Signature=" + signature
	signed.RawPath = canonicalURI
	signed.Path = target.Path
	return signed.String()
}

// canonicalQueryString sorts by key then value and RFC 3986-encodes both,
// which is the SigV4 canonical form (space is %20, not +).
func canonicalQueryString(values url.Values) string {
	type pair struct{ k, v string }
	var pairs []pair
	for k, vs := range values {
		for _, v := range vs {
			pairs = append(pairs, pair{uriEncode(k, true), uriEncode(v, true)})
		}
	}
	sort.Slice(pairs, func(i, j int) bool {
		if pairs[i].k != pairs[j].k {
			return pairs[i].k < pairs[j].k
		}
		return pairs[i].v < pairs[j].v
	})
	parts := make([]string, 0, len(pairs))
	for _, p := range pairs {
		parts = append(parts, p.k+"="+p.v)
	}
	return strings.Join(parts, "&")
}

// canonicalPath re-encodes every segment of an already-escaped path the SigV4
// way (S3 does NOT double-encode: each segment is encoded exactly once, and
// '/' is preserved). An empty path is "/".
func canonicalPath(escapedPath string) string {
	if escapedPath == "" {
		return "/"
	}
	segments := strings.Split(escapedPath, "/")
	for i, seg := range segments {
		decoded, err := url.PathUnescape(seg)
		if err != nil {
			decoded = seg
		}
		segments[i] = uriEncode(decoded, false)
	}
	return strings.Join(segments, "/")
}

// uriEncode is the SigV4 URI encoder: unreserved characters pass, everything
// else is %XX upper-case; '/' is encoded only when encodeSlash is set.
func uriEncode(s string, encodeSlash bool) string {
	const hexDigits = "0123456789ABCDEF"
	var b strings.Builder
	b.Grow(len(s))
	for i := 0; i < len(s); i++ {
		c := s[i]
		switch {
		case c >= 'A' && c <= 'Z', c >= 'a' && c <= 'z', c >= '0' && c <= '9',
			c == '-', c == '_', c == '.', c == '~':
			b.WriteByte(c)
		case c == '/' && !encodeSlash:
			b.WriteByte(c)
		default:
			b.WriteByte('%')
			b.WriteByte(hexDigits[c>>4])
			b.WriteByte(hexDigits[c&0xF])
		}
	}
	return b.String()
}

func hmacSHA256(key, data []byte) []byte {
	mac := hmac.New(sha256.New, key)
	mac.Write(data)
	return mac.Sum(nil)
}

func hexSHA256(data []byte) string {
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}
