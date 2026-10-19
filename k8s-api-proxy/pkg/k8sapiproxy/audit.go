package k8sapiproxy

import (
	"context"
	"io"
	"log/slog"
	"net/http"
	"os"
	"time"

	"go.opentelemetry.io/otel/trace"
	"k8s.io/apiserver/pkg/endpoints/request"
)

// AuditEventType is the structured event_type value emitted on every Kubernetes API audit log record.
const AuditEventType = "k8s_api_audit"

// NewAuditLogger constructs a structured JSON slog.Logger for Kubernetes API audit events.
// Request and response bodies and authentication credentials are never written to the audit log.
func NewAuditLogger(w io.Writer) *slog.Logger {
	if w == nil {
		w = os.Stdout
	}
	return slog.New(slog.NewJSONHandler(w, &slog.HandlerOptions{
		Level: slog.LevelInfo,
	}))
}

type auditEntry struct {
	caller         *CallerIdentity
	clusterID      string
	httpMethod     string
	path           string
	userAgent      string
	info           *request.RequestInfo
	mode           Mode
	dryRun         bool
	secretRedacted bool
	outcome        string
	reason         RejectionReason
	statusCode     int
	duration       time.Duration
	span           trace.Span
}

func logAuditEvent(ctx context.Context, logger *slog.Logger, entry auditEntry) {
	if logger == nil {
		return
	}

	verb := verbFromHTTPMethod(entry.httpMethod)
	resource := "unknown"
	subresource := ""
	apiGroup := ""
	apiVersion := ""
	namespace := ""
	name := ""

	if entry.info != nil {
		if !entry.info.IsResourceRequest {
			verb = "discovery"
			resource = "discovery"
		} else {
			if entry.info.Verb != "" {
				verb = entry.info.Verb
			}
			if entry.info.Resource != "" {
				resource = entry.info.Resource
			}
			subresource = entry.info.Subresource
			apiGroup = entry.info.APIGroup
			apiVersion = entry.info.APIVersion
			namespace = entry.info.Namespace
			name = entry.info.Name
		}
	}

	attrs := make([]slog.Attr, 0, 20)
	attrs = append(attrs,
		slog.String("event_type", AuditEventType),
		slog.String("mode", sanitizeModeLabel(entry.mode)),
		slog.String("outcome", entry.outcome),
		slog.Int("status_code", entry.statusCode),
		slog.String("http_method", entry.httpMethod),
		slog.String("path", entry.path),
		slog.String("verb", verb),
		slog.String("resource", resource),
		slog.Bool("dry_run", entry.dryRun),
		slog.Bool("secret_redacted", entry.secretRedacted),
		slog.Int64("duration_ms", entry.duration.Milliseconds()),
	)

	if entry.clusterID != "" {
		attrs = append(attrs, slog.String("cluster", entry.clusterID))
	}
	if apiGroup != "" {
		attrs = append(attrs, slog.String("api_group", apiGroup))
	}
	if apiVersion != "" {
		attrs = append(attrs, slog.String("api_version", apiVersion))
	}
	if subresource != "" {
		attrs = append(attrs, slog.String("subresource", subresource))
	}
	if namespace != "" {
		attrs = append(attrs, slog.String("namespace", namespace))
	}
	if name != "" {
		attrs = append(attrs, slog.String("name", name))
	}
	if entry.reason != ReasonNone {
		attrs = append(attrs, slog.String("rejection_reason", string(entry.reason)))
	}
	if entry.caller != nil {
		if entry.caller.Username != "" {
			attrs = append(attrs, slog.String("caller_username", entry.caller.Username))
		}
		if entry.caller.UID != "" {
			attrs = append(attrs, slog.String("caller_uid", entry.caller.UID))
		}
		if len(entry.caller.Groups) > 0 {
			attrs = append(attrs, slog.Any("caller_groups", entry.caller.Groups))
		}
	}
	if entry.userAgent != "" {
		attrs = append(attrs, slog.String("user_agent", entry.userAgent))
	}
	if entry.span != nil {
		sc := entry.span.SpanContext()
		if sc.IsValid() {
			attrs = append(attrs,
				slog.String("trace_id", sc.TraceID().String()),
				slog.String("span_id", sc.SpanID().String()),
			)
		}
	}

	level := slog.LevelInfo
	switch entry.outcome {
	case OutcomeRejectedAuth, OutcomeRejectedRoute, OutcomeRejectedPolicy:
		level = slog.LevelWarn
	case OutcomeUpstreamError:
		level = slog.LevelError
	}

	if r := recover(); r != nil {
		panic(r)
	}
	logger.LogAttrs(ctx, level, "kube-proxy audit", attrs...)
}

func auditPathForRequest(r *http.Request, cleanPath string) string {
	if cleanPath != "" {
		return cleanPath
	}
	if r != nil && r.URL != nil {
		return r.URL.Path
	}
	return ""
}
