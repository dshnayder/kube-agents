package k8sapiproxy

import (
	"net/http"
	"strings"
)

// HasUpgradeHeader returns true when r requests a protocol upgrade (such as SPDY or WebSocket).
func HasUpgradeHeader(r *http.Request) bool {
	if strings.TrimSpace(r.Header.Get("Upgrade")) != "" {
		return true
	}
	for _, connVal := range r.Header.Values("Connection") {
		for _, token := range strings.Split(connVal, ",") {
			if strings.EqualFold(strings.TrimSpace(token), "upgrade") {
				return true
			}
		}
	}
	return false
}

// SanitizeRequestHeaders removes caller credentials and Kubernetes impersonation headers from h.
func SanitizeRequestHeaders(h http.Header) {
	h.Del("Authorization")
	h.Del("Upgrade")
	for key := range h {
		lower := strings.ToLower(key)
		if strings.HasPrefix(lower, "impersonate-") || strings.HasPrefix(lower, "x-remote-") {
			h.Del(key)
		}
	}
}
