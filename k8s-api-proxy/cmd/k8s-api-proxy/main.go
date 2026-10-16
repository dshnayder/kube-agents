package main

import (
	"context"
	"crypto/tls"
	"errors"
	"flag"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/gke-labs/kube-agents/k8s-api-proxy/pkg/k8sapiproxy"
	httpstransport "github.com/gke-labs/kube-agents/k8s-api-proxy/pkg/transport/https"
)

func envOrDefault(key, fallback string) string {
	if val := strings.TrimSpace(os.Getenv(key)); val != "" {
		return val
	}
	return fallback
}

func envBoolOrDefault(key string, fallback bool) bool {
	val := strings.TrimSpace(strings.ToLower(os.Getenv(key)))
	switch val {
	case "1", "true", "yes":
		return true
	case "0", "false", "no":
		return false
	default:
		return fallback
	}
}

func main() {
	defaultCluster := envOrDefault("KUBE_PROXY_DEFAULT_CLUSTER", envOrDefault("KUBE_CONTEXT_NAME", "in-cluster"))

	var (
		listenAddr     = flag.String("listen-addr", envOrDefault("KUBE_PROXY_LISTEN_ADDR", ":8443"), "TCP address to listen on")
		tlsCertFile    = flag.String("tls-cert-file", envOrDefault("KUBE_PROXY_TLS_CERT_FILE", "/var/run/secrets/kubeagents/kube-proxy-tls/tls.crt"), "TLS server certificate PEM file (empty for plain HTTP)")
		tlsKeyFile     = flag.String("tls-key-file", envOrDefault("KUBE_PROXY_TLS_KEY_FILE", "/var/run/secrets/kubeagents/kube-proxy-tls/tls.key"), "TLS server private key PEM file")
		modeRaw        = flag.String("mode", envOrDefault("KUBE_PROXY_MODE", string(k8sapiproxy.ModeReadOnlyWithDryRun)), "Proxy policy mode: read-only, read-only-dry-run, or read-write")
		defaultClusterF = flag.String("default-cluster", defaultCluster, "Default cluster ID when /clusters/{id} prefix is omitted")
		redactSecrets  = flag.Bool("redact-secrets", envBoolOrDefault("KUBE_PROXY_REDACT_SECRETS", true), "Redact core/v1 Secret payloads and rewrite Accept headers to PartialObjectMetadata")
		kubeAPIServer  = flag.String("kube-api-server", envOrDefault("CREDENTIAL_PROXY_KUBE_API_SERVER", "https://kubernetes.default.svc"), "Host cluster Kubernetes API server URL")
		kubeCAFile     = flag.String("kube-ca-file", envOrDefault("CREDENTIAL_PROXY_KUBE_CA_FILE", "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"), "Host cluster CA bundle PEM file")
		kubeTokenFile  = flag.String("kube-token-file", envOrDefault("CREDENTIAL_PROXY_KUBE_TOKEN_FILE", "/var/run/secrets/kubernetes.io/serviceaccount/token"), "Host cluster ServiceAccount token file")
		audience       = flag.String("audience", envOrDefault("CREDENTIAL_PROXY_AUDIENCE", "kubeagents-credential-proxy"), "Required TokenReview JWT audience")
		allowedCallers = flag.String("allowed-callers", envOrDefault("CREDENTIAL_PROXY_ALLOWED_CALLERS", ""), "Comma-separated allowlist of caller ServiceAccount usernames")
	)
	flag.Parse()

	mode, err := k8sapiproxy.ParseMode(*modeRaw)
	if err != nil {
		log.Fatalf("invalid mode: %v", err)
	}

	var callers []string
	for _, entry := range strings.Split(*allowedCallers, ",") {
		if trimmed := strings.TrimSpace(entry); trimmed != "" {
			callers = append(callers, trimmed)
		}
	}

	authn, err := k8sapiproxy.NewTokenReviewAuthenticator(k8sapiproxy.TokenReviewConfig{
		APIServerURL:   *kubeAPIServer,
		CAFile:         *kubeCAFile,
		TokenFile:      *kubeTokenFile,
		Audience:       *audience,
		AllowedCallers: callers,
	})
	if err != nil {
		log.Fatalf("failed to initialize TokenReview authenticator: %v", err)
	}

	gcpTokens := httpstransport.NewGCPTokenSource(httpstransport.GCPTokenConfig{
		WIFAudience:       os.Getenv("CREDENTIAL_PROXY_WIF_AUDIENCE"),
		WIFServiceAccount: os.Getenv("CREDENTIAL_PROXY_WIF_SERVICE_ACCOUNT"),
		WIFTokenFile:      os.Getenv("CREDENTIAL_PROXY_WIF_TOKEN_FILE"),
	})

	upstream, err := httpstransport.NewTransport(httpstransport.Config{
		InClusterServerURL: *kubeAPIServer,
		InClusterCAFile:    *kubeCAFile,
		InClusterTokenFile: *kubeTokenFile,
		TokenProvider:      gcpTokens,
	})
	if err != nil {
		log.Fatalf("failed to initialize upstream HTTPS transport: %v", err)
	}

	handler, err := k8sapiproxy.NewHandler(k8sapiproxy.Config{
		Authenticator:    authn,
		Upstream:         upstream,
		DefaultClusterID: *defaultClusterF,
		Mode:             mode,
		RedactSecrets:    *redactSecrets,
	})
	if err != nil {
		log.Fatalf("failed to initialize k8s-api-proxy handler: %v", err)
	}

	srv := &http.Server{
		Addr:              *listenAddr,
		Handler:           handler,
		ReadHeaderTimeout: 15 * time.Second,
		TLSConfig: &tls.Config{
			MinVersion: tls.VersionTLS12,
		},
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	go func() {
		<-ctx.Done()
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		_ = srv.Shutdown(shutdownCtx)
	}()

	if *tlsCertFile != "" && *tlsKeyFile != "" {
		log.Printf("starting k8s-api-proxy (TLS) on %s (mode=%s, defaultCluster=%s, redactSecrets=%v)", *listenAddr, mode, *defaultClusterF, *redactSecrets)
		err = srv.ListenAndServeTLS(*tlsCertFile, *tlsKeyFile)
	} else {
		log.Printf("starting k8s-api-proxy (plain HTTP) on %s (mode=%s, defaultCluster=%s, redactSecrets=%v)", *listenAddr, mode, *defaultClusterF, *redactSecrets)
		err = srv.ListenAndServe()
	}
	if err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatalf("k8s-api-proxy server exited with error: %v", err)
	}
}
