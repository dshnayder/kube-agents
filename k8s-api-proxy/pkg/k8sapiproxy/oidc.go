package k8sapiproxy

import (
	"bytes"
	"context"
	"crypto"
	"crypto/ecdsa"
	"crypto/ed25519"
	"crypto/elliptic"
	"crypto/rsa"
	"crypto/sha256"
	"crypto/sha512"
	"crypto/tls"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math/big"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"time"

	"k8s.io/apimachinery/pkg/util/sets"
)

const (
	defaultOIDCJWKSCacheTTL    = 5 * time.Minute
	defaultOIDCMinRefreshDelay = 10 * time.Second
	defaultOIDCClockSkew       = 30 * time.Second
	defaultOIDCHTTPTimeout     = 10 * time.Second
	maxOIDCResponseBytes       = 1 << 20 // 1 MiB
	maxBearerTokenBytes        = 16 << 10
	minRSAKeyBits              = 2048

	actorSubjectPrefix = "actor/"
	atespaceGroupPref  = "atespace:"
)

// OIDCConfig configures NewOIDCAuthenticator for OpenID Connect JWT callers such as Agent Substrate actors.
type OIDCConfig struct {
	// IssuerURL is the expected iss claim and discovery base URL (https://<host>[:port]). Required.
	IssuerURL string
	// Audience is a required aud claim value (comma-separated values are split). Either Audience or Audiences is required.
	Audience string
	// Audiences lists additional accepted aud claim values.
	Audiences []string
	// CAFile is an optional PEM bundle used to verify TLS connections to IssuerURL.
	CAFile string
	// HTTPClient overrides the HTTP client used for OIDC discovery and JWKS fetches; optional.
	HTTPClient *http.Client
	// RequiredSubjectPrefix restricts accepted sub claims to this prefix; defaults to "actor/".
	RequiredSubjectPrefix string
	// AllowedAtespaces restricts actor/<atespace>/<name> callers to the listed atespaces; empty allows all atespaces.
	AllowedAtespaces []string
	// JWKSCacheTTL sets how long a fetched JWKS stays fresh before background refresh; defaults to 5m.
	JWKSCacheTTL time.Duration
	// ClockSkew allows small clock differences on exp and nbf claims; defaults to 30s.
	ClockSkew time.Duration
	// Now returns the current time; time.Now when nil.
	Now func() time.Time
}

// AtespaceFromCaller returns the Agent Substrate atespace recorded on id, or "" when absent.
func AtespaceFromCaller(id *CallerIdentity) string {
	if id == nil {
		return ""
	}
	for _, g := range id.Groups {
		if after, ok := strings.CutPrefix(g, atespaceGroupPref); ok && after != "" {
			return after
		}
	}
	return ""
}

// ChainAuthenticators tries each authenticator in order and returns the first accepted CallerIdentity.
// If none accepts the request and at least one fails with an unavailable backend, it returns a 503 error.
func ChainAuthenticators(authenticators ...CallerAuthenticator) (CallerAuthenticator, error) {
	var active []CallerAuthenticator
	for _, a := range authenticators {
		if a != nil {
			active = append(active, a)
		}
	}
	if len(active) == 0 {
		return nil, errors.New("k8sapiproxy: ChainAuthenticators requires at least one authenticator")
	}
	if len(active) == 1 {
		return active[0], nil
	}
	return &chainedAuthenticator{authenticators: active}, nil
}

type chainedAuthenticator struct {
	authenticators []CallerAuthenticator
}

func (c *chainedAuthenticator) AuthenticateRequest(r *http.Request) (*CallerIdentity, error) {
	var unavailableErr error
	for _, a := range c.authenticators {
		caller, err := a.AuthenticateRequest(r)
		if err == nil {
			return caller, nil
		}
		if errors.Is(err, errAuthenticationUnavailable) && unavailableErr == nil {
			unavailableErr = err
		}
	}
	if unavailableErr != nil {
		return nil, unavailableErr
	}
	return nil, errNotAuthenticated
}

type oidcAuthenticator struct {
	issuer           string
	issuerHost       string
	audiences        sets.Set[string]
	requiredSubPref  string
	allowedAtespaces sets.Set[string]
	cacheTTL         time.Duration
	clockSkew        time.Duration
	now              func() time.Time
	httpClient       *http.Client

	mu            sync.Mutex
	jwksURI       string
	keys          []parsedJWK
	fetchedAt     time.Time
	lastRefreshAt time.Time
}

type parsedJWK struct {
	kid string
	alg string
	pub crypto.PublicKey
}

// NewOIDCAuthenticator returns a CallerAuthenticator that verifies OIDC bearer JWTs against IssuerURL's JWKS.
func NewOIDCAuthenticator(cfg OIDCConfig) (CallerAuthenticator, error) {
	issuer := strings.TrimSpace(cfg.IssuerURL)
	if err := validateHTTPSIssuer(issuer); err != nil {
		return nil, fmt.Errorf("k8sapiproxy: OIDCConfig.IssuerURL: %w", err)
	}
	u, _ := url.Parse(issuer)

	audiences := sets.New[string]()
	for _, raw := range append([]string{cfg.Audience}, cfg.Audiences...) {
		for _, entry := range strings.Split(raw, ",") {
			if aud := strings.TrimSpace(entry); aud != "" {
				audiences.Insert(aud)
			}
		}
	}
	if audiences.Len() == 0 {
		return nil, errors.New("k8sapiproxy: OIDCConfig.Audience is required")
	}

	allowedAtespaces := sets.New[string]()
	for _, raw := range cfg.AllowedAtespaces {
		for _, entry := range strings.Split(raw, ",") {
			ns := strings.TrimSpace(entry)
			if ns != "" && ns != "*" {
				allowedAtespaces.Insert(ns)
			}
		}
	}

	subPref := cfg.RequiredSubjectPrefix
	if subPref == "" {
		subPref = actorSubjectPrefix
	}
	cacheTTL := cfg.JWKSCacheTTL
	if cacheTTL <= 0 {
		cacheTTL = defaultOIDCJWKSCacheTTL
	}
	skew := cfg.ClockSkew
	if skew < 0 {
		skew = 0
	} else if skew == 0 {
		skew = defaultOIDCClockSkew
	}
	nowFn := cfg.Now
	if nowFn == nil {
		nowFn = time.Now
	}

	client := cfg.HTTPClient
	if client == nil {
		var err error
		client, err = newOIDCHTTPClient(strings.TrimSpace(cfg.CAFile))
		if err != nil {
			return nil, fmt.Errorf("k8sapiproxy: OIDCConfig.CAFile: %w", err)
		}
	}

	return &oidcAuthenticator{
		issuer:           issuer,
		issuerHost:       u.Host,
		audiences:        audiences,
		requiredSubPref:  subPref,
		allowedAtespaces: allowedAtespaces,
		cacheTTL:         cacheTTL,
		clockSkew:        skew,
		now:              nowFn,
		httpClient:       client,
	}, nil
}

func validateHTTPSIssuer(issuer string) error {
	if issuer == "" {
		return errors.New("issuer URL is required")
	}
	u, err := url.Parse(issuer)
	if err != nil {
		return err
	}
	if u.Scheme != "https" || u.Hostname() == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" {
		return fmt.Errorf("%q must use https://<host>[:port] with no query or fragment", issuer)
	}
	if strings.HasSuffix(u.Path, "/") {
		return fmt.Errorf("%q must not end with a trailing slash", issuer)
	}
	return nil
}

func newOIDCHTTPClient(caFile string) (*http.Client, error) {
	base, ok := http.DefaultTransport.(*http.Transport)
	if !ok {
		return nil, errors.New("default HTTP transport is not *http.Transport")
	}
	noRedirect := func(*http.Request, []*http.Request) error {
		return http.ErrUseLastResponse
	}
	if caFile == "" {
		tr := base.Clone()
		tr.TLSClientConfig = &tls.Config{MinVersion: tls.VersionTLS12}
		return &http.Client{Timeout: defaultOIDCHTTPTimeout, Transport: tr, CheckRedirect: noRedirect}, nil
	}
	rt, err := newReloadingCATransport(base, caFile)
	if err != nil {
		return nil, err
	}
	return &http.Client{Timeout: defaultOIDCHTTPTimeout, Transport: rt, CheckRedirect: noRedirect}, nil
}

type reloadingCATransport struct {
	base   *http.Transport
	caFile string

	mu        sync.Mutex
	rawPEM    []byte
	transport *http.Transport
}

func newReloadingCATransport(base *http.Transport, caFile string) (*reloadingCATransport, error) {
	cleanPath := filepath.Clean(caFile)
	pemBytes, pool, err := readCertPool(cleanPath)
	if err != nil {
		return nil, err
	}
	tr := base.Clone()
	tr.TLSClientConfig = &tls.Config{MinVersion: tls.VersionTLS12, RootCAs: pool}
	return &reloadingCATransport{
		base:      base,
		caFile:    cleanPath,
		rawPEM:    pemBytes,
		transport: tr,
	}, nil
}

func (t *reloadingCATransport) RoundTrip(req *http.Request) (*http.Response, error) {
	return t.current().RoundTrip(req)
}

func (t *reloadingCATransport) current() *http.Transport {
	t.mu.Lock()
	defer t.mu.Unlock()
	pemBytes, err := os.ReadFile(t.caFile) // #nosec G304 -- path is configured by the operator via CLI flag or env
	if err != nil || bytes.Equal(pemBytes, t.rawPEM) {
		return t.transport
	}
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM(pemBytes) {
		return t.transport
	}
	next := t.base.Clone()
	next.TLSClientConfig = &tls.Config{MinVersion: tls.VersionTLS12, RootCAs: pool}
	t.transport.CloseIdleConnections()
	t.rawPEM = pemBytes
	t.transport = next
	return t.transport
}

func readCertPool(cleanPath string) ([]byte, *x509.CertPool, error) {
	pemBytes, err := os.ReadFile(cleanPath) // #nosec G304 -- path is configured by the operator via CLI flag or env
	if err != nil {
		return nil, nil, err
	}
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM(pemBytes) {
		return nil, nil, fmt.Errorf("no PEM certificates found in %q", cleanPath)
	}
	return pemBytes, pool, nil
}

func (a *oidcAuthenticator) AuthenticateRequest(r *http.Request) (*CallerIdentity, error) {
	token, ok := extractBearerToken(r.Header.Get("Authorization"))
	if !ok {
		return nil, errNotAuthenticated
	}
	hdr, payload, sig, signingInput, err := splitCompactJWT(token)
	if err != nil {
		return nil, errNotAuthenticated
	}

	keys, err := a.verificationKeys(r.Context(), hdr.KeyID, false)
	if err != nil {
		return nil, fmt.Errorf("%w: %w", errAuthenticationUnavailable, err)
	}
	if !verifyWithAnyKey(keys, hdr, signingInput, sig) {
		// Refresh once on an unknown kid so key rotation succeeds without failing callers.
		if hdr.KeyID != "" && !hasKeyID(keys, hdr.KeyID) {
			refreshed, refreshErr := a.verificationKeys(r.Context(), hdr.KeyID, true)
			if refreshErr != nil {
				return nil, fmt.Errorf("%w: %w", errAuthenticationUnavailable, refreshErr)
			}
			if !verifyWithAnyKey(refreshed, hdr, signingInput, sig) {
				return nil, errNotAuthenticated
			}
		} else {
			return nil, errNotAuthenticated
		}
	}

	return a.validateClaims(payload)
}

func extractBearerToken(authHeader string) (string, bool) {
	trimmed := strings.TrimSpace(authHeader)
	if len(trimmed) < len("Bearer ")+1 || len(trimmed) > maxBearerTokenBytes {
		return "", false
	}
	if !strings.EqualFold(trimmed[:len("Bearer ")], "Bearer ") {
		return "", false
	}
	tok := strings.TrimSpace(trimmed[len("Bearer "):])
	if tok == "" || strings.ContainsAny(tok, " \t\r\n") {
		return "", false
	}
	return tok, true
}

type jwtHeader struct {
	Algorithm string `json:"alg"`
	KeyID     string `json:"kid"`
}

func splitCompactJWT(token string) (jwtHeader, []byte, []byte, []byte, error) {
	firstDot := strings.IndexByte(token, '.')
	if firstDot <= 0 {
		return jwtHeader{}, nil, nil, nil, errors.New("malformed JWT")
	}
	rest := token[firstDot+1:]
	secondDot := strings.IndexByte(rest, '.')
	if secondDot <= 0 || strings.IndexByte(rest[secondDot+1:], '.') >= 0 {
		return jwtHeader{}, nil, nil, nil, errors.New("malformed JWT")
	}

	hdrBytes, err := base64.RawURLEncoding.DecodeString(token[:firstDot])
	if err != nil {
		return jwtHeader{}, nil, nil, nil, err
	}
	var hdr jwtHeader
	if err := json.Unmarshal(hdrBytes, &hdr); err != nil {
		return jwtHeader{}, nil, nil, nil, err
	}
	if !supportedJWSAlg(hdr.Algorithm) {
		return jwtHeader{}, nil, nil, nil, errors.New("unsupported JWS alg")
	}

	payload, err := base64.RawURLEncoding.DecodeString(rest[:secondDot])
	if err != nil {
		return jwtHeader{}, nil, nil, nil, err
	}
	sig, err := base64.RawURLEncoding.DecodeString(rest[secondDot+1:])
	if err != nil {
		return jwtHeader{}, nil, nil, nil, err
	}
	signingInput := []byte(token[:firstDot+1+secondDot])
	return hdr, payload, sig, signingInput, nil
}

func supportedJWSAlg(alg string) bool {
	switch alg {
	case "ES256", "ES384", "RS256", "RS384", "RS512", "EdDSA":
		return true
	default:
		return false
	}
}

func hasKeyID(keys []parsedJWK, kid string) bool {
	for _, k := range keys {
		if k.kid == kid {
			return true
		}
	}
	return false
}

func verifyWithAnyKey(keys []parsedJWK, hdr jwtHeader, signingInput, sig []byte) bool {
	for _, k := range keys {
		if hdr.KeyID != "" && k.kid != "" && k.kid != hdr.KeyID {
			continue
		}
		if k.alg != "" && k.alg != hdr.Algorithm {
			continue
		}
		if verifySignature(k.pub, hdr.Algorithm, signingInput, sig) {
			return true
		}
	}
	return false
}

func verifySignature(pub crypto.PublicKey, alg string, signingInput, sig []byte) bool {
	switch alg {
	case "ES256":
		ecKey, ok := pub.(*ecdsa.PublicKey)
		if !ok || ecKey.Curve != elliptic.P256() || len(sig) != 64 {
			return false
		}
		sum := sha256.Sum256(signingInput)
		r := new(big.Int).SetBytes(sig[:32])
		s := new(big.Int).SetBytes(sig[32:])
		return ecdsa.Verify(ecKey, sum[:], r, s)
	case "ES384":
		ecKey, ok := pub.(*ecdsa.PublicKey)
		if !ok || ecKey.Curve != elliptic.P384() || len(sig) != 96 {
			return false
		}
		sum := sha512.Sum384(signingInput)
		r := new(big.Int).SetBytes(sig[:48])
		s := new(big.Int).SetBytes(sig[48:])
		return ecdsa.Verify(ecKey, sum[:], r, s)
	case "RS256":
		rsaKey, ok := pub.(*rsa.PublicKey)
		if !ok || rsaKey.N.BitLen() < minRSAKeyBits {
			return false
		}
		sum := sha256.Sum256(signingInput)
		return rsa.VerifyPKCS1v15(rsaKey, crypto.SHA256, sum[:], sig) == nil
	case "RS384":
		rsaKey, ok := pub.(*rsa.PublicKey)
		if !ok || rsaKey.N.BitLen() < minRSAKeyBits {
			return false
		}
		sum := sha512.Sum384(signingInput)
		return rsa.VerifyPKCS1v15(rsaKey, crypto.SHA384, sum[:], sig) == nil
	case "RS512":
		rsaKey, ok := pub.(*rsa.PublicKey)
		if !ok || rsaKey.N.BitLen() < minRSAKeyBits {
			return false
		}
		sum := sha512.Sum512(signingInput)
		return rsa.VerifyPKCS1v15(rsaKey, crypto.SHA512, sum[:], sig) == nil
	case "EdDSA":
		edKey, ok := pub.(ed25519.PublicKey)
		if !ok || len(edKey) != ed25519.PublicKeySize {
			return false
		}
		return ed25519.Verify(edKey, signingInput, sig)
	default:
		return false
	}
}

type jwtAudience []string

func (a *jwtAudience) UnmarshalJSON(data []byte) error {
	var single string
	if err := json.Unmarshal(data, &single); err == nil {
		*a = []string{single}
		return nil
	}
	var list []string
	if err := json.Unmarshal(data, &list); err != nil {
		return err
	}
	*a = list
	return nil
}

type ateActorClaim struct {
	Atespace  string `json:"atespace"`
	ActorName string `json:"actorName"`
	ActorUID  string `json:"actorUID"`
}

type oidcTokenClaims struct {
	Issuer    string         `json:"iss"`
	Subject   string         `json:"sub"`
	Audience  jwtAudience    `json:"aud"`
	ExpiresAt *int64         `json:"exp"`
	NotBefore *int64         `json:"nbf"`
	Actor     *ateActorClaim `json:"actor.ate.dev"`
}

func (a *oidcAuthenticator) validateClaims(payload []byte) (*CallerIdentity, error) {
	var claims oidcTokenClaims
	if err := json.Unmarshal(payload, &claims); err != nil {
		return nil, errNotAuthenticated
	}
	if claims.Issuer != a.issuer || claims.Subject == "" || claims.ExpiresAt == nil {
		return nil, errNotAuthenticated
	}
	if !strings.HasPrefix(claims.Subject, a.requiredSubPref) {
		return nil, errNotAuthenticated
	}
	matchedAud := false
	for _, aud := range claims.Audience {
		if a.audiences.Has(aud) {
			matchedAud = true
			break
		}
	}
	if !matchedAud {
		return nil, errNotAuthenticated
	}

	now := a.now()
	exp := time.Unix(*claims.ExpiresAt, 0)
	if !now.Before(exp.Add(a.clockSkew)) {
		return nil, errNotAuthenticated
	}
	if claims.NotBefore != nil {
		nbf := time.Unix(*claims.NotBefore, 0)
		if now.Before(nbf.Add(-a.clockSkew)) {
			return nil, errNotAuthenticated
		}
	}

	var (
		uid    string
		groups []string
	)
	if after, ok := strings.CutPrefix(claims.Subject, actorSubjectPrefix); ok {
		parts := strings.Split(after, "/")
		if len(parts) != 2 || parts[0] == "" || parts[1] == "" {
			return nil, errNotAuthenticated
		}
		atespace, actorName := parts[0], parts[1]
		if claims.Actor != nil {
			if (claims.Actor.Atespace != "" && claims.Actor.Atespace != atespace) ||
				(claims.Actor.ActorName != "" && claims.Actor.ActorName != actorName) {
				return nil, errNotAuthenticated
			}
			uid = claims.Actor.ActorUID
		}
		if a.allowedAtespaces.Len() > 0 && !a.allowedAtespaces.Has(atespace) {
			return nil, fmt.Errorf("atespace %q is not allowed", atespace)
		}
		groups = []string{atespaceGroupPref + atespace}
	} else if a.allowedAtespaces.Len() > 0 {
		return nil, errNotAuthenticated
	}

	return &CallerIdentity{
		Username: claims.Subject,
		UID:      uid,
		Groups:   groups,
	}, nil
}

func (a *oidcAuthenticator) verificationKeys(ctx context.Context, wantKID string, force bool) ([]parsedJWK, error) {
	a.mu.Lock()
	defer a.mu.Unlock()

	now := a.now()
	if !force && len(a.keys) > 0 && now.Sub(a.fetchedAt) < a.cacheTTL {
		return slices.Clone(a.keys), nil
	}
	if force && len(a.keys) > 0 && now.Sub(a.lastRefreshAt) < defaultOIDCMinRefreshDelay {
		return slices.Clone(a.keys), nil
	}

	ctx, cancel := context.WithTimeout(ctx, defaultOIDCHTTPTimeout)
	defer cancel()

	if a.jwksURI == "" {
		jwksURI, err := a.fetchDiscovery(ctx)
		if err != nil {
			return nil, err
		}
		a.jwksURI = jwksURI
	}

	keys, err := a.fetchJWKS(ctx, a.jwksURI)
	if err != nil {
		// Re-run discovery once in case jwks_uri changed.
		if jwksURI, discErr := a.fetchDiscovery(ctx); discErr == nil && jwksURI != a.jwksURI {
			a.jwksURI = jwksURI
			keys, err = a.fetchJWKS(ctx, a.jwksURI)
		}
		if err != nil {
			return nil, err
		}
	}

	a.keys = keys
	a.fetchedAt = now
	a.lastRefreshAt = now
	_ = wantKID
	return slices.Clone(a.keys), nil
}

type oidcDiscoveryDoc struct {
	Issuer  string `json:"issuer"`
	JWKSURI string `json:"jwks_uri"`
}

func (a *oidcAuthenticator) fetchDiscovery(ctx context.Context) (string, error) {
	body, err := a.getPinnedPath(ctx, "/.well-known/openid-configuration", "")
	if err != nil {
		return "", fmt.Errorf("fetching OIDC discovery document: %w", err)
	}
	var doc oidcDiscoveryDoc
	if err := json.Unmarshal(body, &doc); err != nil {
		return "", fmt.Errorf("decoding OIDC discovery document: %w", err)
	}
	if doc.Issuer != a.issuer {
		return "", fmt.Errorf("OIDC discovery issuer %q does not match %q", doc.Issuer, a.issuer)
	}
	u, err := url.Parse(doc.JWKSURI)
	if err != nil || u.Scheme != "https" || u.Host != a.issuerHost || u.User != nil {
		return "", fmt.Errorf("OIDC jwks_uri %q must use https on host %q", doc.JWKSURI, a.issuerHost)
	}
	return doc.JWKSURI, nil
}

type jwksDoc struct {
	Keys []rawJWK `json:"keys"`
}

type rawJWK struct {
	KeyType string `json:"kty"`
	Use     string `json:"use"`
	Alg     string `json:"alg"`
	KeyID   string `json:"kid"`
	Curve   string `json:"crv"`
	X       string `json:"x"`
	Y       string `json:"y"`
	N       string `json:"n"`
	E       string `json:"e"`
}

func (a *oidcAuthenticator) fetchJWKS(ctx context.Context, jwksURI string) ([]parsedJWK, error) {
	u, err := url.Parse(jwksURI)
	if err != nil || u.Scheme != "https" || u.Host != a.issuerHost || u.User != nil {
		return nil, fmt.Errorf("OIDC jwks_uri %q must use https on host %q", jwksURI, a.issuerHost)
	}
	body, err := a.getPinnedPath(ctx, u.Path, u.RawQuery)
	if err != nil {
		return nil, fmt.Errorf("fetching JWKS: %w", err)
	}
	var doc jwksDoc
	if err := json.Unmarshal(body, &doc); err != nil {
		return nil, fmt.Errorf("decoding JWKS: %w", err)
	}
	var parsed []parsedJWK
	for _, raw := range doc.Keys {
		if raw.Use != "" && raw.Use != "sig" {
			continue
		}
		pub, err := parseRawJWK(raw)
		if err != nil {
			continue
		}
		parsed = append(parsed, parsedJWK{
			kid: raw.KeyID,
			alg: raw.Alg,
			pub: pub,
		})
	}
	if len(parsed) == 0 {
		return nil, errors.New("JWKS contains no supported signing keys")
	}
	return parsed, nil
}

func (a *oidcAuthenticator) getPinnedPath(ctx context.Context, path, rawQuery string) ([]byte, error) {
	target := (&url.URL{
		Scheme:   "https",
		Host:     a.issuerHost,
		Path:     path,
		RawQuery: rawQuery,
	}).String()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, target, nil) // #nosec G704 -- scheme and host are pinned to the operator-configured OIDC issuer
	if err != nil {
		return nil, err
	}
	resp, err := a.httpClient.Do(req) // #nosec G704 -- scheme and host are pinned to the operator-configured OIDC issuer and redirects are disabled
	if err != nil {
		return nil, err
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("unexpected HTTP status %d", resp.StatusCode)
	}
	data, err := io.ReadAll(io.LimitReader(resp.Body, maxOIDCResponseBytes+1))
	if err != nil {
		return nil, err
	}
	if len(data) > maxOIDCResponseBytes {
		return nil, errors.New("OIDC response exceeds 1 MiB limit")
	}
	return data, nil
}

func parseRawJWK(raw rawJWK) (crypto.PublicKey, error) {
	switch raw.KeyType {
	case "EC":
		var curve elliptic.Curve
		var coordLen int
		switch raw.Curve {
		case "P-256":
			curve, coordLen = elliptic.P256(), 32
		case "P-384":
			curve, coordLen = elliptic.P384(), 48
		default:
			return nil, fmt.Errorf("unsupported EC curve %q", raw.Curve)
		}
		xBytes, err := base64.RawURLEncoding.DecodeString(raw.X)
		if err != nil || len(xBytes) != coordLen {
			return nil, errors.New("invalid EC x coordinate")
		}
		yBytes, err := base64.RawURLEncoding.DecodeString(raw.Y)
		if err != nil || len(yBytes) != coordLen {
			return nil, errors.New("invalid EC y coordinate")
		}
		uncompressed := make([]byte, 1+2*coordLen)
		uncompressed[0] = 0x04
		copy(uncompressed[1:1+coordLen], xBytes)
		copy(uncompressed[1+coordLen:], yBytes)
		return ecdsa.ParseUncompressedPublicKey(curve, uncompressed)
	case "RSA":
		nBytes, err := base64.RawURLEncoding.DecodeString(raw.N)
		if err != nil || len(nBytes) == 0 {
			return nil, errors.New("invalid RSA modulus")
		}
		eBytes, err := base64.RawURLEncoding.DecodeString(raw.E)
		if err != nil || len(eBytes) == 0 || len(eBytes) > 4 {
			return nil, errors.New("invalid RSA exponent")
		}
		n := new(big.Int).SetBytes(nBytes)
		if n.BitLen() < minRSAKeyBits {
			return nil, errors.New("RSA key shorter than 2048 bits")
		}
		eInt := int(new(big.Int).SetBytes(eBytes).Int64())
		if eInt < 3 || eInt%2 == 0 {
			return nil, errors.New("invalid RSA public exponent")
		}
		return &rsa.PublicKey{N: n, E: eInt}, nil
	case "OKP":
		if raw.Curve != "Ed25519" {
			return nil, fmt.Errorf("unsupported OKP curve %q", raw.Curve)
		}
		xBytes, err := base64.RawURLEncoding.DecodeString(raw.X)
		if err != nil || len(xBytes) != ed25519.PublicKeySize {
			return nil, errors.New("invalid Ed25519 public key")
		}
		return ed25519.PublicKey(xBytes), nil
	default:
		return nil, fmt.Errorf("unsupported JWK kty %q", raw.KeyType)
	}
}
