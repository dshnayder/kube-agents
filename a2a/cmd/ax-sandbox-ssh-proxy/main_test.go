package main

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"golang.org/x/crypto/ssh"
)

type recordedBrokerRequest struct {
	AuthHeader string
	Body       brokerExecRequest
}

func TestSSHProxyForwardsExecStdinStderrEnvAndExitCode(t *testing.T) {
	var (
		mu       sync.Mutex
		requests []recordedBrokerRequest
	)

	broker := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}
		var req brokerExecRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		mu.Lock()
		requests = append(requests, recordedBrokerRequest{
			AuthHeader: r.Header.Get("Authorization"),
			Body:       req,
		})
		mu.Unlock()

		var stdinRaw []byte
		if req.StdinB64 != "" {
			var err error
			stdinRaw, err = base64.StdEncoding.DecodeString(req.StdinB64)
			if err != nil {
				http.Error(w, err.Error(), http.StatusBadRequest)
				return
			}
		}

		var resp brokerExecResponse
		switch req.Command {
		case "binary-roundtrip":
			out := append([]byte{0x00, 0xff, 0xfe}, stdinRaw...)
			resp = brokerExecResponse{
				StdoutB64: base64.StdEncoding.EncodeToString(out),
				StderrB64: base64.StdEncoding.EncodeToString([]byte("warn-line\n")),
				ExitCode:  0,
			}
		case "check-env-and-fail":
			resp = brokerExecResponse{
				StdoutB64: base64.StdEncoding.EncodeToString([]byte("FOO=" + req.Env["FOO"] + "\n")),
				StderrB64: base64.StdEncoding.EncodeToString([]byte("policy denied\n")),
				ExitCode:  77,
			}
		default:
			resp = brokerExecResponse{
				StdoutB64: base64.StdEncoding.EncodeToString([]byte("ok:" + req.Command + "\n")),
				ExitCode:  0,
			}
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(resp)
	}))
	t.Cleanup(broker.Close)

	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)

	srv, err := startSSHProxy(ctx, proxyConfig{
		ListenAddr:  "127.0.0.1:0",
		BrokerURL:   broker.URL + "/v1/sandbox/exec",
		BrokerToken: "ax-placeholder",
	})
	if err != nil {
		t.Fatalf("startSSHProxy: %v", err)
	}
	t.Cleanup(func() {
		cancel()
		srv.Wait()
	})

	clientCfg := &ssh.ClientConfig{
		User:            "agent",
		HostKeyCallback: ssh.InsecureIgnoreHostKey(), // #nosec G106 -- loopback unit test
		Timeout:         5 * time.Second,
	}
	sshClient, err := ssh.Dial("tcp", srv.Addr(), clientCfg)
	if err != nil {
		t.Fatalf("ssh.Dial: %v", err)
	}
	t.Cleanup(func() { _ = sshClient.Close() })

	// 1. Binary stdin + binary stdout + stderr over first session channel.
	sess1, err := sshClient.NewSession()
	if err != nil {
		t.Fatalf("NewSession 1: %v", err)
	}
	binaryStdin := []byte{0x1f, 0x8b, 0x08, 0x00, 0xde, 0xad, 0xbe, 0xef}
	sess1.Stdin = bytes.NewReader(binaryStdin)
	var stdout1, stderr1 bytes.Buffer
	sess1.Stdout = &stdout1
	sess1.Stderr = &stderr1
	if err := sess1.Run("binary-roundtrip"); err != nil {
		t.Fatalf("sess1.Run(binary-roundtrip): %v", err)
	}
	_ = sess1.Close()

	wantOut1 := append([]byte{0x00, 0xff, 0xfe}, binaryStdin...)
	if !bytes.Equal(stdout1.Bytes(), wantOut1) {
		t.Fatalf("stdout1 = %x, want %x", stdout1.Bytes(), wantOut1)
	}
	if stderr1.String() != "warn-line\n" {
		t.Fatalf("stderr1 = %q, want %q", stderr1.String(), "warn-line\n")
	}

	// 2. Multiplexed second session channel on same connection with Setenv + non-zero exit status.
	sess2, err := sshClient.NewSession()
	if err != nil {
		t.Fatalf("NewSession 2: %v", err)
	}
	if err := sess2.Setenv("FOO", "bar-value"); err != nil {
		t.Fatalf("Setenv: %v", err)
	}
	var stdout2, stderr2 bytes.Buffer
	sess2.Stdout = &stdout2
	sess2.Stderr = &stderr2
	runErr := sess2.Run("check-env-and-fail")
	_ = sess2.Close()

	var exitErr *ssh.ExitError
	if !errors.As(runErr, &exitErr) || exitErr.ExitStatus() != 77 {
		t.Fatalf("sess2.Run error = %v, want ExitStatus 77", runErr)
	}
	if stdout2.String() != "FOO=bar-value\n" {
		t.Fatalf("stdout2 = %q, want %q", stdout2.String(), "FOO=bar-value\n")
	}
	if stderr2.String() != "policy denied\n" {
		t.Fatalf("stderr2 = %q, want %q", stderr2.String(), "policy denied\n")
	}

	// 3. If OpenSSH client binary is present, verify ControlMaster multiplexing and keepalives end-to-end.
	if sshBin, lookErr := exec.LookPath("ssh"); lookErr == nil {
		_, port, splitErr := net.SplitHostPort(srv.Addr())
		if splitErr != nil {
			t.Fatalf("SplitHostPort: %v", splitErr)
		}
		sockPath := filepath.Join(t.TempDir(), "cm.sock")
		baseArgs := []string{
			"-p", port,
			"-o", "ControlMaster=auto",
			"-o", "ControlPath=" + sockPath,
			"-o", "ControlPersist=5",
			"-o", "BatchMode=yes",
			"-o", "StrictHostKeyChecking=no",
			"-o", "UserKnownHostsFile=/dev/null",
			"-o", "LogLevel=ERROR",
			"agent@127.0.0.1",
		}
		t.Cleanup(func() {
			_ = exec.Command(sshBin, "-o", "ControlPath="+sockPath, "-O", "exit", "agent@127.0.0.1").Run()
		})

		for _, cmdStr := range []string{"echo first", "kubectl get nodes"} {
			cmd := exec.Command(sshBin, append(baseArgs, cmdStr)...)
			cmd.Stdin = nil // Go sets /dev/null when Stdin is nil
			cmd.Env = os.Environ()
			out, cmdErr := cmd.CombinedOutput()
			if cmdErr != nil {
				t.Fatalf("OpenSSH ControlMaster command %q failed: %v (output=%q)", cmdStr, cmdErr, string(out))
			}
			want := "ok:" + cmdStr + "\n"
			if string(out) != want {
				t.Fatalf("OpenSSH output = %q, want %q", string(out), want)
			}
		}
	}

	mu.Lock()
	defer mu.Unlock()
	if len(requests) < 2 {
		t.Fatalf("recorded requests = %d, want >= 2", len(requests))
	}
	for i, r := range requests {
		if r.AuthHeader != "Bearer ax-placeholder" {
			t.Errorf("request[%d].AuthHeader = %q, want Bearer ax-placeholder", i, r.AuthHeader)
		}
	}
}

func TestSSHProxyAnswersReadOnlyProbesWithoutCallingBroker(t *testing.T) {
	brokerHits := 0
	broker := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		brokerHits++
		http.Error(w, "unexpected broker call", http.StatusInternalServerError)
	}))
	t.Cleanup(broker.Close)

	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)

	srv, err := startSSHProxy(ctx, proxyConfig{
		ListenAddr:  "127.0.0.1:0",
		BrokerURL:   broker.URL + "/v1/sandbox/exec",
		BrokerToken: "ax-placeholder",
	})
	if err != nil {
		t.Fatalf("startSSHProxy: %v", err)
	}
	t.Cleanup(func() {
		cancel()
		srv.Wait()
	})

	clientCfg := &ssh.ClientConfig{
		User:            "agent",
		HostKeyCallback: ssh.InsecureIgnoreHostKey(), // #nosec G106 -- loopback unit test
		Timeout:         5 * time.Second,
	}
	sshClient, err := ssh.Dial("tcp", srv.Addr(), clientCfg)
	if err != nil {
		t.Fatalf("ssh.Dial: %v", err)
	}
	t.Cleanup(func() { _ = sshClient.Close() })

	sess1, err := sshClient.NewSession()
	if err != nil {
		t.Fatalf("NewSession 1: %v", err)
	}
	out1, err := sess1.CombinedOutput("echo 'SSH connection established'")
	_ = sess1.Close()
	if err != nil || string(out1) != "SSH connection established\n" {
		t.Fatalf("probe 1 = (%q, %v)", string(out1), err)
	}

	wrappedProbe := `bash -l -c 'builtin cd -- /workspace || exit 126
eval '\''printf '\''\'\'''\''os=%s\nkernel=%s\nhome=%s\ncwd=%s\nuser=%s\n'\''\'\'''\'' "$(uname -s 2>/dev/null || echo unknown)" "$(uname -r 2>/dev/null || echo unknown)" "$HOME" "$(pwd)" "$(whoami 2>/dev/null || id -un 2>/dev/null || echo unknown)"'\''
__hermes_ec=$?
printf '\''\n__HERMES_CWD_deadbeef__%s__HERMES_CWD_deadbeef__\n'\'' "$(pwd -P)"
exit $__hermes_ec'`
	sess2, err := sshClient.NewSession()
	if err != nil {
		t.Fatalf("NewSession 2: %v", err)
	}
	out2, err := sess2.CombinedOutput(wrappedProbe)
	_ = sess2.Close()
	if err != nil {
		t.Fatalf("probe 2 err: %v", err)
	}
	want2 := "os=Linux\nkernel=4.4.0\nhome=/home/agent\ncwd=/workspace\nuser=agent\n\n__HERMES_CWD_deadbeef__/workspace__HERMES_CWD_deadbeef__\n"
	if string(out2) != want2 {
		t.Fatalf("probe 2 = %q, want %q", string(out2), want2)
	}
	if brokerHits != 0 {
		t.Fatalf("brokerHits = %d, want 0", brokerHits)
	}
}
