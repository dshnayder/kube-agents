package k8sapiproxy

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/http/httputil"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apiserver/pkg/endpoints/request"
)

type contextKey int

const (
	clusterIDContextKey contextKey = iota
	requestInfoContextKey
	callerWantsPartialContextKey
)

// NewHandler constructs the Kubernetes API proxy HTTP handler.
func NewHandler(cfg Config) (http.Handler, error) {
	if cfg.Upstream == nil {
		return nil, errors.New("k8sapiproxy: Config.Upstream is required")
	}
	if cfg.Authenticator == nil {
		return nil, errors.New("k8sapiproxy: Config.Authenticator is required")
	}
	mode, err := ParseMode(string(cfg.Mode))
	if err != nil {
		return nil, err
	}
	defaultCluster := cfg.DefaultClusterID
	if defaultCluster == "" {
		defaultCluster = "in-cluster"
	}

	factory := NewRequestInfoFactory()

	h := &proxyHandler{
		authenticator:    cfg.Authenticator,
		upstream:         cfg.Upstream,
		defaultClusterID: defaultCluster,
		mode:             mode,
		redactSecrets:    cfg.RedactSecrets,
		factory:          factory,
	}

	h.reverseProxy = &httputil.ReverseProxy{
		FlushInterval: -1,
		Director: func(req *http.Request) {
			if req.URL.Scheme == "" {
				req.URL.Scheme = "https"
			}
			if req.URL.Host == "" {
				req.URL.Host = "kubernetes.default.svc"
			}
		},
		Transport: roundTripperFunc(func(req *http.Request) (*http.Response, error) {
			clusterID, _ := req.Context().Value(clusterIDContextKey).(string)
			return h.upstream.RoundTripCluster(req.Context(), clusterID, req)
		}),
		ModifyResponse: func(resp *http.Response) error {
			if !h.redactSecrets {
				return nil
			}
			info, _ := resp.Request.Context().Value(requestInfoContextKey).(*request.RequestInfo)
			if IsCoreV1Secret(info) {
				callerWantsPartial, _ := resp.Request.Context().Value(callerWantsPartialContextKey).(bool)
				ScrubSecretResponse(resp, info, callerWantsPartial)
			}
			return nil
		},
		ErrorHandler: func(w http.ResponseWriter, _ *http.Request, err error) {
			WriteStatusError(
				w,
				http.StatusBadGateway,
				metav1.StatusReasonInternalError,
				fmt.Sprintf("kube-proxy upstream transport error: %v", err),
			)
		},
	}

	return h, nil
}

type proxyHandler struct {
	authenticator    CallerAuthenticator
	upstream         UpstreamTransport
	defaultClusterID string
	mode             Mode
	redactSecrets    bool
	factory          *request.RequestInfoFactory
	reverseProxy     *httputil.ReverseProxy
}

func (h *proxyHandler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path == "/_kube_proxy/healthz" && (r.Method == http.MethodGet || r.Method == http.MethodHead) {
		w.Header().Set("Content-Type", "text/plain; charset=utf-8")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok\n"))
		return
	}

	if HasUpgradeHeader(r) {
		WriteStatusError(
			w,
			http.StatusForbidden,
			metav1.StatusReasonForbidden,
			"kube-proxy policy blocked protocol upgrade request: interactive streaming subresources are not permitted. This is a permission boundary, not an error to work around.",
		)
		return
	}

	if _, err := h.authenticator.AuthenticateRequest(r); err != nil {
		WriteStatusError(
			w,
			http.StatusUnauthorized,
			metav1.StatusReasonUnauthorized,
			fmt.Sprintf("kube-proxy caller authentication failed: %v", err),
		)
		return
	}

	clusterID, cleanPath, policyErr := NormalizeAndExtractCluster(r.URL.RawPath, r.URL.Path, h.defaultClusterID)
	if policyErr != nil {
		WriteStatusError(w, policyErr.Code, policyErr.Reason, policyErr.Message)
		return
	}

	outReq := r.Clone(r.Context())
	outReq.URL.Path = cleanPath
	outReq.URL.RawPath = ""
	SanitizeRequestHeaders(outReq.Header)

	info, err := h.factory.NewRequestInfo(outReq)
	if err != nil {
		WriteStatusError(
			w,
			http.StatusBadRequest,
			metav1.StatusReasonBadRequest,
			fmt.Sprintf("kube-proxy failed to parse Kubernetes API request path %q: %v", cleanPath, err),
		)
		return
	}

	if policyErr := EvaluatePolicy(h.mode, h.redactSecrets, outReq, info); policyErr != nil {
		WriteStatusError(w, policyErr.Code, policyErr.Reason, policyErr.Message)
		return
	}

	var callerWantsPartial bool
	if h.redactSecrets && IsCoreV1Secret(info) {
		callerWantsPartial = RewriteSecretRequestHeaders(outReq, info)
	}

	ctx := context.WithValue(outReq.Context(), clusterIDContextKey, clusterID)
	ctx = context.WithValue(ctx, requestInfoContextKey, info)
	ctx = context.WithValue(ctx, callerWantsPartialContextKey, callerWantsPartial)
	outReq = outReq.WithContext(ctx)

	h.reverseProxy.ServeHTTP(w, outReq)
}

type roundTripperFunc func(*http.Request) (*http.Response, error)

func (f roundTripperFunc) RoundTrip(r *http.Request) (*http.Response, error) {
	return f(r)
}
