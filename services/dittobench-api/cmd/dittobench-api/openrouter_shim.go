package main

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"errors"
	"fmt"
	"math/big"
	"net/http"
	"os"
	"path/filepath"
	"time"
)

const openRouterShimServerName = "openrouter.ai"

var systemCABundleCandidates = []string{
	"/etc/ssl/certs/ca-certificates.crt", // Alpine, Debian, Ubuntu
	"/etc/pki/tls/certs/ca-bundle.crt",   // Fedora, RHEL
}

// prepareOpenRouterShim creates one process-local CA and leaf certificate for
// openrouter.ai, then publishes only the CA certificate alongside the system
// roots for read-only mounting into miner sandboxes. The CA private key and TLS
// leaf private key remain in scorer memory and never cross the Docker boundary.
func prepareOpenRouterShim(caBundlePath string, now time.Time) (*tls.Config, error) {
	if caBundlePath == "" {
		return nil, nil
	}
	caKey, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return nil, fmt.Errorf("generate OpenRouter shim CA key: %w", err)
	}
	caSerial, err := randomCertificateSerial()
	if err != nil {
		return nil, err
	}
	caTemplate := &x509.Certificate{
		SerialNumber: caSerial,
		Subject: pkix.Name{
			CommonName:   "Ditto SN118 OpenRouter compatibility CA",
			Organization: []string{"Ditto SN118 validator"},
		},
		NotBefore:             now.Add(-time.Hour),
		NotAfter:              now.AddDate(5, 0, 0),
		IsCA:                  true,
		BasicConstraintsValid: true,
		KeyUsage:              x509.KeyUsageCertSign | x509.KeyUsageCRLSign | x509.KeyUsageDigitalSignature,
	}
	caDER, err := x509.CreateCertificate(rand.Reader, caTemplate, caTemplate, &caKey.PublicKey, caKey)
	if err != nil {
		return nil, fmt.Errorf("create OpenRouter shim CA: %w", err)
	}
	caPEM := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: caDER})

	leafKey, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return nil, fmt.Errorf("generate OpenRouter shim leaf key: %w", err)
	}
	leafSerial, err := randomCertificateSerial()
	if err != nil {
		return nil, err
	}
	leafTemplate := &x509.Certificate{
		SerialNumber: leafSerial,
		Subject:      pkix.Name{CommonName: openRouterShimServerName},
		DNSNames:     []string{openRouterShimServerName},
		NotBefore:    now.Add(-time.Hour),
		NotAfter:     now.AddDate(5, 0, 0),
		KeyUsage:     x509.KeyUsageDigitalSignature,
		ExtKeyUsage:  []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
	}
	leafDER, err := x509.CreateCertificate(rand.Reader, leafTemplate, caTemplate, &leafKey.PublicKey, caKey)
	if err != nil {
		return nil, fmt.Errorf("create OpenRouter shim leaf certificate: %w", err)
	}
	leafKeyDER, err := x509.MarshalPKCS8PrivateKey(leafKey)
	if err != nil {
		return nil, fmt.Errorf("marshal OpenRouter shim leaf key: %w", err)
	}
	certificate, err := tls.X509KeyPair(
		pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: leafDER}),
		pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: leafKeyDER}),
	)
	if err != nil {
		return nil, fmt.Errorf("load OpenRouter shim certificate: %w", err)
	}
	if err := writeOpenRouterShimCABundle(caBundlePath, caPEM); err != nil {
		return nil, err
	}
	return &tls.Config{
		MinVersion:   tls.VersionTLS12,
		Certificates: []tls.Certificate{certificate},
		NextProtos:   []string{"http/1.1"},
	}, nil
}

func randomCertificateSerial() (*big.Int, error) {
	limit := new(big.Int).Lsh(big.NewInt(1), 128)
	serial, err := rand.Int(rand.Reader, limit)
	if err != nil {
		return nil, fmt.Errorf("generate certificate serial: %w", err)
	}
	if serial.Sign() == 0 {
		serial.SetInt64(1)
	}
	return serial, nil
}

func writeOpenRouterShimCABundle(path string, shimCA []byte) error {
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return fmt.Errorf("create OpenRouter shim CA directory: %w", err)
	}
	bundle := make([]byte, 0, len(shimCA)+256*1024)
	for _, candidate := range systemCABundleCandidates {
		roots, err := os.ReadFile(candidate)
		if err != nil {
			if errors.Is(err, os.ErrNotExist) {
				continue
			}
			return fmt.Errorf("read system CA bundle: %w", err)
		}
		bundle = append(bundle, roots...)
		if len(bundle) > 0 && bundle[len(bundle)-1] != '\n' {
			bundle = append(bundle, '\n')
		}
		break
	}
	bundle = append(bundle, shimCA...)
	tmp, err := os.CreateTemp(dir, ".openrouter-shim-ca-*")
	if err != nil {
		return fmt.Errorf("create OpenRouter shim CA bundle: %w", err)
	}
	tmpPath := tmp.Name()
	cleanup := func() {
		_ = tmp.Close()
		_ = os.Remove(tmpPath)
	}
	if _, err := tmp.Write(bundle); err != nil {
		cleanup()
		return fmt.Errorf("write OpenRouter shim CA bundle: %w", err)
	}
	if err := tmp.Chmod(0o644); err != nil {
		cleanup()
		return fmt.Errorf("protect OpenRouter shim CA bundle: %w", err)
	}
	if err := tmp.Close(); err != nil {
		cleanup()
		return fmt.Errorf("close OpenRouter shim CA bundle: %w", err)
	}
	if err := os.Rename(tmpPath, path); err != nil {
		cleanup()
		return fmt.Errorf("publish OpenRouter shim CA bundle: %w", err)
	}
	return nil
}

func startOpenRouterShim(broker *inferenceBroker, caBundlePath string, port int) error {
	tlsConfig, err := prepareOpenRouterShim(caBundlePath, time.Now())
	if err != nil || tlsConfig == nil {
		return err
	}
	listener, err := tls.Listen("tcp", ":"+fmt.Sprint(port), tlsConfig)
	if err != nil {
		return fmt.Errorf("listen for OpenRouter compatibility traffic: %w", err)
	}
	server := newInferenceBrokerHTTPServer(
		"",
		http.HandlerFunc(broker.handleOpenRouterShim),
	)
	go func() {
		if serveErr := server.Serve(listener); serveErr != nil && !errors.Is(serveErr, http.ErrServerClosed) {
			panic(fmt.Sprintf("OpenRouter compatibility listener failed: %v", serveErr))
		}
	}()
	return nil
}
