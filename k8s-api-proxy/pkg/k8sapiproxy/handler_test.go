package k8sapiproxy

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

type recordingTransport struct {
	lastClusterID string
	lastReq       *http.Request
	respStatus    int
	respBody      string
}

func (rt *recordingTransport) RoundTripCluster(_ context.Context, clusterID string, req *http.Request) (*http.Response, error) {
	rt.lastClusterID = clusterID
	rt.lastReq = req.Clone(context.Background())
	status := rt.respStatus
	if status == 0 {
		status = http.StatusOK
	}
	body := rt.respBody
	if body == "" {
		body = `{"kind":"PodList","apiVersion":"v1","items":[]}`
	}
	return &http.Response{
		StatusCode: status,
		Header:     http.Header{"Content-Type": []string{"application/json"}},
		Body:       io.NopCloser(strings.NewReader(body)),
		Request:    req,
	}, nil
}

type failingAuthenticator struct{}

func (failingAuthenticator) AuthenticateRequest(_ *http.Request) (*CallerIdentity, error) {
	return nil, errors.New("invalid token")
}

func TestHandlerEndToEnd(t *testing.T) {
	t.Run("unauthenticated healthz probe succeeds", func(t *testing.T) {
		upstream := &recordingTransport{}
		h, err := NewHandler(Config{
			Authenticator:    failingAuthenticator{},
			Upstream:         upstream,
			DefaultClusterID: "in-cluster",
			Mode:             ModeReadOnlyWithDryRun,
			RedactSecrets:    true,
		})
		if err != nil {
			t.Fatalf("NewHandler failed: %v", err)
		}

		rec := httptest.NewRecorder()
		req := httptest.NewRequest(http.MethodGet, "/_kube_proxy/healthz", nil)
		h.ServeHTTP(rec, req)
		if rec.Code != http.StatusOK {
			t.Fatalf("Code = %d, want 200", rec.Code)
		}
	})

	t.Run("unauthenticated API request returns 401 Status", func(t *testing.T) {
		upstream := &recordingTransport{}
		h, _ := NewHandler(Config{
			Authenticator:    failingAuthenticator{},
			Upstream:         upstream,
			DefaultClusterID: "in-cluster",
			Mode:             ModeReadOnlyWithDryRun,
			RedactSecrets:    true,
		})

		rec := httptest.NewRecorder()
		req := httptest.NewRequest(http.MethodGet, "/api/v1/pods", nil)
		h.ServeHTTP(rec, req)
		if rec.Code != http.StatusUnauthorized {
			t.Fatalf("Code = %d, want 401", rec.Code)
		}
	})

	t.Run("strips caller Authorization and Impersonate headers and routes cluster prefix", func(t *testing.T) {
		upstream := &recordingTransport{}
		h, _ := NewHandler(Config{
			Authenticator:    &StaticAuthenticator{Identity: CallerIdentity{Username: "system:serviceaccount:ns:shell"}},
			Upstream:         upstream,
			DefaultClusterID: "in-cluster",
			Mode:             ModeReadOnlyWithDryRun,
			RedactSecrets:    true,
		})

		rec := httptest.NewRecorder()
		req := httptest.NewRequest(http.MethodGet, "/clusters/gke_my-proj_us-central1_clu/api/v1/namespaces/kube-system/pods", nil)
		req.Header.Set("Authorization", "Bearer sandbox-jwt")
		req.Header.Set("Impersonate-User", "cluster-admin")
		req.Header.Set("Impersonate-Group", "system:masters")
		req.Header.Set("X-Remote-User", "admin")

		h.ServeHTTP(rec, req)
		if rec.Code != http.StatusOK {
			t.Fatalf("Code = %d, want 200 (body=%s)", rec.Code, rec.Body.String())
		}
		if upstream.lastClusterID != "gke_my-proj_us-central1_clu" {
			t.Errorf("clusterID = %q, want gke_my-proj_us-central1_clu", upstream.lastClusterID)
		}
		if upstream.lastReq.URL.Path != "/api/v1/namespaces/kube-system/pods" {
			t.Errorf("upstream path = %q, want /api/v1/namespaces/kube-system/pods", upstream.lastReq.URL.Path)
		}
		for _, hdr := range []string{"Authorization", "Impersonate-User", "Impersonate-Group", "X-Remote-User"} {
			if got := upstream.lastReq.Header.Get(hdr); got != "" {
				t.Errorf("expected header %s to be stripped, got %q", hdr, got)
			}
		}
	})

	t.Run("redacts Secret response end-to-end", func(t *testing.T) {
		upstream := &recordingTransport{
			respBody: `{"apiVersion":"v1","kind":"Secret","metadata":{"name":"my-sec"},"data":{"key":"dG9wLXNlY3JldA=="}}`,
		}
		h, _ := NewHandler(Config{
			Authenticator:    &StaticAuthenticator{Identity: CallerIdentity{Username: "system:serviceaccount:ns:shell"}},
			Upstream:         upstream,
			DefaultClusterID: "in-cluster",
			Mode:             ModeReadOnlyWithDryRun,
			RedactSecrets:    true,
		})

		rec := httptest.NewRecorder()
		req := httptest.NewRequest(http.MethodGet, "/api/v1/namespaces/default/secrets/my-sec", nil)
		req.Header.Set("Accept", "application/json")
		h.ServeHTTP(rec, req)
		if rec.Code != http.StatusOK {
			t.Fatalf("Code = %d, want 200", rec.Code)
		}
		if strings.Contains(rec.Body.String(), "dG9wLXNlY3JldA==") {
			t.Fatalf("response leaked secret data: %s", rec.Body.String())
		}
		var out map[string]any
		_ = json.Unmarshal(rec.Body.Bytes(), &out)
		dataMap, ok := out["data"].(map[string]any)
		if !ok || dataMap["key"] != "" {
			t.Errorf("expected data field key to be preserved with empty value, got %v", out["data"])
		}
	})
}
