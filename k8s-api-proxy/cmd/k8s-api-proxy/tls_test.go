package main

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"math/big"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func generateTestBundlePEM(t *testing.T, cn string) (certPEM, keyPEM, combinedPEM []byte) {
	t.Helper()
	priv, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	tmpl := &x509.Certificate{
		SerialNumber: big.NewInt(1),
		Subject:      pkix.Name{CommonName: cn},
		NotBefore:    time.Now().Add(-time.Hour),
		NotAfter:     time.Now().Add(time.Hour),
		KeyUsage:     x509.KeyUsageDigitalSignature,
		ExtKeyUsage:  []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, &priv.PublicKey, priv)
	if err != nil {
		t.Fatal(err)
	}
	keyDER, err := x509.MarshalECPrivateKey(priv)
	if err != nil {
		t.Fatal(err)
	}
	certPEM = pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})
	keyPEM = pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: keyDER})
	combinedPEM = append(append([]byte{}, keyPEM...), certPEM...)
	return certPEM, keyPEM, combinedPEM
}

func TestCertReloaderSupportsCombinedBundleAndRotation(t *testing.T) {
	dir := t.TempDir()
	bundlePath := filepath.Join(dir, "credential-bundle.pem")

	_, _, bundle1 := generateTestBundlePEM(t, "first.svc")
	if err := os.WriteFile(bundlePath, bundle1, 0o600); err != nil {
		t.Fatal(err)
	}

	reloader, err := newCertReloader(bundlePath, bundlePath)
	if err != nil {
		t.Fatalf("newCertReloader: %v", err)
	}
	got1, err := reloader.GetCertificate(nil)
	if err != nil {
		t.Fatal(err)
	}
	leaf1, err := x509.ParseCertificate(got1.Certificate[0])
	if err != nil {
		t.Fatal(err)
	}
	if leaf1.Subject.CommonName != "first.svc" {
		t.Fatalf("CN = %q, want first.svc", leaf1.Subject.CommonName)
	}

	// Rotate bundle on disk.
	_, _, bundle2 := generateTestBundlePEM(t, "rotated.svc")
	if err := os.WriteFile(bundlePath, bundle2, 0o600); err != nil {
		t.Fatal(err)
	}
	got2, err := reloader.GetCertificate(nil)
	if err != nil {
		t.Fatal(err)
	}
	leaf2, err := x509.ParseCertificate(got2.Certificate[0])
	if err != nil {
		t.Fatal(err)
	}
	if leaf2.Subject.CommonName != "rotated.svc" {
		t.Fatalf("rotated CN = %q, want rotated.svc", leaf2.Subject.CommonName)
	}
}
