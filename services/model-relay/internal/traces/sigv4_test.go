package traces

import (
	"net/url"
	"strings"
	"testing"
	"time"
)

// The worked example from the S3 developer guide, "Authenticating Requests:
// Using Query Parameters (AWS Signature Version 4)": a presigned GET of
// examplebucket/test.txt, valid 24h, signed at 2013-05-24T00:00:00Z with the
// documentation keys. The expected signature is published there.
func TestPresignURLMatchesAWSWorkedExample(t *testing.T) {
	target, _ := url.Parse("https://examplebucket.s3.amazonaws.com/test.txt")
	creds := presignCredentials{
		AccessKeyID:     "AKIAIOSFODNN7EXAMPLE",
		SecretAccessKey: "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
		Region:          "us-east-1",
	}
	at := time.Date(2013, 5, 24, 0, 0, 0, 0, time.UTC)
	got := presignURL("GET", target, creds, at, 24*time.Hour)
	want := "https://examplebucket.s3.amazonaws.com/test.txt" +
		"?X-Amz-Algorithm=AWS4-HMAC-SHA256" +
		"&X-Amz-Credential=AKIAIOSFODNN7EXAMPLE%2F20130524%2Fus-east-1%2Fs3%2Faws4_request" +
		"&X-Amz-Date=20130524T000000Z&X-Amz-Expires=86400&X-Amz-SignedHeaders=host" +
		"&X-Amz-Signature=aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404"
	if got != want {
		t.Fatalf("presigned URL mismatch\n got %s\nwant %s", got, want)
	}
}

func TestCanonicalPathEncodesSegmentsOnce(t *testing.T) {
	u, _ := url.Parse("https://s3.example.com/ditto-subnet-traces/traces/v1/lane=inference/kind=chat/a b.jsonl.zst")
	got := canonicalPath(u.EscapedPath())
	want := "/ditto-subnet-traces/traces/v1/lane%3Dinference/kind%3Dchat/a%20b.jsonl.zst"
	if got != want {
		t.Fatalf("canonical path: got %s want %s", got, want)
	}
	if strings.Contains(got, "%25") {
		t.Fatalf("double encoding: %s", got)
	}
}

func TestPresignKeepsHostPortInSignature(t *testing.T) {
	// A non-default port must be signed as part of host:, because that is
	// what Go's http client sends in the Host header.
	target, _ := url.Parse("http://127.0.0.1:9999/bucket/key")
	creds := presignCredentials{AccessKeyID: "k", SecretAccessKey: "s", Region: "r"}
	a := presignURL("PUT", target, creds, time.Unix(0, 0), time.Minute)
	target2, _ := url.Parse("http://127.0.0.1:9998/bucket/key")
	b := presignURL("PUT", target2, creds, time.Unix(0, 0), time.Minute)
	sigA := a[strings.LastIndex(a, "X-Amz-Signature="):]
	sigB := b[strings.LastIndex(b, "X-Amz-Signature="):]
	if sigA == sigB {
		t.Fatalf("port is not part of the signature")
	}
}
