// Command ax-sandbox-ssh-proxy listens on loopback SSH (127.0.0.1:2222) inside
// an AX agent task and forwards exec sessions over HTTP to the paired sandbox
// broker on hermes-bridge. Substrate's egress proxy replaces the placeholder
// Authorization header with the caller actor's signed OIDC JWT.
package main

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"regexp"
	"strings"
	"sync"
	"syscall"
	"time"

	"golang.org/x/crypto/ssh"
)

const (
	defaultListenAddr       = "127.0.0.1:2222"
	defaultBrokerToken      = "ax-placeholder"
	defaultBrokerTimeout    = 10 * time.Minute
	defaultEgressTrustCA    = "/run/ate/egress-trust-bundle/trust-bundle.pem"
	maxStdinBytes           = 32 << 20
	maxBrokerBodyBytes      = 64 << 20
	handshakeTimeout        = 15 * time.Second
)

var (
	validEnvNameRe = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]*$`)
	cwdMarkerRe    = regexp.MustCompile(`\\n([A-Za-z0-9_:-]+)%s([A-Za-z0-9_:-]+)\\n`)
)

type proxyConfig struct {
	ListenAddr    string
	BrokerURL     string
	BrokerToken   string
	BrokerTimeout time.Duration
	HTTPClient    *http.Client
	Logger        *slog.Logger
}

type brokerExecRequest struct {
	Command  string            `json:"command"`
	StdinB64 string            `json:"stdin_b64,omitempty"`
	Env      map[string]string `json:"env,omitempty"`
}

type brokerExecResponse struct {
	StdoutB64 string `json:"stdout_b64"`
	StderrB64 string `json:"stderr_b64"`
	Stdout    string `json:"stdout"`
	Stderr    string `json:"stderr"`
	ExitCode  int32  `json:"exit_code"`
}

type sshEnvPayload struct {
	Name  string
	Value string
}

type sshExecPayload struct {
	Command string
}

type sshExitStatusPayload struct {
	Status uint32
}

func main() {
	log := slog.New(slog.NewJSONHandler(os.Stderr, nil))
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()

	cfg := proxyConfig{
		ListenAddr:    envOrDefault("LISTEN_ADDR", defaultListenAddr),
		BrokerURL:     strings.TrimSpace(os.Getenv("SANDBOX_BROKER_URL")),
		BrokerToken:   envOrDefault("SANDBOX_BROKER_TOKEN", defaultBrokerToken),
		BrokerTimeout: envDurationOrDefault("SANDBOX_BROKER_TIMEOUT", defaultBrokerTimeout),
		Logger:        log,
	}
	if cfg.BrokerURL == "" {
		log.Error("SANDBOX_BROKER_URL is required")
		os.Exit(2)
	}

	srv, err := startSSHProxy(ctx, cfg)
	if err != nil {
		log.Error("failed to start ax-sandbox-ssh-proxy", "err", err)
		os.Exit(1)
	}
	log.Info("ax-sandbox-ssh-proxy listening", "addr", srv.Addr(), "broker_url", cfg.BrokerURL)
	srv.Wait()
}

type sshProxyServer struct {
	ln     net.Listener
	sshCfg *ssh.ServerConfig
	client *http.Client
	cfg    proxyConfig
	wg     sync.WaitGroup
	done   chan struct{}
}

func startSSHProxy(ctx context.Context, cfg proxyConfig) (*sshProxyServer, error) {
	if strings.TrimSpace(cfg.ListenAddr) == "" {
		cfg.ListenAddr = defaultListenAddr
	}
	if strings.TrimSpace(cfg.BrokerURL) == "" {
		return nil, errors.New("BrokerURL is required")
	}
	if strings.TrimSpace(cfg.BrokerToken) == "" {
		cfg.BrokerToken = defaultBrokerToken
	}
	if cfg.BrokerTimeout <= 0 {
		cfg.BrokerTimeout = defaultBrokerTimeout
	}
	if cfg.Logger == nil {
		cfg.Logger = slog.Default()
	}

	// Bind the loopback listener first so SSH clients can connect immediately.
	ln, err := net.Listen("tcp", cfg.ListenAddr)
	if err != nil {
		return nil, fmt.Errorf("listen %s: %w", cfg.ListenAddr, err)
	}

	_, privKey, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		_ = ln.Close()
		return nil, fmt.Errorf("generate ephemeral ed25519 host key: %w", err)
	}
	signer, err := ssh.NewSignerFromKey(privKey)
	if err != nil {
		_ = ln.Close()
		return nil, fmt.Errorf("create host key signer: %w", err)
	}

	sshCfg := &ssh.ServerConfig{
		NoClientAuth: true,
	}
	sshCfg.AddHostKey(signer)

	httpClient := cfg.HTTPClient
	if httpClient == nil {
		httpClient = buildBrokerHTTPClient(cfg.BrokerTimeout)
	}

	s := &sshProxyServer{
		ln:     ln,
		sshCfg: sshCfg,
		client: httpClient,
		cfg:    cfg,
		done:   make(chan struct{}),
	}

	go func() {
		<-ctx.Done()
		_ = ln.Close()
	}()

	go s.acceptLoop(ctx)
	return s, nil
}

func (s *sshProxyServer) Addr() string {
	return s.ln.Addr().String()
}

func (s *sshProxyServer) Wait() {
	<-s.done
	s.wg.Wait()
}

func (s *sshProxyServer) acceptLoop(ctx context.Context) {
	defer close(s.done)
	for {
		tcpConn, err := s.ln.Accept()
		if err != nil {
			if ctx.Err() != nil || errors.Is(err, net.ErrClosed) {
				return
			}
			s.cfg.Logger.Warn("accept failed", "err", err)
			continue
		}
		s.wg.Add(1)
		go func(conn net.Conn) {
			defer s.wg.Done()
			s.serveConn(ctx, conn)
		}(tcpConn)
	}
}

func (s *sshProxyServer) serveConn(ctx context.Context, tcpConn net.Conn) {
	defer func() { _ = tcpConn.Close() }()

	_ = tcpConn.SetDeadline(time.Now().Add(handshakeTimeout))
	sshConn, chans, reqs, err := ssh.NewServerConn(tcpConn, s.sshCfg)
	if err != nil {
		return
	}
	_ = tcpConn.SetDeadline(time.Time{})
	defer func() { _ = sshConn.Close() }()

	stopClose := context.AfterFunc(ctx, func() {
		_ = sshConn.Close()
	})
	defer stopClose()

	go handleGlobalRequests(reqs)

	var chanWG sync.WaitGroup
	for newCh := range chans {
		if newCh.ChannelType() != "session" {
			_ = newCh.Reject(ssh.UnknownChannelType, "only session channels are supported")
			continue
		}
		ch, requests, err := newCh.Accept()
		if err != nil {
			continue
		}
		chanWG.Add(1)
		go func(channel ssh.Channel, in <-chan *ssh.Request) {
			defer chanWG.Done()
			s.serveSession(ctx, channel, in)
		}(ch, requests)
	}
	chanWG.Wait()
}

func handleGlobalRequests(reqs <-chan *ssh.Request) {
	for req := range reqs {
		if !req.WantReply {
			continue
		}
		if req.Type == "keepalive@openssh.com" {
			_ = req.Reply(true, nil)
			continue
		}
		_ = req.Reply(false, nil)
	}
}

func (s *sshProxyServer) serveSession(ctx context.Context, ch ssh.Channel, requests <-chan *ssh.Request) {
	var (
		mu          sync.Mutex
		envVars     = make(map[string]string)
		execStarted bool
		cancelExec  context.CancelFunc
		execDone    = make(chan struct{})
	)
	defer func() {
		mu.Lock()
		started := execStarted
		cancel := cancelExec
		mu.Unlock()
		if started {
			<-execDone
		} else {
			if cancel != nil {
				cancel()
			}
			_ = ch.Close()
		}
	}()

	for req := range requests {
		switch req.Type {
		case "env":
			var payload sshEnvPayload
			if err := ssh.Unmarshal(req.Payload, &payload); err != nil {
				if req.WantReply {
					_ = req.Reply(false, nil)
				}
				continue
			}
			mu.Lock()
			if !execStarted && validEnvNameRe.MatchString(payload.Name) {
				envVars[payload.Name] = payload.Value
			}
			mu.Unlock()
			if req.WantReply {
				_ = req.Reply(true, nil)
			}
		case "keepalive@openssh.com":
			if req.WantReply {
				_ = req.Reply(true, nil)
			}
		case "signal":
			mu.Lock()
			cancel := cancelExec
			mu.Unlock()
			if cancel != nil {
				cancel()
			}
			if req.WantReply {
				_ = req.Reply(true, nil)
			}
		case "exec":
			var payload sshExecPayload
			if err := ssh.Unmarshal(req.Payload, &payload); err != nil {
				if req.WantReply {
					_ = req.Reply(false, nil)
				}
				continue
			}
			mu.Lock()
			if execStarted {
				mu.Unlock()
				if req.WantReply {
					_ = req.Reply(false, nil)
				}
				continue
			}
			execStarted = true
			envSnapshot := make(map[string]string, len(envVars))
			for k, v := range envVars {
				envSnapshot[k] = v
			}
			execCtx, cancel := context.WithTimeout(ctx, s.cfg.BrokerTimeout)
			cancelExec = cancel
			mu.Unlock()

			if req.WantReply {
				_ = req.Reply(true, nil)
			}

			go func(command string, envMap map[string]string) {
				defer close(execDone)
				defer cancel()
				s.runExec(execCtx, ch, command, envMap)
			}(payload.Command, envSnapshot)
		default:
			if req.WantReply {
				_ = req.Reply(false, nil)
			}
		}
	}
}

func (s *sshProxyServer) runExec(ctx context.Context, ch ssh.Channel, command string, envVars map[string]string) {
	defer func() { _ = ch.Close() }()

	stdinBytes, err := io.ReadAll(io.LimitReader(ch, maxStdinBytes+1))
	if err != nil {
		writeExecFailure(ch, fmt.Sprintf("ax-sandbox-ssh-proxy: read stdin failed: %v\n", err), 255)
		return
	}
	if len(stdinBytes) > maxStdinBytes {
		writeExecFailure(ch, fmt.Sprintf("ax-sandbox-ssh-proxy: stdin exceeds %d bytes limit\n", maxStdinBytes), 255)
		return
	}

	if localStdout, handled := tryHandleLocalProbe(command, stdinBytes); handled {
		if len(localStdout) > 0 {
			_, _ = ch.Write(localStdout)
		}
		_, _ = ch.SendRequest("exit-status", false, ssh.Marshal(sshExitStatusPayload{Status: 0}))
		_ = ch.CloseWrite()
		return
	}

	reqBody := brokerExecRequest{
		Command: command,
	}
	if len(stdinBytes) > 0 {
		reqBody.StdinB64 = base64.StdEncoding.EncodeToString(stdinBytes)
	}
	if len(envVars) > 0 {
		reqBody.Env = envVars
	}
	encodedReq, err := json.Marshal(reqBody)
	if err != nil {
		writeExecFailure(ch, fmt.Sprintf("ax-sandbox-ssh-proxy: encode request failed: %v\n", err), 255)
		return
	}

	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, s.cfg.BrokerURL, bytes.NewReader(encodedReq))
	if err != nil {
		writeExecFailure(ch, fmt.Sprintf("ax-sandbox-ssh-proxy: build HTTP request failed: %v\n", err), 255)
		return
	}
	httpReq.Header.Set("Content-Type", "application/json")
	httpReq.Header.Set("Authorization", "Bearer "+s.cfg.BrokerToken)

	resp, err := s.client.Do(httpReq) // #nosec G704 -- BrokerURL is configured by the operator
	if err != nil {
		writeExecFailure(ch, fmt.Sprintf("ax-sandbox-ssh-proxy: sandbox broker request failed: %v\n", err), 255)
		return
	}
	defer func() { _ = resp.Body.Close() }()

	rawResp, err := io.ReadAll(io.LimitReader(resp.Body, maxBrokerBodyBytes+1))
	if err != nil {
		writeExecFailure(ch, fmt.Sprintf("ax-sandbox-ssh-proxy: read broker response failed: %v\n", err), 255)
		return
	}
	if len(rawResp) > maxBrokerBodyBytes {
		writeExecFailure(ch, "ax-sandbox-ssh-proxy: broker response exceeded size limit\n", 255)
		return
	}
	if resp.StatusCode != http.StatusOK {
		msg := strings.TrimSpace(string(rawResp))
		if msg == "" {
			msg = http.StatusText(resp.StatusCode)
		}
		writeExecFailure(ch, fmt.Sprintf("ax-sandbox-ssh-proxy: broker HTTP %d: %s\n", resp.StatusCode, msg), 255)
		return
	}

	var parsed brokerExecResponse
	if err := json.Unmarshal(rawResp, &parsed); err != nil {
		writeExecFailure(ch, fmt.Sprintf("ax-sandbox-ssh-proxy: decode broker response failed: %v\n", err), 255)
		return
	}

	stdoutBytes, err := decodeOutputField(parsed.StdoutB64, parsed.Stdout)
	if err != nil {
		writeExecFailure(ch, fmt.Sprintf("ax-sandbox-ssh-proxy: decode stdout failed: %v\n", err), 255)
		return
	}
	stderrBytes, err := decodeOutputField(parsed.StderrB64, parsed.Stderr)
	if err != nil {
		writeExecFailure(ch, fmt.Sprintf("ax-sandbox-ssh-proxy: decode stderr failed: %v\n", err), 255)
		return
	}

	if len(stdoutBytes) > 0 {
		_, _ = ch.Write(stdoutBytes)
	}
	if len(stderrBytes) > 0 {
		_, _ = ch.Stderr().Write(stderrBytes)
	}
	exitCode := parsed.ExitCode
	if exitCode < 0 {
		exitCode = 1
	}
	_, _ = ch.SendRequest("exit-status", false, ssh.Marshal(sshExitStatusPayload{Status: uint32(exitCode)}))
	_ = ch.CloseWrite()
}

// tryHandleLocalProbe answers read-only SSH connection and prompt-builder
// probes locally so pure reasoning turns never wake the paired sandbox Actor.
func tryHandleLocalProbe(command string, stdinBytes []byte) ([]byte, bool) {
	if len(stdinBytes) > 0 {
		return nil, false
	}
	trimmed := strings.TrimSpace(command)
	if trimmed == "echo 'SSH connection established'" {
		return []byte("SSH connection established\n"), true
	}
	const probeSignature = `os=%s\nkernel=%s\nhome=%s\ncwd=%s\nuser=%s\n`
	if strings.Contains(trimmed, probeSignature) && strings.Contains(trimmed, "uname -s") {
		var b strings.Builder
		b.WriteString("os=Linux\nkernel=4.4.0\nhome=/home/agent\ncwd=/workspace\nuser=agent\n")
		if m := cwdMarkerRe.FindStringSubmatch(trimmed); len(m) == 3 && m[1] == m[2] {
			b.WriteString("\n")
			b.WriteString(m[1])
			b.WriteString("/workspace")
			b.WriteString(m[1])
			b.WriteString("\n")
		}
		return []byte(b.String()), true
	}
	return nil, false
}

func decodeOutputField(b64Val, plainVal string) ([]byte, error) {
	if b64Val != "" {
		return base64.StdEncoding.DecodeString(b64Val)
	}
	if plainVal != "" {
		return []byte(plainVal), nil
	}
	return nil, nil
}

func writeExecFailure(ch ssh.Channel, message string, code uint32) {
	_, _ = ch.Stderr().Write([]byte(message))
	_, _ = ch.SendRequest("exit-status", false, ssh.Marshal(sshExitStatusPayload{Status: code}))
	_ = ch.CloseWrite()
}

func buildBrokerHTTPClient(timeout time.Duration) *http.Client {
	pool, err := x509.SystemCertPool()
	if err != nil || pool == nil {
		pool = x509.NewCertPool()
	}
	for _, caPath := range []string{os.Getenv("SSL_CERT_FILE"), defaultEgressTrustCA} {
		trimmed := strings.TrimSpace(caPath)
		if trimmed == "" {
			continue
		}
		if pemBytes, readErr := os.ReadFile(trimmed); readErr == nil { // #nosec G304 -- operator/runtime CA path
			pool.AppendCertsFromPEM(pemBytes)
		}
	}
	base, ok := http.DefaultTransport.(*http.Transport)
	if !ok {
		return &http.Client{Timeout: timeout}
	}
	tr := base.Clone()
	tr.TLSClientConfig = &tls.Config{
		MinVersion: tls.VersionTLS12,
		RootCAs:    pool,
	}
	return &http.Client{Timeout: timeout, Transport: tr}
}

func envOrDefault(key, fallback string) string {
	if v := strings.TrimSpace(os.Getenv(key)); v != "" {
		return v
	}
	return fallback
}

func envDurationOrDefault(key string, fallback time.Duration) time.Duration {
	v := strings.TrimSpace(os.Getenv(key))
	if v == "" {
		return fallback
	}
	d, err := time.ParseDuration(v)
	if err != nil || d <= 0 {
		return fallback
	}
	return d
}
