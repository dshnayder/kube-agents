package k8sapiproxy

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/http/httputil"
	"time"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/trace"
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

	metrics := cfg.Metrics
	if metrics == nil {
		metrics = NewMetrics()
	}

	factory := NewRequestInfoFactory()

	h := &proxyHandler{
		authenticator:    cfg.Authenticator,
		upstream:         cfg.Upstream,
		defaultClusterID: defaultCluster,
		mode:             mode,
		redactSecrets:    cfg.RedactSecrets,
		metrics:          metrics,
		tracerProvider:   cfg.TracerProvider,
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
			upCtx, upSpan := startUpstreamSpan(req.Context(), h.tracerProvider, clusterID, req.Method)
			defer upSpan.End()

			resp, err := h.upstream.RoundTripCluster(upCtx, clusterID, req.WithContext(upCtx))
			if err != nil {
				upSpan.RecordError(err)
				upSpan.SetStatus(codes.Error, err.Error())
				return nil, err
			}
			upSpan.SetAttributes(attribute.Int("http.response.status_code", resp.StatusCode))
			if resp.StatusCode >= http.StatusInternalServerError {
				upSpan.SetStatus(codes.Error, http.StatusText(resp.StatusCode))
			} else {
				upSpan.SetStatus(codes.Ok, "")
			}
			return resp, nil
		}),
		ModifyResponse: func(resp *http.Response) error {
			if !h.redactSecrets {
				return nil
			}
			info, _ := resp.Request.Context().Value(requestInfoContextKey).(*request.RequestInfo)
			if IsCoreV1Secret(info) {
				callerWantsPartial, _ := resp.Request.Context().Value(callerWantsPartialContextKey).(bool)
				ScrubSecretResponse(resp, info, callerWantsPartial)
				h.metrics.recordSecretRedaction(SecretActionPayloadScrubbed)
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
	metrics          *Metrics
	tracerProvider   trace.TracerProvider
	factory          *request.RequestInfoFactory
	reverseProxy     *httputil.ReverseProxy
}

func (h *proxyHandler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	start := time.Now()
	if r.URL.Path == "/_kube_proxy/healthz" && (r.Method == http.MethodGet || r.Method == http.MethodHead) {
		w.Header().Set("Content-Type", "text/plain; charset=utf-8")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok\n"))
		h.metrics.recordRequest("healthz", "healthz", metricsLabelNone, h.mode, false, OutcomeAllowed, http.StatusOK, time.Since(start))
		return
	}

	h.metrics.incInFlight()
	defer h.metrics.decInFlight()

	dryRun := HasStrictDryRunAll(r)
	spanCtx, span := startRequestSpan(h.tracerProvider, r, h.mode)
	r = r.WithContext(spanCtx)

	if _, err := h.authenticator.AuthenticateRequest(r); err != nil {
		verbLabel, resLabel, subLabel := ClassifyRequestLabels(r, nil)
		WriteStatusError(
			w,
			http.StatusUnauthorized,
			metav1.StatusReasonUnauthorized,
			fmt.Sprintf("kube-proxy caller authentication failed: %v", err),
		)
		h.metrics.recordRejection(ReasonUnauthenticated, verbLabel, resLabel, subLabel, h.mode)
		h.metrics.recordRequest(verbLabel, resLabel, subLabel, h.mode, dryRun, OutcomeRejectedAuth, http.StatusUnauthorized, time.Since(start))
		finishRequestSpan(span, OutcomeRejectedAuth, ReasonUnauthenticated, http.StatusUnauthorized)
		return
	}

	clusterID, cleanPath, policyErr := NormalizeAndExtractCluster(r.URL.RawPath, r.URL.Path, h.defaultClusterID)
	if policyErr != nil {
		verbLabel, resLabel, subLabel := ClassifyRequestLabels(r, nil)
		WriteStatusError(w, policyErr.Code, policyErr.Reason, policyErr.Message)
		h.metrics.recordRejection(policyErr.RejectionReason, verbLabel, resLabel, subLabel, h.mode)
		h.metrics.recordRequest(verbLabel, resLabel, subLabel, h.mode, dryRun, OutcomeRejectedRoute, policyErr.Code, time.Since(start))
		finishRequestSpan(span, OutcomeRejectedRoute, policyErr.RejectionReason, policyErr.Code)
		return
	}

	outReq := r.Clone(r.Context())
	outReq.URL.Path = cleanPath
	outReq.URL.RawPath = ""

	info, err := h.factory.NewRequestInfo(outReq)
	if err != nil {
		verbLabel, resLabel, subLabel := ClassifyRequestLabels(outReq, nil)
		WriteStatusError(
			w,
			http.StatusBadRequest,
			metav1.StatusReasonBadRequest,
			fmt.Sprintf("kube-proxy failed to parse Kubernetes API request path %q: %v", cleanPath, err),
		)
		h.metrics.recordRejection(ReasonInvalidRoute, verbLabel, resLabel, subLabel, h.mode)
		h.metrics.recordRequest(verbLabel, resLabel, subLabel, h.mode, dryRun, OutcomeRejectedRoute, http.StatusBadRequest, time.Since(start))
		finishRequestSpan(span, OutcomeRejectedRoute, ReasonInvalidRoute, http.StatusBadRequest)
		return
	}

	verbLabel, resLabel, subLabel := ClassifyRequestLabels(outReq, info)
	secretRedacted := h.redactSecrets && IsCoreV1Secret(info)
	annotateRequestSpanInfo(span, clusterID, info, dryRun, secretRedacted)

	if policyErr := EvaluatePolicy(h.mode, h.redactSecrets, outReq, info); policyErr != nil {
		WriteStatusError(w, policyErr.Code, policyErr.Reason, policyErr.Message)
		h.metrics.recordRejection(policyErr.RejectionReason, verbLabel, resLabel, subLabel, h.mode)
		h.metrics.recordRequest(verbLabel, resLabel, subLabel, h.mode, dryRun, OutcomeRejectedPolicy, policyErr.Code, time.Since(start))
		finishRequestSpan(span, OutcomeRejectedPolicy, policyErr.RejectionReason, policyErr.Code)
		return
	}

	SanitizeRequestHeaders(outReq.Header)

	var callerWantsPartial bool
	if secretRedacted {
		callerWantsPartial = RewriteSecretRequestHeaders(outReq, info)
		h.metrics.recordSecretRedaction(SecretActionAcceptHeaderRewritten)
	}

	ctx := context.WithValue(outReq.Context(), clusterIDContextKey, clusterID)
	ctx = context.WithValue(ctx, requestInfoContextKey, info)
	ctx = context.WithValue(ctx, callerWantsPartialContextKey, callerWantsPartial)
	outReq = outReq.WithContext(ctx)

	rec := &statusRecorder{ResponseWriter: w, statusCode: http.StatusOK}
	h.reverseProxy.ServeHTTP(rec, outReq)

	outcome := OutcomeAllowed
	if rec.statusCode >= http.StatusInternalServerError {
		outcome = OutcomeUpstreamError
	}
	h.metrics.recordRequest(verbLabel, resLabel, subLabel, h.mode, dryRun, outcome, rec.statusCode, time.Since(start))
	finishRequestSpan(span, outcome, ReasonNone, rec.statusCode)
}

type statusRecorder struct {
	http.ResponseWriter
	statusCode  int
	wroteHeader bool
}

func (r *statusRecorder) WriteHeader(code int) {
	if !r.wroteHeader {
		r.statusCode = code
		r.wroteHeader = true
	}
	r.ResponseWriter.WriteHeader(code)
}

func (r *statusRecorder) Write(b []byte) (int, error) {
	if !r.wroteHeader {
		r.statusCode = http.StatusOK
		r.wroteHeader = true
	}
	return r.ResponseWriter.Write(b)
}

func (r *statusRecorder) Flush() {
	if flusher, ok := r.ResponseWriter.(http.Flusher); ok {
		flusher.Flush()
	}
}

type roundTripperFunc func(*http.Request) (*http.Response, error)

func (f roundTripperFunc) RoundTrip(r *http.Request) (*http.Response, error) {
	return f(r)
}
