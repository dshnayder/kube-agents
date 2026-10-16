package k8sapiproxy

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestRewriteSecretRequestHeaders(t *testing.T) {
	factory := NewRequestInfoFactory()

	t.Run("get secret rewrites Accept to PartialObjectMetadata", func(t *testing.T) {
		req := httptest.NewRequest(http.MethodGet, "/api/v1/namespaces/default/secrets/my-secret", nil)
		req.Header.Set("Accept", "application/json")
		info, _ := factory.NewRequestInfo(req)
		callerPartial := RewriteSecretRequestHeaders(req, info)
		if callerPartial {
			t.Errorf("expected callerPartial=false")
		}
		if got := req.Header.Get("Accept"); got != partialObjectMetadataAccept {
			t.Errorf("Accept = %q, want %q", got, partialObjectMetadataAccept)
		}
		if got := req.Header.Get("Accept-Encoding"); got != "identity" {
			t.Errorf("Accept-Encoding = %q, want identity", got)
		}
	})

	t.Run("list secrets with as=Table preserves Table Accept", func(t *testing.T) {
		req := httptest.NewRequest(http.MethodGet, "/api/v1/namespaces/default/secrets", nil)
		req.Header.Set("Accept", "application/json;as=Table;v=v1;g=meta.k8s.io,application/json")
		info, _ := factory.NewRequestInfo(req)
		RewriteSecretRequestHeaders(req, info)
		if got := req.Header.Get("Accept"); got != tableAccept {
			t.Errorf("Accept = %q, want %q", got, tableAccept)
		}
	})
}

func TestScrubSecretResponse(t *testing.T) {
	factory := NewRequestInfoFactory()

	t.Run("scrubs data, stringData, and last-applied-configuration from Secret", func(t *testing.T) {
		rawSecret := `{
			"apiVersion": "v1",
			"kind": "Secret",
			"metadata": {
				"name": "db-creds",
				"namespace": "default",
				"annotations": {
					"kubectl.kubernetes.io/last-applied-configuration": "{\"data\":{\"password\":\"c2VjcmV0\"}}",
					"app.kubernetes.io/managed-by": "helm"
				}
			},
			"data": {
				"password": "c2VjcmV0"
			},
			"stringData": {
				"token": "plaintext-secret"
			}
		}`
		req := httptest.NewRequest(http.MethodGet, "/api/v1/namespaces/default/secrets/db-creds", nil)
		info, _ := factory.NewRequestInfo(req)
		resp := &http.Response{
			StatusCode: http.StatusOK,
			Header:     make(http.Header),
			Body:       io.NopCloser(strings.NewReader(rawSecret)),
		}

		ScrubSecretResponse(resp, info, false)
		bodyBytes, err := io.ReadAll(resp.Body)
		if err != nil {
			t.Fatalf("failed to read scrubbed body: %v", err)
		}
		bodyStr := string(bodyBytes)
		if strings.Contains(bodyStr, "c2VjcmV0") || strings.Contains(bodyStr, "plaintext-secret") || strings.Contains(bodyStr, "last-applied-configuration") {
			t.Fatalf("scrubbed body leaked secret content: %s", bodyStr)
		}

		var parsed map[string]any
		if err := json.Unmarshal(bodyBytes, &parsed); err != nil {
			t.Fatalf("scrubbed output is not valid JSON: %v", err)
		}
		if _, hasData := parsed["data"]; hasData {
			t.Errorf("expected data field to be removed")
		}
		if _, hasStringData := parsed["stringData"]; hasStringData {
			t.Errorf("expected stringData field to be removed")
		}
		meta := parsed["metadata"].(map[string]any)
		annotations := meta["annotations"].(map[string]any)
		if annotations["app.kubernetes.io/managed-by"] != "helm" {
			t.Errorf("expected non-sensitive annotation to be preserved")
		}
	})

	t.Run("normalizes PartialObjectMetadata to Secret when caller did not request PartialObjectMetadata", func(t *testing.T) {
		rawPartial := `{
			"apiVersion": "meta.k8s.io/v1",
			"kind": "PartialObjectMetadata",
			"metadata": {
				"name": "tls-secret",
				"namespace": "default"
			}
		}`
		scrubbed, err := ScrubSecretJSONBytes([]byte(rawPartial), false)
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		var parsed map[string]any
		_ = json.Unmarshal(scrubbed, &parsed)
		if parsed["apiVersion"] != "v1" || parsed["kind"] != "Secret" {
			t.Errorf("got apiVersion=%v kind=%v, want v1/Secret", parsed["apiVersion"], parsed["kind"])
		}
	})

	t.Run("fails closed with 502 on malformed JSON", func(t *testing.T) {
		req := httptest.NewRequest(http.MethodGet, "/api/v1/namespaces/default/secrets/db-creds", nil)
		info, _ := factory.NewRequestInfo(req)
		resp := &http.Response{
			StatusCode: http.StatusOK,
			Header:     make(http.Header),
			Body:       io.NopCloser(bytes.NewBufferString("not-valid-json{")),
		}
		ScrubSecretResponse(resp, info, false)
		if resp.StatusCode != http.StatusBadGateway {
			t.Errorf("StatusCode = %d, want %d", resp.StatusCode, http.StatusBadGateway)
		}
	})

	t.Run("scrubs streaming watch events", func(t *testing.T) {
		watchStream := `{"type":"ADDED","object":{"apiVersion":"v1","kind":"Secret","metadata":{"name":"s1"},"data":{"k":"dmFs"}}}` + "\n"
		req := httptest.NewRequest(http.MethodGet, "/api/v1/namespaces/default/secrets?watch=true", nil)
		info, _ := factory.NewRequestInfo(req)
		resp := &http.Response{
			StatusCode: http.StatusOK,
			Header:     make(http.Header),
			Body:       io.NopCloser(strings.NewReader(watchStream)),
		}
		ScrubSecretResponse(resp, info, false)
		out, err := io.ReadAll(resp.Body)
		if err != nil {
			t.Fatalf("unexpected watch read error: %v", err)
		}
		if strings.Contains(string(out), "dmFs") {
			t.Fatalf("watch event leaked secret data: %s", string(out))
		}
	})
}
