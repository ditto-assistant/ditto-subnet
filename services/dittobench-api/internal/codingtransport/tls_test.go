package codingtransport

import (
	"crypto/ed25519"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"math/big"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontrol"
)

func TestTLS13ClientCertificateReachesBoundProxy(t *testing.T) {
	ca, caKey, caPool := testCA(t)
	serverCertificate := testCertificate(t, ca, caKey, true, "")
	clientCertificate := testCertificate(t, ca, caKey, false, testHotkey)
	serverTLS, err := ServerTLSConfig(serverCertificate, caPool)
	if err != nil {
		t.Fatal(err)
	}
	proxy, err := New(Config{
		ValidatorHotkey: testHotkey, UnixSocketPath: ControlSocketPath,
		RoundTripper: roundTripFunc(func(*http.Request) (*http.Response, error) {
			return &http.Response{
				StatusCode: http.StatusNoContent, Header: make(http.Header),
				Body: http.NoBody,
			}, nil
		}),
	})
	if err != nil {
		t.Fatal(err)
	}
	server := httptest.NewUnstartedServer(proxy.Handler())
	server.TLS = serverTLS
	server.StartTLS()
	defer server.Close()

	client := server.Client()
	client.Transport.(*http.Transport).TLSClientConfig = &tls.Config{
		MinVersion: tls.VersionTLS13, MaxVersion: tls.VersionTLS13,
		RootCAs: caPool, Certificates: []tls.Certificate{clientCertificate},
	}
	request, err := http.NewRequest(
		http.MethodPost, server.URL+"/v1/coding/supervisor/author", http.NoBody,
	)
	if err != nil {
		t.Fatal(err)
	}
	request.Header.Set(codingcontrol.EnvelopeHeader, envelopeHeader(t, testHotkey))
	response, err := client.Do(request)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusNoContent || response.TLS == nil ||
		response.TLS.Version != tls.VersionTLS13 {
		t.Fatalf("status=%d TLS=%#v", response.StatusCode, response.TLS)
	}
}

func testCA(t *testing.T) (*x509.Certificate, ed25519.PrivateKey, *x509.CertPool) {
	t.Helper()
	public, private, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	certificate := &x509.Certificate{
		SerialNumber: big.NewInt(1), Subject: pkix.Name{CommonName: "coding-test-ca"},
		NotBefore: time.Now().Add(-time.Minute), NotAfter: time.Now().Add(time.Hour),
		IsCA: true, BasicConstraintsValid: true,
		KeyUsage: x509.KeyUsageCertSign | x509.KeyUsageDigitalSignature,
	}
	raw, err := x509.CreateCertificate(rand.Reader, certificate, certificate, public, private)
	if err != nil {
		t.Fatal(err)
	}
	parsed, err := x509.ParseCertificate(raw)
	if err != nil {
		t.Fatal(err)
	}
	pool := x509.NewCertPool()
	pool.AddCert(parsed)
	return parsed, private, pool
}

func testCertificate(
	t *testing.T,
	ca *x509.Certificate,
	caKey ed25519.PrivateKey,
	server bool,
	hotkey string,
) tls.Certificate {
	t.Helper()
	public, private, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	certificate := &x509.Certificate{
		SerialNumber: big.NewInt(2), Subject: pkix.Name{CommonName: "coding-test"},
		NotBefore: time.Now().Add(-time.Minute), NotAfter: time.Now().Add(time.Hour),
		KeyUsage: x509.KeyUsageDigitalSignature,
	}
	if server {
		certificate.ExtKeyUsage = []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}
		certificate.IPAddresses = []net.IP{net.ParseIP("127.0.0.1")}
	} else {
		certificate.ExtKeyUsage = []x509.ExtKeyUsage{x509.ExtKeyUsageClientAuth}
		uri, uriErr := ValidatorURI(hotkey)
		if uriErr != nil {
			t.Fatal(uriErr)
		}
		certificate.URIs = []*url.URL{uri}
	}
	raw, err := x509.CreateCertificate(rand.Reader, certificate, ca, public, caKey)
	if err != nil {
		t.Fatal(err)
	}
	certificatePEM := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: raw})
	key, err := x509.MarshalPKCS8PrivateKey(private)
	if err != nil {
		t.Fatal(err)
	}
	keyPEM := pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: key})
	value, err := tls.X509KeyPair(certificatePEM, keyPEM)
	if err != nil {
		t.Fatal(err)
	}
	return value
}
