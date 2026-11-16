package k8sapiproxy

import (
	"crypto"
	"crypto/ecdsa"
	"crypto/ed25519"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"errors"
	"math/big"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"
)

type testOIDCIssuer struct {
	srv *httptest.Server

	mu         sync.Mutex
	jwks       jwksDoc
	statusCode int
}

func newTestOIDCIssuer(t *testing.T, keys ...rawJWK) *testOIDCIssuer {
	t.Helper()
	idp := &testOIDCIssuer{
		jwks:       jwksDoc{Keys: keys},
		statusCode: http.StatusOK,
	}
	srv := httptest.NewUnstartedServer(nil)
	srv.Config.Handler = http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		idp.mu.Lock()
		code := idp.statusCode
		doc := idp.jwks
		idp.mu.Unlock()
		if code != http.StatusOK {
			http.Error(w, "idp error", code)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case "/.well-known/openid-configuration":
			_ = json.NewEncoder(w).Encode(oidcDiscoveryDoc{
				Issuer:  srv.URL,
				JWKSURI: srv.URL + "/jwks",
			})
		case "/jwks":
			_ = json.NewEncoder(w).Encode(doc)
		default:
			http.NotFound(w, r)
		}
	})
	srv.StartTLS()
	t.Cleanup(srv.Close)
	idp.srv = srv
	return idp
}

func (i *testOIDCIssuer) writeCAFile(t *testing.T) string {
	t.Helper()
	cert := i.srv.Certificate()
	pemBytes := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: cert.Raw})
	path := filepath.Join(t.TempDir(), "ca.pem")
	if err := os.WriteFile(path, pemBytes, 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func ecJWK(t *testing.T, kid string, key *ecdsa.PrivateKey) rawJWK {
	t.Helper()
	uncompressed, err := key.PublicKey.Bytes()
	if err != nil {
		t.Fatal(err)
	}
	coordLen := (len(uncompressed) - 1) / 2
	return rawJWK{
		KeyType: "EC",
		Use:     "sig",
		Alg:     "ES256",
		KeyID:   kid,
		Curve:   "P-256",
		X:       base64.RawURLEncoding.EncodeToString(uncompressed[1 : 1+coordLen]),
		Y:       base64.RawURLEncoding.EncodeToString(uncompressed[1+coordLen:]),
	}
}

func rsaJWK(t *testing.T, kid string, key *rsa.PrivateKey) rawJWK {
	t.Helper()
	eBytes := big.NewInt(int64(key.PublicKey.E)).Bytes()
	return rawJWK{
		KeyType: "RSA",
		Use:     "sig",
		Alg:     "RS256",
		KeyID:   kid,
		N:       base64.RawURLEncoding.EncodeToString(key.PublicKey.N.Bytes()),
		E:       base64.RawURLEncoding.EncodeToString(eBytes),
	}
}

func edJWK(kid string, pub ed25519.PublicKey) rawJWK {
	return rawJWK{
		KeyType: "OKP",
		Use:     "sig",
		Alg:     "EdDSA",
		KeyID:   kid,
		Curve:   "Ed25519",
		X:       base64.RawURLEncoding.EncodeToString(pub),
	}
}

func signES256JWT(t *testing.T, kid string, key *ecdsa.PrivateKey, claims map[string]any) string {
	t.Helper()
	hdrJSON, err := json.Marshal(map[string]string{"alg": "ES256", "kid": kid, "typ": "JWT"})
	if err != nil {
		t.Fatal(err)
	}
	payloadJSON, err := json.Marshal(claims)
	if err != nil {
		t.Fatal(err)
	}
	signingInput := base64.RawURLEncoding.EncodeToString(hdrJSON) + "." + base64.RawURLEncoding.EncodeToString(payloadJSON)
	sum := sha256.Sum256([]byte(signingInput))
	r, s, err := ecdsa.Sign(rand.Reader, key, sum[:])
	if err != nil {
		t.Fatal(err)
	}
	sig := make([]byte, 64)
	r.FillBytes(sig[:32])
	s.FillBytes(sig[32:])
	return signingInput + "." + base64.RawURLEncoding.EncodeToString(sig)
}

func signRS256JWT(t *testing.T, kid string, key *rsa.PrivateKey, claims map[string]any) string {
	t.Helper()
	hdrJSON, _ := json.Marshal(map[string]string{"alg": "RS256", "kid": kid, "typ": "JWT"})
	payloadJSON, _ := json.Marshal(claims)
	signingInput := base64.RawURLEncoding.EncodeToString(hdrJSON) + "." + base64.RawURLEncoding.EncodeToString(payloadJSON)
	sum := sha256.Sum256([]byte(signingInput))
	sig, err := rsa.SignPKCS1v15(rand.Reader, key, crypto.SHA256, sum[:])
	if err != nil {
		t.Fatal(err)
	}
	return signingInput + "." + base64.RawURLEncoding.EncodeToString(sig)
}

func signEdDSAJWT(t *testing.T, kid string, key ed25519.PrivateKey, claims map[string]any) string {
	t.Helper()
	hdrJSON, _ := json.Marshal(map[string]string{"alg": "EdDSA", "kid": kid, "typ": "JWT"})
	payloadJSON, _ := json.Marshal(claims)
	signingInput := base64.RawURLEncoding.EncodeToString(hdrJSON) + "." + base64.RawURLEncoding.EncodeToString(payloadJSON)
	sig := ed25519.Sign(key, []byte(signingInput))
	return signingInput + "." + base64.RawURLEncoding.EncodeToString(sig)
}

func TestOIDCAuthenticatorVerifiesJWTClaimsPartitionAndKeyRotation(t *testing.T) {
	key1, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	key2, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}

	idp := newTestOIDCIssuer(t, ecJWK(t, "k1", key1))
	caFile := idp.writeCAFile(t)

	now := time.Unix(1_760_000_000, 0)
	currentNow := now
	authn, err := NewOIDCAuthenticator(OIDCConfig{
		IssuerURL:             idp.srv.URL,
		Audience:              "https://k8s-api-proxy.ax-system.svc",
		CAFile:                caFile,
		RequiredSubjectPrefix: "actor/",
		PartitionClaim:        `custom\.claim.tenant`,
		UIDClaim:              `custom\.claim.uid`,
		AllowedPartitions:     []string{"team-a"},
		Now:                   func() time.Time { return currentNow },
	})
	if err != nil {
		t.Fatalf("NewOIDCAuthenticator: %v", err)
	}

	validClaims := func() map[string]any {
		return map[string]any{
			"iss": idp.srv.URL,
			"sub": "actor/team-a/triage-1",
			"aud": []string{"https://k8s-api-proxy.ax-system.svc"},
			"exp": currentNow.Add(15 * time.Minute).Unix(),
			"nbf": currentNow.Add(-5 * time.Minute).Unix(),
			"custom.claim": map[string]string{
				"tenant": "team-a",
				"uid":    "uid-123",
			},
		}
	}

	req, _ := http.NewRequest(http.MethodGet, "https://k8s-api-proxy.ax-system.svc/clusters/in-cluster/api", nil)
	req.Header.Set("Authorization", "Bearer "+signES256JWT(t, "k1", key1, validClaims()))
	caller, err := authn.AuthenticateRequest(req)
	if err != nil {
		t.Fatalf("AuthenticateRequest valid ES256: %v", err)
	}
	if caller.Username != "actor/team-a/triage-1" || caller.UID != "uid-123" || PartitionFromCaller(caller) != "team-a" {
		t.Fatalf("unexpected caller: %+v (partition=%q)", caller, PartitionFromCaller(caller))
	}

	// Rotate signing key on the OIDC provider and advance clock past defaultOIDCMinRefreshDelay.
	idp.mu.Lock()
	idp.jwks = jwksDoc{Keys: []rawJWK{ecJWK(t, "k2", key2)}}
	idp.mu.Unlock()
	currentNow = currentNow.Add(15 * time.Second)

	reqRot, _ := http.NewRequest(http.MethodGet, "https://k8s-api-proxy.ax-system.svc/clusters/in-cluster/api", nil)
	reqRot.Header.Set("Authorization", "Bearer "+signES256JWT(t, "k2", key2, validClaims()))
	if _, err := authn.AuthenticateRequest(reqRot); err != nil {
		t.Fatalf("AuthenticateRequest after key rotation: %v", err)
	}
}

func TestOIDCAuthenticatorSupportsArbitrarySubjectsRS256AndEdDSA(t *testing.T) {
	rsaKey, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	edPub, edPriv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}

	idp := newTestOIDCIssuer(t, rsaJWK(t, "rsa-1", rsaKey), edJWK("ed-1", edPub))
	now := time.Unix(1_760_000_000, 0)
	authn, err := NewOIDCAuthenticator(OIDCConfig{
		IssuerURL:  idp.srv.URL,
		Audience:   "https://k8s-api-proxy.example.com",
		HTTPClient: idp.srv.Client(),
		Now:        func() time.Time { return now },
	})
	if err != nil {
		t.Fatal(err)
	}

	// Without RequiredSubjectPrefix, numeric/Cloud Run/Entra subjects work out of the box.
	claims := map[string]any{
		"iss": idp.srv.URL,
		"sub": "1149284759201948",
		"aud": "https://k8s-api-proxy.example.com",
		"exp": now.Add(10 * time.Minute).Unix(),
	}

	for _, tok := range []string{
		signRS256JWT(t, "rsa-1", rsaKey, claims),
		signEdDSAJWT(t, "ed-1", edPriv, claims),
	} {
		req, _ := http.NewRequest(http.MethodGet, "https://example.invalid/clusters/in-cluster/api", nil)
		req.Header.Set("Authorization", "Bearer "+tok)
		if _, err := authn.AuthenticateRequest(req); err != nil {
			t.Fatalf("AuthenticateRequest failed: %v", err)
		}
	}
}

func TestOIDCAuthenticatorRejectsInvalidTokensAndSurfaces503WhenIDPDown(t *testing.T) {
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	stranger, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	idp := newTestOIDCIssuer(t, ecJWK(t, "k1", key))
	now := time.Unix(1_760_000_000, 0)

	authn, err := NewOIDCAuthenticator(OIDCConfig{
		IssuerURL:             idp.srv.URL,
		Audience:              "https://k8s-api-proxy.ax-system.svc",
		HTTPClient:            idp.srv.Client(),
		RequiredSubjectPrefix: "actor/",
		PartitionClaim:        "tenant",
		AllowedPartitions:     []string{"allowed-ns"},
		Now:                   func() time.Time { return now },
	})
	if err != nil {
		t.Fatal(err)
	}

	// 1. IDP unreachable on initial discovery -> errAuthenticationUnavailable (503).
	idp.mu.Lock()
	idp.statusCode = http.StatusServiceUnavailable
	idp.mu.Unlock()
	reqDown, _ := http.NewRequest(http.MethodGet, "https://example.invalid/clusters/in-cluster/api", nil)
	reqDown.Header.Set("Authorization", "Bearer "+signES256JWT(t, "k1", key, map[string]any{
		"iss":    idp.srv.URL,
		"sub":    "actor/allowed-ns/t1",
		"aud":    []string{"https://k8s-api-proxy.ax-system.svc"},
		"exp":    now.Add(time.Hour).Unix(),
		"tenant": "allowed-ns",
	}))
	if _, err := authn.AuthenticateRequest(reqDown); !errors.Is(err, errAuthenticationUnavailable) {
		t.Fatalf("IDP 503 error = %v, want errAuthenticationUnavailable", err)
	}

	idp.mu.Lock()
	idp.statusCode = http.StatusOK
	idp.mu.Unlock()

	cases := []struct {
		name  string
		token string
	}{
		{
			name: "wrong signature",
			token: signES256JWT(t, "k1", stranger, map[string]any{
				"iss":    idp.srv.URL,
				"sub":    "actor/allowed-ns/t1",
				"aud":    []string{"https://k8s-api-proxy.ax-system.svc"},
				"exp":    now.Add(time.Hour).Unix(),
				"tenant": "allowed-ns",
			}),
		},
		{
			name: "disallowed subject prefix rejected",
			token: signES256JWT(t, "k1", key, map[string]any{
				"iss":    idp.srv.URL,
				"sub":    "worker/allowed-ns/t1",
				"aud":    []string{"https://k8s-api-proxy.ax-system.svc"},
				"exp":    now.Add(time.Hour).Unix(),
				"tenant": "allowed-ns",
			}),
		},
		{
			name: "wrong audience",
			token: signES256JWT(t, "k1", key, map[string]any{
				"iss":    idp.srv.URL,
				"sub":    "actor/allowed-ns/t1",
				"aud":    []string{"https://other.example"},
				"exp":    now.Add(time.Hour).Unix(),
				"tenant": "allowed-ns",
			}),
		},
		{
			name: "expired",
			token: signES256JWT(t, "k1", key, map[string]any{
				"iss":    idp.srv.URL,
				"sub":    "actor/allowed-ns/t1",
				"aud":    []string{"https://k8s-api-proxy.ax-system.svc"},
				"exp":    now.Add(-5 * time.Minute).Unix(),
				"tenant": "allowed-ns",
			}),
		},
		{
			name: "disallowed partition",
			token: signES256JWT(t, "k1", key, map[string]any{
				"iss":    idp.srv.URL,
				"sub":    "actor/other-ns/t1",
				"aud":    []string{"https://k8s-api-proxy.ax-system.svc"},
				"exp":    now.Add(time.Hour).Unix(),
				"tenant": "other-ns",
			}),
		},
		{
			name: "missing partition claim rejected when configured",
			token: signES256JWT(t, "k1", key, map[string]any{
				"iss": idp.srv.URL,
				"sub": "actor/allowed-ns/t1",
				"aud": []string{"https://k8s-api-proxy.ax-system.svc"},
				"exp": now.Add(time.Hour).Unix(),
			}),
		},
		{
			name:  "alg none rejected",
			token: base64.RawURLEncoding.EncodeToString([]byte(`{"alg":"none"}`)) + "." + base64.RawURLEncoding.EncodeToString([]byte(`{"iss":"`+idp.srv.URL+`"}`)) + ".",
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			req, _ := http.NewRequest(http.MethodGet, "https://example.invalid/clusters/in-cluster/api", nil)
			req.Header.Set("Authorization", "Bearer "+tc.token)
			if _, err := authn.AuthenticateRequest(req); err == nil {
				t.Fatalf("expected authentication error for %s", tc.name)
			} else if errors.Is(err, errAuthenticationUnavailable) {
				t.Fatalf("unexpected 503 error for %s: %v", tc.name, err)
			}
		})
	}

	badCA := filepath.Join(t.TempDir(), "empty.pem")
	_ = os.WriteFile(badCA, []byte("not a cert"), 0o600)
	if _, err := NewOIDCAuthenticator(OIDCConfig{
		IssuerURL: idp.srv.URL,
		Audience:  "aud",
		CAFile:    badCA,
	}); err == nil {
		t.Fatal("expected error for invalid CAFile")
	}
	_ = x509.Certificate{}
}
