// Copyright 2026 The Kubernetes Authors.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package main

import (
	"bytes"
	"compress/gzip"
	"errors"
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync"
	"testing"
	"time"
)

const publicHost = "ghes.example.internal"

// seen is one request as an upstream got it.
type seen struct {
	method, host, path, rawPath, query, auth, acceptEncoding string
	body                                                     []byte
}

type upstream struct {
	srv     *httptest.Server
	mu      sync.Mutex
	got     []seen
	handler http.HandlerFunc
}

func newUpstream(t *testing.T) *upstream {
	t.Helper()
	u := &upstream{}
	u.srv = httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body []byte
		if u.handler == nil {
			body, _ = io.ReadAll(r.Body)
		}
		u.mu.Lock()
		u.got = append(u.got, seen{
			method: r.Method, host: r.Host, path: r.URL.Path, rawPath: r.URL.RawPath, query: r.URL.RawQuery,
			auth: r.Header.Get("Authorization"), acceptEncoding: r.Header.Get("Accept-Encoding"), body: body,
		})
		u.mu.Unlock()
		if u.handler != nil {
			u.handler(w, r)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"ok":true}`)
	}))
	t.Cleanup(u.srv.Close)
	return u
}

func (u *upstream) host() string {
	parsed, _ := url.Parse(u.srv.URL)
	return parsed.Host
}

func (u *upstream) requests() []seen {
	u.mu.Lock()
	defer u.mu.Unlock()
	return append([]seen(nil), u.got...)
}

type fixture struct {
	api, web *upstream
	front    *httptest.Server
	logs     *syncBuffer
}

type syncBuffer struct {
	mu sync.Mutex
	b  bytes.Buffer
}

func (s *syncBuffer) Write(p []byte) (int, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.b.Write(p)
}

func (s *syncBuffer) String() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.b.String()
}

func newFixture(t *testing.T, mutate ...func(*Config)) *fixture {
	t.Helper()
	f := &fixture{api: newUpstream(t), web: newUpstream(t), logs: &syncBuffer{}}
	cfg := Config{PublicHost: publicHost, UpstreamAPIHost: f.api.host(), UpstreamWebHost: f.web.host()}
	for _, m := range mutate {
		m(&cfg)
	}
	// The two test servers share httptest's certificate, so one client trusts both.
	transport := f.api.srv.Client().Transport.(*http.Transport).Clone()
	f.front = httptest.NewServer(NewHandler(cfg, transport, log.New(f.logs, "", 0)))
	t.Cleanup(f.front.Close)
	return f
}

func (f *fixture) do(t *testing.T, method, path string, body io.Reader, header http.Header) *http.Response {
	t.Helper()
	req, err := http.NewRequest(method, f.front.URL+path, body)
	if err != nil {
		t.Fatal(err)
	}
	for k, vs := range header {
		for _, v := range vs {
			req.Header.Add(k, v)
		}
	}
	resp, err := f.front.Client().Do(req)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = resp.Body.Close() })
	return resp
}

func readAll(t *testing.T, r io.Reader) []byte {
	t.Helper()
	b, err := io.ReadAll(r)
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func TestPathMapping(t *testing.T) {
	cases := []struct {
		name, method, path string
		toWeb              bool
		wantPath, wantRaw  string
		wantQuery          string
	}{
		{"rest", "GET", "/api/v3/repos/acme/infra/issues/7?per_page=100&page=2", false, "/repos/acme/infra/issues/7", "", "per_page=100&page=2"},
		{"rest root", "GET", "/api/v3", false, "/", "", ""},
		{"rest meta", "GET", "/api/v3/meta", false, "/meta", "", ""},
		{"rest escaped segment", "GET", "/api/v3/repos/acme/infra/git/ref/heads%2Ffix", false, "/repos/acme/infra/git/ref/heads/fix", "/repos/acme/infra/git/ref/heads%2Ffix", ""},
		{"app token mint", "POST", "/api/v3/app/installations/42/access_tokens", false, "/app/installations/42/access_tokens", "", ""},
		{"graphql", "POST", "/api/graphql", false, "/graphql", "", ""},
		{"info refs upload", "GET", "/acme/infra.git/info/refs?service=git-upload-pack", true, "/acme/infra.git/info/refs", "", "service=git-upload-pack"},
		{"info refs receive", "GET", "/acme/infra.git/info/refs?service=git-receive-pack", true, "/acme/infra.git/info/refs", "", "service=git-receive-pack"},
		{"upload pack", "POST", "/acme/infra.git/git-upload-pack", true, "/acme/infra.git/git-upload-pack", "", ""},
		{"receive pack", "POST", "/acme/infra.git/git-receive-pack", true, "/acme/infra.git/git-receive-pack", "", ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			f := newFixture(t)
			resp := f.do(t, tc.method, tc.path, strings.NewReader("x"), nil)
			if resp.StatusCode != http.StatusOK {
				t.Fatalf("status %d", resp.StatusCode)
			}
			hit, idle := f.api, f.web
			if tc.toWeb {
				hit, idle = f.web, f.api
			}
			got := hit.requests()
			if len(got) != 1 || len(idle.requests()) != 0 {
				t.Fatalf("api got %d, web got %d", len(f.api.requests()), len(f.web.requests()))
			}
			r := got[0]
			if r.method != tc.method || r.path != tc.wantPath || r.rawPath != tc.wantRaw || r.query != tc.wantQuery {
				t.Errorf("upstream got %s %q (raw %q) ?%q", r.method, r.path, r.rawPath, r.query)
			}
			if r.host != hit.host() {
				t.Errorf("upstream Host %q, want %q", r.host, hit.host())
			}
			if v := resp.Header.Get(enterpriseVersionHeader); v != DefaultEnterpriseVersion {
				t.Errorf("%s = %q", enterpriseVersionHeader, v)
			}
		})
	}
}

func TestOtherPathsAreRefused(t *testing.T) {
	cases := []struct{ method, path string }{
		{"GET", "/"},
		{"GET", "/acme/infra"},
		{"GET", "/acme/infra/pull/3"},
		{"GET", "/acme/infra.git"},
		{"GET", "/acme/infra.git/info/refs"},
		{"GET", "/acme/infra.git/info/refs?service=evil"},
		{"POST", "/acme/infra.git/info/refs?service=git-upload-pack"},
		{"GET", "/acme/infra.git/git-upload-pack"},
		{"GET", "/acme/infra.git/git-receive-pack"},
		{"POST", "/acme/infra.git/objects/info/packs"},
		{"POST", "/../infra.git/git-upload-pack"},
		{"GET", "/acme/sub/infra.git/info/refs?service=git-upload-pack"},
		{"GET", "/api/v4/projects"},
		{"GET", "/api/v3x/repos"},
		{"POST", "/api/graphqlx"},
		{"GET", "/api"},
		{"GET", "/login/oauth/authorize"},
	}
	f := newFixture(t)
	for _, tc := range cases {
		resp := f.do(t, tc.method, tc.path, nil, nil)
		if resp.StatusCode != http.StatusNotFound {
			t.Errorf("%s %s: status %d, want 404", tc.method, tc.path, resp.StatusCode)
		}
	}
	if n := len(f.api.requests()) + len(f.web.requests()); n != 0 {
		t.Fatalf("a refused path reached an upstream %d times", n)
	}
}

func TestAuthorizationIsPassedThroughUnchanged(t *testing.T) {
	f := newFixture(t)
	bearer := "Bearer ghs_exampleinstallationtoken"
	jwt := "Bearer eyJhbGciOiJSUzI1NiJ9.eyJpc3MiOiIxMjMifQ.c2ln"
	basic := "Basic eC1hY2Nlc3MtdG9rZW46Z2hzX2V4YW1wbGU="
	f.do(t, "GET", "/api/v3/repos/acme/infra", nil, http.Header{"Authorization": {bearer}})
	f.do(t, "POST", "/api/v3/app/installations/1/access_tokens", nil, http.Header{"Authorization": {jwt}})
	f.do(t, "POST", "/api/graphql", strings.NewReader(`{"query":"{viewer{login}}"}`), http.Header{"Authorization": {bearer}})
	f.do(t, "POST", "/acme/infra.git/git-receive-pack", strings.NewReader("pack"), http.Header{"Authorization": {basic}})
	api := f.api.requests()
	if len(api) != 3 || api[0].auth != bearer || api[1].auth != jwt || api[2].auth != bearer {
		t.Errorf("api Authorization: %+v", api)
	}
	web := f.web.requests()
	if len(web) != 1 || web[0].auth != basic || string(web[0].body) != "pack" {
		t.Errorf("git Authorization or body: %+v", web)
	}
}

func TestJSONFieldsAreRewrittenAtAnyDepth(t *testing.T) {
	f := newFixture(t)
	api, web := f.api.host(), f.web.host()
	body := `{
  "html_url": "https://` + web + `/acme/infra/pull/3",
  "url": "https://` + api + `/repos/acme/infra/pulls/3",
  "head": {"repo": {"html_url": "https://` + web + `/acme/infra", "clone_url": "https://` + web + `/acme/infra.git"}},
  "labels": [{"name": "a", "html_url": "https://` + web + `/acme/infra/labels/a"}, [{"html_url":"https://` + web + `/x"}]],
  "access_tokens_url": "https://` + api + `/app/installations/42/access_tokens",
  "user": {"html_url": "https:\/\/` + web + `\/octo"},
  "other": {"html_url": "https://gist.github.com/1", "n": 1.50, "html_url_note": "https://` + web + `/kept"}
}`
	want := strings.NewReplacer(
		`"html_url": "https://`+web+`/acme/infra/pull/3"`, `"html_url": "https://`+publicHost+`/acme/infra/pull/3"`,
		`"html_url": "https://`+web+`/acme/infra"`, `"html_url": "https://`+publicHost+`/acme/infra"`,
		`"html_url": "https://`+web+`/acme/infra/labels/a"`, `"html_url": "https://`+publicHost+`/acme/infra/labels/a"`,
		`"html_url":"https://`+web+`/x"`, `"html_url":"https://`+publicHost+`/x"`,
		`"access_tokens_url": "https://`+api+`/app/`, `"access_tokens_url": "https://`+publicHost+`/api/v3/app/`,
		`"html_url": "https:\/\/`+web+`\/octo"`, `"html_url": "https://`+publicHost+`/octo"`,
	).Replace(body)
	f.api.handler = func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		_, _ = io.WriteString(w, body)
	}
	resp := f.do(t, "GET", "/api/v3/repos/acme/infra/pulls/3", nil, nil)
	got := string(readAll(t, resp.Body))
	if got != want {
		t.Fatalf("rewritten body:\n%s\nwant:\n%s", got, want)
	}
	if resp.ContentLength != int64(len(want)) {
		t.Errorf("Content-Length %d, want %d", resp.ContentLength, len(want))
	}
}

func TestBodiesAndDiffsAreLeftUnchanged(t *testing.T) {
	f := newFixture(t)
	web := f.web.host()
	// A comment body that quotes JSON with an html_url in it, and a body
	// with a github.com link: text the model reads, so it is not touched.
	jsonBody := `[{"body":"see https://` + web + `/acme/infra/pull/1 and {\"html_url\": \"https://` + web + `/a\"} <b>&</b>","html_url":"https://example.com/x","title":"\u003cfix\u003e"}]`
	diff := "diff --git a/README.md b/README.md\n+see https://" + web + "/acme/infra\n"
	f.api.handler = func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, ".diff") {
			w.Header().Set("Content-Type", "application/vnd.github.diff; charset=utf-8")
			_, _ = io.WriteString(w, diff)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, jsonBody)
	}
	if got := string(readAll(t, f.do(t, "GET", "/api/v3/repos/acme/infra/issues/1/comments", nil, nil).Body)); got != jsonBody {
		t.Errorf("JSON body changed:\n%s\nwant:\n%s", got, jsonBody)
	}
	if got := string(readAll(t, f.do(t, "GET", "/api/v3/repos/acme/infra/pulls/1.diff", nil, nil).Body)); got != diff {
		t.Errorf("diff changed:\n%s", got)
	}
	// Malformed JSON goes through as it is.
	f.api.handler = func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"html_url":"https://`+web+`/a"`)
	}
	if got := string(readAll(t, f.do(t, "GET", "/api/v3/x", nil, nil).Body)); got != `{"html_url":"https://`+web+`/a"` {
		t.Errorf("malformed JSON changed: %s", got)
	}
	// git responses are not JSON-rewritten even if they claim to be JSON.
	f.web.handler = func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"html_url":"https://`+web+`/a"}`)
	}
	if got := string(readAll(t, f.do(t, "POST", "/acme/infra.git/git-upload-pack", nil, nil).Body)); got != `{"html_url":"https://`+web+`/a"}` {
		t.Errorf("git body changed: %s", got)
	}
}

func TestAHeadReplyKeepsItsContentLength(t *testing.T) {
	f := newFixture(t)
	f.api.handler = func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("Content-Length", "1234")
	}
	resp := f.do(t, "HEAD", "/api/v3/repos/acme/infra", nil, nil)
	if resp.StatusCode != http.StatusOK || resp.ContentLength != 1234 {
		t.Errorf("HEAD: status %d, Content-Length %d, want 200 and 1234", resp.StatusCode, resp.ContentLength)
	}
}

func TestGzipJSONIsDecodedAndRewritten(t *testing.T) {
	f := newFixture(t)
	web := f.web.host()
	f.api.handler = func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		if !strings.Contains(r.Header.Get("Accept-Encoding"), "gzip") {
			_, _ = io.WriteString(w, `{"html_url":"https://`+web+`/a"}`)
			return
		}
		w.Header().Set("Content-Encoding", "gzip")
		gz := gzip.NewWriter(w)
		_, _ = io.WriteString(gz, `{"html_url":"https://`+web+`/a"}`)
		_ = gz.Close()
	}
	resp := f.do(t, "GET", "/api/v3/repos/acme/infra", nil, http.Header{"Accept-Encoding": {"gzip"}})
	body := resp.Body
	if resp.Header.Get("Content-Encoding") == "gzip" {
		gz, err := gzip.NewReader(resp.Body)
		if err != nil {
			t.Fatal(err)
		}
		body = gz
	}
	if got := string(readAll(t, body)); got != `{"html_url":"https://`+publicHost+`/a"}` {
		t.Errorf("body %s", got)
	}
}

func TestLocationAndLinkAreRewritten(t *testing.T) {
	f := newFixture(t)
	api, web := f.api.host(), f.web.host()
	f.api.handler = func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Location", "https://"+api+"/repositories/9/issues/1")
		w.Header().Set("Link", `<https://`+api+`/repositories/9/issues?page=2>; rel="next", <https://`+api+`/repositories/9/issues?page=5>; rel="last"`)
		w.WriteHeader(http.StatusMovedPermanently)
	}
	f.web.handler = func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Location", "https://"+web+"/acme/renamed.git/info/refs?service=git-upload-pack")
		w.WriteHeader(http.StatusMovedPermanently)
	}
	client := &http.Client{CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	resp, err := client.Get(f.front.URL + "/api/v3/repos/acme/infra/issues/1")
	if err != nil {
		t.Fatal(err)
	}
	_ = resp.Body.Close()
	if got := resp.Header.Get("Location"); got != "https://"+publicHost+"/api/v3/repositories/9/issues/1" {
		t.Errorf("API Location %q", got)
	}
	wantLink := `<https://` + publicHost + `/api/v3/repositories/9/issues?page=2>; rel="next", <https://` + publicHost + `/api/v3/repositories/9/issues?page=5>; rel="last"`
	if got := resp.Header.Get("Link"); got != wantLink {
		t.Errorf("Link %q", got)
	}
	resp, err = client.Get(f.front.URL + "/acme/infra.git/info/refs?service=git-upload-pack")
	if err != nil {
		t.Fatal(err)
	}
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusMovedPermanently {
		t.Errorf("redirect followed: status %d", resp.StatusCode)
	}
	if got := resp.Header.Get("Location"); got != "https://"+publicHost+"/acme/renamed.git/info/refs?service=git-upload-pack" {
		t.Errorf("git Location %q", got)
	}
}

func TestRewriteURL(t *testing.T) {
	p := &proxy{cfg: Config{PublicHost: publicHost, UpstreamAPIHost: "api.github.com", UpstreamWebHost: "github.com"}}
	for in, want := range map[string]string{
		"https://api.github.com/app/installations/1/access_tokens": "https://" + publicHost + "/api/v3/app/installations/1/access_tokens",
		"https://api.github.com":                                   "https://" + publicHost + "/api/v3",
		"https://api.github.com/graphql":                           "https://" + publicHost + "/api/graphql",
		"https://github.com/acme/infra/issues/4":                   "https://" + publicHost + "/acme/infra/issues/4",
		"https://github.com":                                       "https://" + publicHost,
		"https://gist.github.com/1":                                "https://gist.github.com/1",
		"https://github.com.evil.example/x":                        "https://github.com.evil.example/x",
		"http://github.com/x":                                      "http://github.com/x",
		"https://uploads.github.com/x":                             "https://uploads.github.com/x",
	} {
		if got := p.rewriteURL(in); got != want {
			t.Errorf("rewriteURL(%q) = %q, want %q", in, got, want)
		}
	}
}

// TestGitStreamsBothWays proves neither direction is held whole: the
// upstream sends the first part of a response and waits until the client has
// read it, and the client sends the first part of a request body and waits
// until the upstream has read it.
func TestGitStreamsBothWays(t *testing.T) {
	f := newFixture(t)
	chunk := bytes.Repeat([]byte("p"), 1<<20)
	clientRead := make(chan struct{})
	upstreamRead := make(chan struct{})
	f.web.handler = func(w http.ResponseWriter, r *http.Request) {
		first := make([]byte, len(chunk))
		if _, err := io.ReadFull(r.Body, first); err != nil {
			t.Errorf("upstream read: %v", err)
			return
		}
		close(upstreamRead)
		rest := readAll(t, r.Body)
		w.Header().Set("Content-Type", "application/x-git-upload-pack-result")
		_, _ = w.Write(chunk)
		w.(http.Flusher).Flush()
		select {
		case <-clientRead:
		case <-time.After(10 * time.Second):
			t.Errorf("the client never got the first chunk: the response was buffered")
			return
		}
		_, _ = w.Write(rest)
	}
	pr, pw := io.Pipe()
	go func() {
		_, _ = pw.Write(chunk)
		select {
		case <-upstreamRead:
		case <-time.After(10 * time.Second):
			_ = pw.CloseWithError(errors.New("the upstream never got the first chunk: the request was buffered"))
			return
		}
		_, _ = pw.Write([]byte("tail"))
		_ = pw.Close()
	}()
	resp := f.do(t, "POST", "/acme/infra.git/git-upload-pack", pr, nil)
	first := make([]byte, len(chunk))
	if _, err := io.ReadFull(resp.Body, first); err != nil {
		t.Fatalf("client read: %v", err)
	}
	close(clientRead)
	if rest := string(readAll(t, resp.Body)); rest != "tail" {
		t.Fatalf("rest %q", rest)
	}
}

func TestAJSONBodyOverTheCapIsSentAsItIs(t *testing.T) {
	f := newFixture(t, func(c *Config) { c.MaxRewriteBytes = 16 })
	web := f.web.host()
	body := `{"html_url":"https://` + web + `/acme/infra/pull/3","pad":"` + strings.Repeat("x", 100) + `"}`
	f.api.handler = func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, body)
	}
	if got := string(readAll(t, f.do(t, "GET", "/api/v3/repos/acme/infra/pulls/3", nil, nil).Body)); got != body {
		t.Errorf("body over the cap changed: %s", got)
	}
}

func TestNoCredentialReachesTheLog(t *testing.T) {
	secrets := []string{"ghs_SECRETinstallation", "eyJSECRETjwt", "x-access-token:ghs_SECRETgit", "SECRETquery", "SECRETbody", "ghs_SECRETminted"}
	f := newFixture(t)
	f.api.handler = func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("Location", "https://example.com/?token=SECRETquery")
		_, _ = io.WriteString(w, `{"token":"ghs_SECRETminted","html_url":"https://`+f.web.host()+`/a"}`)
	}
	f.do(t, "GET", "/api/v3/user?access_token=SECRETquery", nil, http.Header{"Authorization": {"Bearer ghs_SECRETinstallation"}})
	f.do(t, "POST", "/api/v3/app/installations/1/access_tokens", strings.NewReader("SECRETbody"), http.Header{"Authorization": {"Bearer eyJSECRETjwt"}})
	f.do(t, "POST", "/acme/infra.git/git-receive-pack", strings.NewReader("SECRETbody"), http.Header{"Authorization": {"Basic x-access-token:ghs_SECRETgit"}})
	f.do(t, "GET", "/nothing?token=SECRETquery", nil, http.Header{"Authorization": {"Bearer ghs_SECRETinstallation"}})

	// An upstream that cannot be reached: the transport's error names the
	// URL, query string included.
	broken := newFixture(t, func(c *Config) { c.UpstreamAPIHost = "127.0.0.1:1" })
	resp := broken.do(t, "GET", "/api/v3/user?access_token=SECRETquery", nil, http.Header{"Authorization": {"Bearer ghs_SECRETinstallation"}})
	if resp.StatusCode != http.StatusBadGateway {
		t.Errorf("unreachable upstream: status %d", resp.StatusCode)
	}

	// A transport that wraps its error in a *url.Error, as http.Client does.
	wrapped := &syncBuffer{}
	h := NewHandler(Config{PublicHost: publicHost, UpstreamAPIHost: "api.github.com", UpstreamWebHost: "github.com"},
		urlErrorTransport{}, log.New(wrapped, "", 0))
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest("GET", "/api/v3/user?access_token=SECRETquery", nil))
	if rec.Code != http.StatusBadGateway {
		t.Errorf("wrapped error: status %d", rec.Code)
	}

	logs := f.logs.String() + broken.logs.String() + wrapped.String()
	if !strings.Contains(logs, "/api/v3/user") || !strings.Contains(logs, "upstream error") {
		t.Fatalf("the log lacks the expected lines:\n%s", logs)
	}
	for _, s := range secrets {
		if strings.Contains(logs, s) {
			t.Errorf("the log holds %q:\n%s", s, logs)
		}
	}
}

type urlErrorTransport struct{}

func (urlErrorTransport) RoundTrip(r *http.Request) (*http.Response, error) {
	return nil, &url.Error{Op: "Get", URL: r.URL.String(), Err: errors.New("connection reset")}
}

func TestParseOptions(t *testing.T) {
	env := map[string]string{"GHES_PROXY_PUBLIC_HOST": "ghes.example.internal", "GHES_PROXY_ENTERPRISE_VERSION": "3.16.2"}
	o, err := parseOptions([]string{"-listen", ":9443"}, func(k string) string { return env[k] }, io.Discard)
	if err != nil {
		t.Fatal(err)
	}
	if o.listen != ":9443" || o.cfg.PublicHost != "ghes.example.internal" || o.cfg.EnterpriseVersion != "3.16.2" ||
		o.cfg.UpstreamAPIHost != "api.github.com" || o.cfg.UpstreamWebHost != "github.com" ||
		o.certFile != "/etc/ghes-proxy/tls/tls.crt" || o.cfg.MaxRewriteBytes != DefaultMaxRewriteBytes {
		t.Errorf("options %+v", o)
	}
	for _, args := range [][]string{
		{},
		{"-public-host", "https://ghes.example.internal"},
		{"-public-host", "ghes.example.internal/x"},
		{"-public-host", "ghes.example.internal", "-upstream-api-host", "api.github.com/v3"},
		{"-public-host", "ghes.example.internal", "-max-rewrite-bytes", "0"},
	} {
		if _, err := parseOptions(args, func(string) string { return "" }, io.Discard); err == nil {
			t.Errorf("parseOptions(%q) accepted", args)
		}
	}
}
