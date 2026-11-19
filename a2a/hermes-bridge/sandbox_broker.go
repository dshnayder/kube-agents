package hermesbridge

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
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strings"
	"sync"
	"time"
)

const (
	// DefaultAXOIDCIssuerURL is the in-cluster OIDC issuer URL for Agent Substrate actor JWTs.
	DefaultAXOIDCIssuerURL = "https://idp.ate-system.svc"
	// DefaultAXSandboxBrokerAudience is the required JWT aud claim for /v1/sandbox/exec.
	DefaultAXSandboxBrokerAudience = "kubeagents-a2a-sandbox-broker"
	// SandboxBrokerExecPath is the HTTP path where ax-sandbox-ssh-proxy submits commands.
	SandboxBrokerExecPath = "/v1/sandbox/exec"

	sandboxBrokerReadHeaderTimeout = 15 * time.Second
	sandboxBrokerMaxRequestBytes   = 48 << 20
	oidcHTTPTimeout                = 10 * time.Second
	oidcJWKSCacheTTL               = 5 * time.Minute
	oidcMinRefreshDelay            = 10 * time.Second
	oidcClockSkew                  = 30 * time.Second
	maxOIDCDocBytes                = 1 << 20
	maxBearerHeaderBytes           = 16 << 10
	minOIDCRSAKeyBits              = 2048
)

var (
	actorSubjectRe   = regexp.MustCompile(`^actor/([^/]+)/([^/]+)$`)
	safeTaskNameRe   = regexp.MustCompile(`^[a-z0-9]([-a-z0-9]*[a-z0-9])?$`)
	safeEnvVarNameRe = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]*$`)

	errOIDCUnauthenticated = errors.New("unauthenticated OIDC bearer token")
	errOIDCUnavailable     = errors.New("OIDC discovery or JWKS unavailable")

	// Protected sandbox environment variables cannot be overridden by SSH SendEnv.
	protectedSandboxEnvKeys = map[string]bool{
		"HOME":                        true,
		"PATH":                        true,
		"KUBECONFIG":                  true,
		"KUBE_PROXY_URL":              true,
		"KUBE_PROXY_CA_FILE":          true,
		"CREDENTIAL_PROXY_URL":        true,
		"CREDENTIAL_PROXY_EXEC_URL":   true,
		"CREDENTIAL_PROXY_TOKEN_FILE": true,
		"SSL_CERT_FILE":               true,
		"LD_PRELOAD":                  true,
		"LD_LIBRARY_PATH":             true,
		"PYTHONPATH":                  true,
		"PYTHONSTARTUP":               true,
		"BASH_ENV":                    true,
		"ENV":                         true,
	}
)

// SandboxExecRequest is the JSON request payload accepted by POST /v1/sandbox/exec.
type SandboxExecRequest struct {
	Command  string            `json:"command"`
	StdinB64 string            `json:"stdin_b64,omitempty"`
	Stdin    string            `json:"stdin,omitempty"`
	Env      map[string]string `json:"env,omitempty"`
	Cwd      string            `json:"cwd,omitempty"`
}

// SandboxExecResponse is the JSON response payload returned by POST /v1/sandbox/exec.
type SandboxExecResponse struct {
	StdoutB64 string `json:"stdout_b64"`
	StderrB64 string `json:"stderr_b64"`
	Stdout    string `json:"stdout"`
	Stderr    string `json:"stderr"`
	ExitCode  int32  `json:"exit_code"`
}

func (b *Bridge) listenSandboxBroker() error {
	addr := strings.TrimSpace(b.cfg.AXSandboxBrokerListen)
	if addr == "" || b.cfg.Executor != ExecutorAX || b.axExec == nil {
		return nil
	}
	handler, err := newSandboxBrokerHandler(b.axExec, b.cfg)
	if err != nil {
		return fmt.Errorf("init AX sandbox broker: %w", err)
	}
	ln, err := net.Listen("tcp", addr)
	if err != nil {
		return fmt.Errorf("listen AX sandbox broker %q: %w", addr, err)
	}
	certFile := strings.TrimSpace(b.cfg.AXSandboxBrokerTLSCertFile)
	keyFile := strings.TrimSpace(b.cfg.AXSandboxBrokerTLSKeyFile)
	if certFile != "" {
		if keyFile == "" {
			keyFile = certFile
		}
		if _, err := tls.LoadX509KeyPair(certFile, keyFile); err != nil {
			_ = ln.Close()
			return fmt.Errorf("load AX sandbox broker TLS certificate %q: %w", certFile, err)
		}
		ln = tls.NewListener(ln, &tls.Config{
			MinVersion: tls.VersionTLS12,
			GetCertificate: func(*tls.ClientHelloInfo) (*tls.Certificate, error) {
				cert, loadErr := tls.LoadX509KeyPair(certFile, keyFile)
				if loadErr != nil {
					return nil, fmt.Errorf("reload AX sandbox broker TLS certificate %q: %w", certFile, loadErr)
				}
				return &cert, nil
			},
		})
	}
	b.sandboxBrokerLn = ln
	b.sandboxBrokerDone = make(chan struct{})
	b.sandboxBrokerSrv = &http.Server{
		Handler:           handler,
		ReadHeaderTimeout: sandboxBrokerReadHeaderTimeout,
	}
	return nil
}

// SandboxBrokerAddr returns the bound address of the AX sandbox broker listener, or "" when disabled.
func (b *Bridge) SandboxBrokerAddr() string {
	if b.sandboxBrokerLn == nil {
		return ""
	}
	return b.sandboxBrokerLn.Addr().String()
}

func (b *Bridge) serveSandboxBroker(ctx context.Context) {
	if b.sandboxBrokerLn == nil || b.sandboxBrokerSrv == nil {
		return
	}
	b.cfg.Logger.Info("AX sandbox broker listening", "addr", b.sandboxBrokerLn.Addr().String())
	go func() {
		select {
		case <-ctx.Done():
			b.closeSandboxBroker()
		case <-b.sandboxBrokerDone:
		}
	}()
	go func() {
		if err := b.sandboxBrokerSrv.Serve(b.sandboxBrokerLn); err != nil && !errors.Is(err, http.ErrServerClosed) {
			b.cfg.Logger.Error("AX sandbox broker stopped with error", "err", err)
		}
	}()
}

func (b *Bridge) closeSandboxBroker() {
	b.sandboxBrokerOnce.Do(func() {
		if b.sandboxBrokerDone != nil {
			close(b.sandboxBrokerDone)
		}
		if b.sandboxBrokerSrv != nil {
			shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()
			_ = b.sandboxBrokerSrv.Shutdown(shutdownCtx)
		}
		if b.sandboxBrokerLn != nil {
			_ = b.sandboxBrokerLn.Close()
		}
	})
}

type sandboxBrokerHandler struct {
	exec     *axExecutor
	verifier *oidcActorVerifier
	atespace string
}

func newSandboxBrokerHandler(exec *axExecutor, cfg Config) (http.Handler, error) {
	cfg.defaults()
	if exec == nil {
		return nil, errors.New("sandbox broker requires axExecutor")
	}
	verifier, err := newOIDCActorVerifier(oidcVerifierConfig{
		IssuerURL:  cfg.AXOIDCIssuerURL,
		Audience:   DefaultAXSandboxBrokerAudience,
		CAFile:     cfg.AXOIDCCAFile,
		HTTPClient: cfg.AXOIDCHTTPClient,
	})
	if err != nil {
		return nil, err
	}
	h := &sandboxBrokerHandler{
		exec:     exec,
		verifier: verifier,
		atespace: cfg.AXAtespace,
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", h.handleHealthz)
	mux.HandleFunc(SandboxBrokerExecPath, h.handleExec)
	return mux, nil
}

func (h *sandboxBrokerHandler) handleHealthz(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet && r.Method != http.MethodHead {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write([]byte(`{"status":"ok"}`))
}

func (h *sandboxBrokerHandler) handleExec(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}

	subject, err := h.verifier.VerifyRequest(r)
	if err != nil {
		if errors.Is(err, errOIDCUnavailable) {
			http.Error(w, "OIDC identity provider unavailable", http.StatusServiceUnavailable)
			return
		}
		http.Error(w, "unauthorized", http.StatusUnauthorized)
		return
	}

	matches := actorSubjectRe.FindStringSubmatch(subject)
	if matches == nil {
		http.Error(w, "forbidden: subject is not an actor identity", http.StatusForbidden)
		return
	}
	atespace, callerTask := matches[1], matches[2]
	if atespace != h.atespace {
		http.Error(w, "forbidden: actor atespace mismatch", http.StatusForbidden)
		return
	}
	if strings.HasSuffix(callerTask, axSandboxTaskSuffix) {
		http.Error(w, "forbidden: sandbox tasks cannot invoke sandbox broker", http.StatusForbidden)
		return
	}
	if !safeTaskNameRe.MatchString(callerTask) {
		http.Error(w, "forbidden: invalid caller task name", http.StatusForbidden)
		return
	}

	rawBody, err := io.ReadAll(io.LimitReader(r.Body, sandboxBrokerMaxRequestBytes+1))
	if err != nil {
		http.Error(w, "failed to read request body", http.StatusBadRequest)
		return
	}
	if len(rawBody) > sandboxBrokerMaxRequestBytes {
		http.Error(w, "request body exceeds size limit", http.StatusRequestEntityTooLarge)
		return
	}

	var req SandboxExecRequest
	if err := json.Unmarshal(rawBody, &req); err != nil {
		http.Error(w, "invalid JSON body", http.StatusBadRequest)
		return
	}
	if strings.TrimSpace(req.Command) == "" {
		http.Error(w, "command is required", http.StatusBadRequest)
		return
	}

	var stdinBytes []byte
	if req.StdinB64 != "" {
		stdinBytes, err = base64.StdEncoding.DecodeString(req.StdinB64)
		if err != nil {
			http.Error(w, "invalid stdin_b64 encoding", http.StatusBadRequest)
			return
		}
	} else if req.Stdin != "" {
		stdinBytes = []byte(req.Stdin)
	}

	filteredEnv := filterCallerSandboxEnv(req.Env)
	stdoutBytes, stderrBytes, exitCode, err := h.exec.ExecInPairedSandbox(
		r.Context(),
		atespace,
		callerTask,
		req.Command,
		req.Cwd,
		filteredEnv,
		stdinBytes,
	)
	if err != nil {
		http.Error(w, fmt.Sprintf("sandbox execution failed: %v", err), http.StatusBadGateway)
		return
	}

	resp := SandboxExecResponse{
		StdoutB64: base64.StdEncoding.EncodeToString(stdoutBytes),
		StderrB64: base64.StdEncoding.EncodeToString(stderrBytes),
		Stdout:    string(stdoutBytes),
		Stderr:    string(stderrBytes),
		ExitCode:  exitCode,
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	_ = json.NewEncoder(w).Encode(resp)
}

func filterCallerSandboxEnv(in map[string]string) map[string]string {
	if len(in) == 0 {
		return nil
	}
	out := make(map[string]string, len(in))
	for k, v := range in {
		if !safeEnvVarNameRe.MatchString(k) {
			continue
		}
		upper := strings.ToUpper(k)
		if protectedSandboxEnvKeys[upper] || strings.HasPrefix(upper, "LD_") || strings.HasPrefix(upper, "AX_") {
			continue
		}
		out[k] = v
	}
	return out
}

type oidcVerifierConfig struct {
	IssuerURL  string
	Audience   string
	CAFile     string
	HTTPClient *http.Client
	Now        func() time.Time
}

type oidcActorVerifier struct {
	issuer     string
	issuerHost string
	audience   string
	now        func() time.Time
	httpClient *http.Client

	mu            sync.Mutex
	jwksURI       string
	keys          []oidcParsedJWK
	fetchedAt     time.Time
	lastRefreshAt time.Time
}

type oidcParsedJWK struct {
	kid string
	alg string
	pub crypto.PublicKey
}

func newOIDCActorVerifier(cfg oidcVerifierConfig) (*oidcActorVerifier, error) {
	issuer := strings.TrimSpace(cfg.IssuerURL)
	if issuer == "" {
		return nil, errors.New("OIDC issuer URL is required")
	}
	u, err := url.Parse(issuer)
	if err != nil || u.Scheme != "https" || u.Hostname() == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" || strings.HasSuffix(u.Path, "/") {
		return nil, fmt.Errorf("invalid OIDC issuer URL %q: must be https://<host>[:port] without trailing slash", issuer)
	}
	aud := strings.TrimSpace(cfg.Audience)
	if aud == "" {
		return nil, errors.New("OIDC audience is required")
	}
	nowFn := cfg.Now
	if nowFn == nil {
		nowFn = time.Now
	}
	httpClient := cfg.HTTPClient
	if httpClient == nil {
		httpClient, err = newReloadingOIDCHTTPClient(strings.TrimSpace(cfg.CAFile))
		if err != nil {
			return nil, fmt.Errorf("configure OIDC CA bundle: %w", err)
		}
	}
	return &oidcActorVerifier{
		issuer:     issuer,
		issuerHost: u.Host,
		audience:   aud,
		now:        nowFn,
		httpClient: httpClient,
	}, nil
}

type caReloadingTransport struct {
	base   *http.Transport
	caFile string

	mu        sync.Mutex
	rawPEM    []byte
	transport *http.Transport
}

func newReloadingOIDCHTTPClient(caFile string) (*http.Client, error) {
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
		return &http.Client{Timeout: oidcHTTPTimeout, Transport: tr, CheckRedirect: noRedirect}, nil
	}
	cleanPath := filepath.Clean(caFile)
	pemBytes, err := os.ReadFile(cleanPath) // #nosec G304 -- path is configured by the operator
	if err != nil {
		return nil, err
	}
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM(pemBytes) {
		return nil, fmt.Errorf("no PEM certificates found in %q", cleanPath)
	}
	tr := base.Clone()
	tr.TLSClientConfig = &tls.Config{MinVersion: tls.VersionTLS12, RootCAs: pool}
	rt := &caReloadingTransport{
		base:      base,
		caFile:    cleanPath,
		rawPEM:    pemBytes,
		transport: tr,
	}
	return &http.Client{Timeout: oidcHTTPTimeout, Transport: rt, CheckRedirect: noRedirect}, nil
}

func (t *caReloadingTransport) RoundTrip(req *http.Request) (*http.Response, error) {
	return t.current().RoundTrip(req)
}

func (t *caReloadingTransport) current() *http.Transport {
	t.mu.Lock()
	defer t.mu.Unlock()
	pemBytes, err := os.ReadFile(t.caFile) // #nosec G304 -- path is configured by the operator
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

func (v *oidcActorVerifier) VerifyRequest(r *http.Request) (string, error) {
	token, ok := parseBearerHeader(r.Header.Get("Authorization"))
	if !ok {
		return "", errOIDCUnauthenticated
	}
	hdr, payload, sig, signingInput, err := parseCompactJWT(token)
	if err != nil {
		return "", errOIDCUnauthenticated
	}
	keys, err := v.loadKeys(r.Context(), false)
	if err != nil {
		return "", fmt.Errorf("%w: %w", errOIDCUnavailable, err)
	}
	if !verifyJWSWithKeys(keys, hdr, signingInput, sig) {
		if hdr.KeyID != "" && !containsKID(keys, hdr.KeyID) {
			refreshed, refreshErr := v.loadKeys(r.Context(), true)
			if refreshErr != nil {
				return "", fmt.Errorf("%w: %w", errOIDCUnavailable, refreshErr)
			}
			if !verifyJWSWithKeys(refreshed, hdr, signingInput, sig) {
				return "", errOIDCUnauthenticated
			}
		} else {
			return "", errOIDCUnauthenticated
		}
	}
	return v.validateActorClaims(payload)
}

func parseBearerHeader(raw string) (string, bool) {
	trimmed := strings.TrimSpace(raw)
	if len(trimmed) < len("Bearer ")+1 || len(trimmed) > maxBearerHeaderBytes {
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

type jwsHeader struct {
	Alg   string `json:"alg"`
	KeyID string `json:"kid"`
}

func parseCompactJWT(token string) (jwsHeader, []byte, []byte, []byte, error) {
	firstDot := strings.IndexByte(token, '.')
	if firstDot <= 0 {
		return jwsHeader{}, nil, nil, nil, errors.New("malformed JWT")
	}
	rest := token[firstDot+1:]
	secondDot := strings.IndexByte(rest, '.')
	if secondDot <= 0 || strings.IndexByte(rest[secondDot+1:], '.') >= 0 {
		return jwsHeader{}, nil, nil, nil, errors.New("malformed JWT")
	}
	hdrBytes, err := base64.RawURLEncoding.DecodeString(token[:firstDot])
	if err != nil {
		return jwsHeader{}, nil, nil, nil, err
	}
	var hdr jwsHeader
	if err := json.Unmarshal(hdrBytes, &hdr); err != nil {
		return jwsHeader{}, nil, nil, nil, err
	}
	switch hdr.Alg {
	case "ES256", "ES384", "RS256", "RS384", "RS512", "EdDSA":
	default:
		return jwsHeader{}, nil, nil, nil, errors.New("unsupported JWT alg")
	}
	payload, err := base64.RawURLEncoding.DecodeString(rest[:secondDot])
	if err != nil {
		return jwsHeader{}, nil, nil, nil, err
	}
	sig, err := base64.RawURLEncoding.DecodeString(rest[secondDot+1:])
	if err != nil {
		return jwsHeader{}, nil, nil, nil, err
	}
	return hdr, payload, sig, []byte(token[:firstDot+1+secondDot]), nil
}

func containsKID(keys []oidcParsedJWK, kid string) bool {
	for _, k := range keys {
		if k.kid == kid {
			return true
		}
	}
	return false
}

func verifyJWSWithKeys(keys []oidcParsedJWK, hdr jwsHeader, signingInput, sig []byte) bool {
	for _, k := range keys {
		if hdr.KeyID != "" && k.kid != "" && k.kid != hdr.KeyID {
			continue
		}
		if k.alg != "" && k.alg != hdr.Alg {
			continue
		}
		if verifyJWSSignature(k.pub, hdr.Alg, signingInput, sig) {
			return true
		}
	}
	return false
}

func verifyJWSSignature(pub crypto.PublicKey, alg string, signingInput, sig []byte) bool {
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
		if !ok || rsaKey.N.BitLen() < minOIDCRSAKeyBits {
			return false
		}
		sum := sha256.Sum256(signingInput)
		return rsa.VerifyPKCS1v15(rsaKey, crypto.SHA256, sum[:], sig) == nil
	case "RS384":
		rsaKey, ok := pub.(*rsa.PublicKey)
		if !ok || rsaKey.N.BitLen() < minOIDCRSAKeyBits {
			return false
		}
		sum := sha512.Sum384(signingInput)
		return rsa.VerifyPKCS1v15(rsaKey, crypto.SHA384, sum[:], sig) == nil
	case "RS512":
		rsaKey, ok := pub.(*rsa.PublicKey)
		if !ok || rsaKey.N.BitLen() < minOIDCRSAKeyBits {
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

type oidcAudienceList []string

func (a *oidcAudienceList) UnmarshalJSON(data []byte) error {
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

type oidcActorClaims struct {
	Issuer    string           `json:"iss"`
	Subject   string           `json:"sub"`
	Audience  oidcAudienceList `json:"aud"`
	ExpiresAt *int64           `json:"exp"`
	NotBefore *int64           `json:"nbf"`
}

func (v *oidcActorVerifier) validateActorClaims(payload []byte) (string, error) {
	var claims oidcActorClaims
	if err := json.Unmarshal(payload, &claims); err != nil {
		return "", errOIDCUnauthenticated
	}
	if claims.Issuer != v.issuer || claims.Subject == "" || claims.ExpiresAt == nil {
		return "", errOIDCUnauthenticated
	}
	if !slices.Contains(claims.Audience, v.audience) {
		return "", errOIDCUnauthenticated
	}
	now := v.now()
	exp := time.Unix(*claims.ExpiresAt, 0)
	if !now.Before(exp.Add(oidcClockSkew)) {
		return "", errOIDCUnauthenticated
	}
	if claims.NotBefore != nil {
		nbf := time.Unix(*claims.NotBefore, 0)
		if now.Before(nbf.Add(-oidcClockSkew)) {
			return "", errOIDCUnauthenticated
		}
	}
	return claims.Subject, nil
}

func (v *oidcActorVerifier) loadKeys(ctx context.Context, force bool) ([]oidcParsedJWK, error) {
	v.mu.Lock()
	defer v.mu.Unlock()

	now := v.now()
	if !force && len(v.keys) > 0 && now.Sub(v.fetchedAt) < oidcJWKSCacheTTL {
		return slices.Clone(v.keys), nil
	}
	if force && len(v.keys) > 0 && now.Sub(v.lastRefreshAt) < oidcMinRefreshDelay {
		return slices.Clone(v.keys), nil
	}

	fetchCtx, cancel := context.WithTimeout(ctx, oidcHTTPTimeout)
	defer cancel()

	if v.jwksURI == "" {
		uri, err := v.fetchDiscoveryDoc(fetchCtx)
		if err != nil {
			return nil, err
		}
		v.jwksURI = uri
	}
	keys, err := v.fetchJWKSDoc(fetchCtx, v.jwksURI)
	if err != nil {
		if uri, discErr := v.fetchDiscoveryDoc(fetchCtx); discErr == nil && uri != v.jwksURI {
			v.jwksURI = uri
			keys, err = v.fetchJWKSDoc(fetchCtx, v.jwksURI)
		}
		if err != nil {
			return nil, err
		}
	}
	v.keys = keys
	v.fetchedAt = now
	v.lastRefreshAt = now
	return slices.Clone(v.keys), nil
}

func (v *oidcActorVerifier) fetchDiscoveryDoc(ctx context.Context) (string, error) {
	body, err := v.getPinned(ctx, "/.well-known/openid-configuration", "")
	if err != nil {
		return "", err
	}
	var doc struct {
		Issuer  string `json:"issuer"`
		JWKSURI string `json:"jwks_uri"`
	}
	if err := json.Unmarshal(body, &doc); err != nil {
		return "", err
	}
	if doc.Issuer != v.issuer {
		return "", fmt.Errorf("OIDC discovery issuer %q does not match %q", doc.Issuer, v.issuer)
	}
	u, err := url.Parse(doc.JWKSURI)
	if err != nil || u.Scheme != "https" || u.Host != v.issuerHost || u.User != nil {
		return "", fmt.Errorf("OIDC jwks_uri %q must use https on host %q", doc.JWKSURI, v.issuerHost)
	}
	return doc.JWKSURI, nil
}

type oidcRawJWK struct {
	Kty string `json:"kty"`
	Use string `json:"use"`
	Alg string `json:"alg"`
	Kid string `json:"kid"`
	Crv string `json:"crv"`
	X   string `json:"x"`
	Y   string `json:"y"`
	N   string `json:"n"`
	E   string `json:"e"`
}

func (v *oidcActorVerifier) fetchJWKSDoc(ctx context.Context, jwksURI string) ([]oidcParsedJWK, error) {
	u, err := url.Parse(jwksURI)
	if err != nil || u.Scheme != "https" || u.Host != v.issuerHost || u.User != nil {
		return nil, fmt.Errorf("OIDC jwks_uri %q must use https on host %q", jwksURI, v.issuerHost)
	}
	body, err := v.getPinned(ctx, u.Path, u.RawQuery)
	if err != nil {
		return nil, err
	}
	var doc struct {
		Keys []oidcRawJWK `json:"keys"`
	}
	if err := json.Unmarshal(body, &doc); err != nil {
		return nil, err
	}
	var parsed []oidcParsedJWK
	for _, raw := range doc.Keys {
		if raw.Use != "" && raw.Use != "sig" {
			continue
		}
		pub, err := decodeRawJWK(raw)
		if err != nil {
			continue
		}
		parsed = append(parsed, oidcParsedJWK{kid: raw.Kid, alg: raw.Alg, pub: pub})
	}
	if len(parsed) == 0 {
		return nil, errors.New("JWKS contains no supported signing keys")
	}
	return parsed, nil
}

func (v *oidcActorVerifier) getPinned(ctx context.Context, path, rawQuery string) ([]byte, error) {
	target := (&url.URL{
		Scheme:   "https",
		Host:     v.issuerHost,
		Path:     path,
		RawQuery: rawQuery,
	}).String()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, target, nil) // #nosec G704 -- pinned to configured OIDC issuer
	if err != nil {
		return nil, err
	}
	resp, err := v.httpClient.Do(req) // #nosec G704 -- pinned to configured OIDC issuer with redirects disabled
	if err != nil {
		return nil, err
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("unexpected HTTP status %d", resp.StatusCode)
	}
	data, err := io.ReadAll(io.LimitReader(resp.Body, maxOIDCDocBytes+1))
	if err != nil {
		return nil, err
	}
	if len(data) > maxOIDCDocBytes {
		return nil, errors.New("OIDC document exceeds 1 MiB limit")
	}
	return data, nil
}

func decodeRawJWK(raw oidcRawJWK) (crypto.PublicKey, error) {
	switch raw.Kty {
	case "EC":
		var curve elliptic.Curve
		var coordLen int
		switch raw.Crv {
		case "P-256":
			curve, coordLen = elliptic.P256(), 32
		case "P-384":
			curve, coordLen = elliptic.P384(), 48
		default:
			return nil, fmt.Errorf("unsupported EC curve %q", raw.Crv)
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
		if n.BitLen() < minOIDCRSAKeyBits {
			return nil, errors.New("RSA key shorter than 2048 bits")
		}
		eInt := int(new(big.Int).SetBytes(eBytes).Int64())
		if eInt < 3 || eInt%2 == 0 {
			return nil, errors.New("invalid RSA exponent")
		}
		return &rsa.PublicKey{N: n, E: eInt}, nil
	case "OKP":
		if raw.Crv != "Ed25519" {
			return nil, fmt.Errorf("unsupported OKP curve %q", raw.Crv)
		}
		xBytes, err := base64.RawURLEncoding.DecodeString(raw.X)
		if err != nil || len(xBytes) != ed25519.PublicKeySize {
			return nil, errors.New("invalid Ed25519 public key")
		}
		return ed25519.PublicKey(xBytes), nil
	default:
		return nil, fmt.Errorf("unsupported JWK kty %q", raw.Kty)
	}
}
