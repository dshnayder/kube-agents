package main

import (
	"bytes"
	"crypto/tls"
	"errors"
	"os"
	"path/filepath"
	"sync"
)

// certReloader loads a server TLS certificate from certFile and keyFile, and reloads it when
// either file changes on disk. Both paths may name the same PEM bundle file, such as an Agent
// Substrate podCertificate credential-bundle.pem.
type certReloader struct {
	certFile string
	keyFile  string

	mu      sync.Mutex
	certPEM []byte
	keyPEM  []byte
	cached  *tls.Certificate
}

func newCertReloader(certFile, keyFile string) (*certReloader, error) {
	cleanCert := filepath.Clean(certFile)
	cleanKey := filepath.Clean(keyFile)
	r := &certReloader{
		certFile: cleanCert,
		keyFile:  cleanKey,
	}
	if _, err := r.load(); err != nil {
		return nil, err
	}
	return r, nil
}

func (r *certReloader) GetCertificate(*tls.ClientHelloInfo) (*tls.Certificate, error) {
	return r.load()
}

func (r *certReloader) load() (*tls.Certificate, error) {
	r.mu.Lock()
	defer r.mu.Unlock()

	certPEM, err := os.ReadFile(r.certFile) // #nosec G304 -- path is configured by the operator via CLI flag or env
	if err != nil {
		if r.cached != nil {
			return r.cached, nil
		}
		return nil, err
	}
	var keyPEM []byte
	if r.keyFile == r.certFile {
		keyPEM = certPEM
	} else {
		keyPEM, err = os.ReadFile(r.keyFile) // #nosec G304 -- path is configured by the operator via CLI flag or env
		if err != nil {
			if r.cached != nil {
				return r.cached, nil
			}
			return nil, err
		}
	}

	if r.cached != nil && bytes.Equal(certPEM, r.certPEM) && bytes.Equal(keyPEM, r.keyPEM) {
		return r.cached, nil
	}

	parsed, err := tls.X509KeyPair(certPEM, keyPEM)
	if err != nil {
		if r.cached != nil {
			return r.cached, nil
		}
		return nil, errors.New("parsing server TLS key pair: " + err.Error())
	}
	r.certPEM = certPEM
	r.keyPEM = keyPEM
	r.cached = &parsed
	return r.cached, nil
}
