package hermesbridge

import (
	"bytes"
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"io"
	"math/big"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	ateenvv1alpha "github.com/agent-substrate/env/proto/ateenv/v1alpha"
	axv1alpha1 "github.com/google/ax/pkg/apis/v1alpha1"
	ateapipb "go.ate.dev/substrate/api/ateapi/v1"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/peer"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"

	"github.com/gke-labs/kube-agents/a2a/capability"
	"github.com/gke-labs/kube-agents/a2a/lib"
)

type fakeAXServer struct {
	axv1alpha1.UnimplementedAXServer

	mu              sync.Mutex
	tasks           map[string]*axv1alpha1.Task
	gateways        map[string]*axv1alpha1.Gateway
	resumeFailures  int
	resumeCalls     map[string]int
	suspendCalls    map[string]int
	createdTasks    []*axv1alpha1.Task
	createdGateways []*axv1alpha1.Gateway
	updatedGateways []*axv1alpha1.Gateway
	suspendNotified chan string
}

func newFakeAXServer() *fakeAXServer {
	return &fakeAXServer{
		tasks:           make(map[string]*axv1alpha1.Task),
		gateways:        make(map[string]*axv1alpha1.Gateway),
		resumeCalls:     make(map[string]int),
		suspendCalls:    make(map[string]int),
		suspendNotified: make(chan string, 32),
	}
}

func (s *fakeAXServer) putTask(task *axv1alpha1.Task) {
	s.mu.Lock()
	defer s.mu.Unlock()
	key := task.GetMetadata().GetAtespace() + "/" + task.GetMetadata().GetName()
	s.tasks[key] = proto.Clone(task).(*axv1alpha1.Task)
}

func (s *fakeAXServer) putGateway(gw *axv1alpha1.Gateway) {
	s.mu.Lock()
	defer s.mu.Unlock()
	key := gw.GetMetadata().GetAtespace() + "/" + gw.GetMetadata().GetName()
	s.gateways[key] = proto.Clone(gw).(*axv1alpha1.Gateway)
}

func (s *fakeAXServer) GetGateway(_ context.Context, req *axv1alpha1.GetGatewayRequest) (*axv1alpha1.Gateway, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	key := req.GetAtespace() + "/" + req.GetName()
	gw, ok := s.gateways[key]
	if !ok {
		return nil, status.Errorf(codes.NotFound, "gateway %s not found", key)
	}
	return proto.Clone(gw).(*axv1alpha1.Gateway), nil
}

func (s *fakeAXServer) CreateGateway(_ context.Context, req *axv1alpha1.CreateGatewayRequest) (*axv1alpha1.Gateway, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	in := req.GetGateway()
	key := in.GetMetadata().GetAtespace() + "/" + in.GetMetadata().GetName()
	if _, exists := s.gateways[key]; exists {
		return nil, status.Errorf(codes.AlreadyExists, "gateway %s already exists", key)
	}
	stored := proto.Clone(in).(*axv1alpha1.Gateway)
	s.gateways[key] = stored
	s.createdGateways = append(s.createdGateways, proto.Clone(stored).(*axv1alpha1.Gateway))
	return proto.Clone(stored).(*axv1alpha1.Gateway), nil
}

func (s *fakeAXServer) UpdateGateway(_ context.Context, req *axv1alpha1.UpdateGatewayRequest) (*axv1alpha1.Gateway, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	in := req.GetGateway()
	key := in.GetMetadata().GetAtespace() + "/" + in.GetMetadata().GetName()
	if _, exists := s.gateways[key]; !exists {
		return nil, status.Errorf(codes.NotFound, "gateway %s not found", key)
	}
	stored := proto.Clone(in).(*axv1alpha1.Gateway)
	s.gateways[key] = stored
	s.updatedGateways = append(s.updatedGateways, proto.Clone(stored).(*axv1alpha1.Gateway))
	return proto.Clone(stored).(*axv1alpha1.Gateway), nil
}

func (s *fakeAXServer) GetTask(_ context.Context, req *axv1alpha1.GetTaskRequest) (*axv1alpha1.Task, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	key := req.GetAtespace() + "/" + req.GetName()
	t, ok := s.tasks[key]
	if !ok {
		return nil, status.Errorf(codes.NotFound, "task %s not found", key)
	}
	return proto.Clone(t).(*axv1alpha1.Task), nil
}

func (s *fakeAXServer) CreateTask(_ context.Context, req *axv1alpha1.CreateTaskRequest) (*axv1alpha1.Task, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	in := req.GetTask()
	// Enforce stock upstream ax-server invariant: ateTemplate on Profile is Unimplemented.
	if tpl := in.GetSpec().GetProfile().GetAteTemplate().GetName(); tpl != "" {
		return nil, status.Errorf(codes.Unimplemented, "spec.profile.ateTemplate is not supported on stock ax-server")
	}
	key := in.GetMetadata().GetAtespace() + "/" + in.GetMetadata().GetName()
	if _, exists := s.tasks[key]; exists {
		return nil, status.Errorf(codes.AlreadyExists, "task %s already exists", key)
	}
	stored := proto.Clone(in).(*axv1alpha1.Task)
	if stored.Status == nil {
		stored.Status = &axv1alpha1.TaskStatus{}
	}
	stored.Status.State = axv1alpha1.TaskState_TASK_STATE_SUSPENDED
	stored.Status.Actor = stored.GetMetadata().GetName()
	stored.Status.AteTemplate = &axv1alpha1.ObjectRef{
		Atespace: stored.GetMetadata().GetAtespace(),
		Name:     stored.GetMetadata().GetName() + "-tmpl-12345678",
	}
	s.tasks[key] = stored
	s.createdTasks = append(s.createdTasks, proto.Clone(stored).(*axv1alpha1.Task))
	return proto.Clone(stored).(*axv1alpha1.Task), nil
}

func (s *fakeAXServer) ResumeTask(_ context.Context, req *axv1alpha1.ResumeTaskRequest) (*axv1alpha1.Task, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	key := req.GetAtespace() + "/" + req.GetName()
	s.resumeCalls[key]++
	if s.resumeFailures > 0 {
		s.resumeFailures--
		return nil, status.Errorf(codes.ResourceExhausted, "actor capacity temporarily full")
	}
	t, ok := s.tasks[key]
	if !ok {
		return nil, status.Errorf(codes.NotFound, "task %s not found", key)
	}
	t.Status.State = axv1alpha1.TaskState_TASK_STATE_WORKING
	if t.Status.Actor == "" {
		t.Status.Actor = req.GetName()
	}
	t.Status.Conditions = []*axv1alpha1.Condition{
		{Type: axv1alpha1.ConditionWorkspaceReady, Status: "True"},
		{Type: axv1alpha1.ConditionReady, Status: "True"},
	}
	return proto.Clone(t).(*axv1alpha1.Task), nil
}

func (s *fakeAXServer) SuspendTask(_ context.Context, req *axv1alpha1.SuspendTaskRequest) (*axv1alpha1.Task, error) {
	s.mu.Lock()
	key := req.GetAtespace() + "/" + req.GetName()
	t, ok := s.tasks[key]
	if !ok {
		s.mu.Unlock()
		return nil, status.Errorf(codes.NotFound, "task %s not found", key)
	}
	s.suspendCalls[key]++
	t.Status.State = axv1alpha1.TaskState_TASK_STATE_SUSPENDED
	t.Status.Conditions = nil
	cloned := proto.Clone(t).(*axv1alpha1.Task)
	s.mu.Unlock()

	select {
	case s.suspendNotified <- key:
	default:
	}
	return cloned, nil
}

type recordedProcessCall struct {
	processID   string
	targetActor string
	clientCN    string
	command     []string
	cwd         string
	env         map[string]string
	stdinOpen   bool
}

type fakeProcessServer struct {
	ateenvv1alpha.UnimplementedProcessServiceServer

	mu              sync.Mutex
	nextID          int
	calls           []recordedProcessCall
	stdinByProc     map[string][]byte
	stdinClosed     map[string]chan struct{}
	streamRequests  []*ateenvv1alpha.StreamProcessOutputRequest
	dropFirstStream bool
	signals         []ateenvv1alpha.Signal
	omitExit        bool
	holdTurn        chan struct{}
	turnActive      chan struct{}
	signalCh        chan ateenvv1alpha.Signal
	onTurnRun       func(targetActor, prompt string)
}

func newFakeProcessServer() *fakeProcessServer {
	return &fakeProcessServer{
		stdinByProc: make(map[string][]byte),
		stdinClosed: make(map[string]chan struct{}),
		signalCh:    make(chan ateenvv1alpha.Signal, 8),
	}
}

func incomingTargetActor(ctx context.Context) string {
	md, ok := metadata.FromIncomingContext(ctx)
	if !ok {
		return ""
	}
	vals := md.Get(axTargetActorHeader)
	if len(vals) == 0 {
		return ""
	}
	return vals[0]
}

func peerCommonName(ctx context.Context) string {
	p, ok := peer.FromContext(ctx)
	if !ok || p.AuthInfo == nil {
		return ""
	}
	tlsInfo, ok := p.AuthInfo.(credentials.TLSInfo)
	if !ok || len(tlsInfo.State.PeerCertificates) == 0 {
		return ""
	}
	return tlsInfo.State.PeerCertificates[0].Subject.CommonName
}

func (p *fakeProcessServer) StartProcess(ctx context.Context, req *ateenvv1alpha.StartProcessRequest) (*ateenvv1alpha.Process, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.nextID++
	id := fmt.Sprintf("proc-%d", p.nextID)
	envCopy := make(map[string]string, len(req.GetEnv()))
	for k, v := range req.GetEnv() {
		envCopy[k] = v
	}
	if req.GetStdin() {
		p.stdinClosed[id] = make(chan struct{})
	}
	p.calls = append(p.calls, recordedProcessCall{
		processID:   id,
		targetActor: incomingTargetActor(ctx),
		clientCN:    peerCommonName(ctx),
		command:     slices.Clone(req.GetCommand()),
		cwd:         req.GetCwd(),
		env:         envCopy,
		stdinOpen:   req.GetStdin(),
	})
	return &ateenvv1alpha.Process{
		ProcessId: id,
		Command:   slices.Clone(req.GetCommand()),
		Pid:       int32(1000 + p.nextID),
		State:     ateenvv1alpha.ProcessState_PROCESS_STATE_RUNNING,
	}, nil
}

func (p *fakeProcessServer) WriteProcessInput(stream grpc.ClientStreamingServer[ateenvv1alpha.WriteProcessInputRequest, ateenvv1alpha.WriteProcessInputResponse]) error {
	var procID string
	var buf bytes.Buffer
	var sawClose bool
	for {
		msg, err := stream.Recv()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			return err
		}
		if procID == "" {
			procID = msg.GetProcessId()
		}
		if len(msg.GetData()) > 0 {
			_, _ = buf.Write(msg.GetData())
		}
		if msg.GetClose() {
			sawClose = true
		}
	}
	p.mu.Lock()
	p.stdinByProc[procID] = buf.Bytes()
	ch := p.stdinClosed[procID]
	p.mu.Unlock()
	if sawClose && ch != nil {
		close(ch)
	}
	return stream.SendAndClose(&ateenvv1alpha.WriteProcessInputResponse{})
}

func (p *fakeProcessServer) StreamProcessOutput(req *ateenvv1alpha.StreamProcessOutputRequest, stream grpc.ServerStreamingServer[ateenvv1alpha.ProcessOutput]) error {
	p.mu.Lock()
	var matchedCall recordedProcessCall
	for _, c := range p.calls {
		if c.processID == req.GetProcessId() {
			matchedCall = c
			break
		}
	}
	stdinDone := p.stdinClosed[req.GetProcessId()]
	hold := p.holdTurn
	active := p.turnActive
	omitExit := p.omitExit
	onTurnRun := p.onTurnRun
	dropNow := false
	p.streamRequests = append(p.streamRequests, proto.Clone(req).(*ateenvv1alpha.StreamProcessOutputRequest))
	if p.dropFirstStream {
		p.dropFirstStream = false
		dropNow = true
	}
	p.mu.Unlock()

	if stdinDone != nil {
		select {
		case <-stdinDone:
		case <-stream.Context().Done():
			return stream.Context().Err()
		}
	}

	isSandboxExec := len(matchedCall.command) == 3 && matchedCall.command[0] == "/bin/bash" && matchedCall.command[1] == "-c"
	if isSandboxExec {
		p.mu.Lock()
		stdinData := slices.Clone(p.stdinByProc[req.GetProcessId()])
		p.mu.Unlock()

		cmdStr := matchedCall.command[2]
		outBytes := append([]byte("sandbox:"+cmdStr+":"), stdinData...)
		errBytes := []byte("sandbox-err\n")
		var code int32
		if strings.Contains(cmdStr, "exit 42") {
			code = 42
		}
		if err := stream.Send(&ateenvv1alpha.ProcessOutput{
			Output: &ateenvv1alpha.ProcessOutput_Stdout{Stdout: outBytes[req.GetStdoutOffset():]},
		}); err != nil {
			return err
		}
		if err := stream.Send(&ateenvv1alpha.ProcessOutput{
			Output: &ateenvv1alpha.ProcessOutput_Stderr{Stderr: errBytes[req.GetStderrOffset():]},
		}); err != nil {
			return err
		}
		return stream.Send(&ateenvv1alpha.ProcessOutput{
			Output: &ateenvv1alpha.ProcessOutput_Exit{
				Exit: &ateenvv1alpha.Process{
					ProcessId: req.GetProcessId(),
					State:     ateenvv1alpha.ProcessState_PROCESS_STATE_EXITED,
					ExitCode:  code,
				},
			},
		})
	}

	var prompt string
	if len(matchedCall.command) > 0 {
		prompt = matchedCall.command[len(matchedCall.command)-1]
	}
	sessionID := matchedCall.env["HERMES_SESSION_ID"]

	if onTurnRun != nil {
		onTurnRun(matchedCall.targetActor, prompt)
	}
	if active != nil {
		select {
		case active <- struct{}{}:
		default:
		}
	}
	if hold != nil {
		select {
		case <-hold:
		case <-stream.Context().Done():
			return stream.Context().Err()
		}
	}

	outText := fmt.Sprintf("\x1b[32manswer for %s\x1b[0m\nsession_id: %s\n", prompt, sessionID)
	errText := "session_id: " + sessionID + "\n"
	if dropNow {
		half := len(outText) / 2
		if err := stream.Send(&ateenvv1alpha.ProcessOutput{
			Output: &ateenvv1alpha.ProcessOutput_Stdout{Stdout: []byte(outText[:half])},
		}); err != nil {
			return err
		}
		return status.Error(codes.Unavailable, "upstream request timeout")
	}
	if off := int(req.GetStdoutOffset()); off < len(outText) {
		if err := stream.Send(&ateenvv1alpha.ProcessOutput{
			Output: &ateenvv1alpha.ProcessOutput_Stdout{Stdout: []byte(outText[off:])},
		}); err != nil {
			return err
		}
	}
	if off := int(req.GetStderrOffset()); off < len(errText) {
		if err := stream.Send(&ateenvv1alpha.ProcessOutput{
			Output: &ateenvv1alpha.ProcessOutput_Stderr{Stderr: []byte(errText[off:])},
		}); err != nil {
			return err
		}
	}
	if omitExit {
		return nil
	}
	return stream.Send(&ateenvv1alpha.ProcessOutput{
		Output: &ateenvv1alpha.ProcessOutput_Exit{
			Exit: &ateenvv1alpha.Process{
				ProcessId: req.GetProcessId(),
				State:     ateenvv1alpha.ProcessState_PROCESS_STATE_EXITED,
				ExitCode:  0,
			},
		},
	})
}

func (p *fakeProcessServer) GetProcess(_ context.Context, req *ateenvv1alpha.GetProcessRequest) (*ateenvv1alpha.Process, error) {
	return &ateenvv1alpha.Process{
		ProcessId: req.GetProcessId(),
		State:     ateenvv1alpha.ProcessState_PROCESS_STATE_EXITED,
		ExitCode:  0,
	}, nil
}

func (p *fakeProcessServer) SignalProcess(_ context.Context, req *ateenvv1alpha.SignalProcessRequest) (*ateenvv1alpha.Process, error) {
	p.mu.Lock()
	p.signals = append(p.signals, req.GetSignal())
	p.mu.Unlock()
	select {
	case p.signalCh <- req.GetSignal():
	default:
	}
	return &ateenvv1alpha.Process{
		ProcessId: req.GetProcessId(),
		State:     ateenvv1alpha.ProcessState_PROCESS_STATE_EXITED,
		ExitCode:  143,
	}, nil
}

type testMTLSBundle struct {
	caKey      *ecdsa.PrivateKey
	caCert     *x509.Certificate
	caFile     string
	clientFile string
	serverTLS  *tls.Config
}

func newTestMTLSBundle(t *testing.T, serverDNS, initialClientCN string) *testMTLSBundle {
	t.Helper()
	dir := t.TempDir()
	now := time.Now()

	caKey, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatalf("generate CA key: %v", err)
	}
	caTmpl := &x509.Certificate{
		SerialNumber:          big.NewInt(1),
		Subject:               pkix.Name{CommonName: "test-ate-ca"},
		NotBefore:             now.Add(-time.Minute),
		NotAfter:              now.Add(time.Hour),
		KeyUsage:              x509.KeyUsageCertSign | x509.KeyUsageCRLSign,
		BasicConstraintsValid: true,
		IsCA:                  true,
	}
	caDER, err := x509.CreateCertificate(rand.Reader, caTmpl, caTmpl, &caKey.PublicKey, caKey)
	if err != nil {
		t.Fatalf("create CA cert: %v", err)
	}
	caCert, err := x509.ParseCertificate(caDER)
	if err != nil {
		t.Fatalf("parse CA cert: %v", err)
	}
	caPEM := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: caDER})
	caFile := filepath.Join(dir, "trust-bundle.pem")
	if err := os.WriteFile(caFile, caPEM, 0o600); err != nil {
		t.Fatalf("write CA file: %v", err)
	}

	srvCert := issueLeafCert(t, caCert, caKey, "atenet-router", []string{serverDNS}, []net.IP{net.ParseIP("127.0.0.1")}, []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth})
	pool := x509.NewCertPool()
	pool.AddCert(caCert)

	clientFile := filepath.Join(dir, "credential-bundle.pem")
	b := &testMTLSBundle{
		caKey:      caKey,
		caCert:     caCert,
		caFile:     caFile,
		clientFile: clientFile,
		serverTLS: &tls.Config{
			MinVersion:   tls.VersionTLS12,
			Certificates: []tls.Certificate{srvCert},
			ClientAuth:   tls.RequireAndVerifyClientCert,
			ClientCAs:    pool,
		},
	}
	b.writeClientBundle(t, initialClientCN)
	return b
}

func (b *testMTLSBundle) writeClientBundle(t *testing.T, commonName string) {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatalf("generate client key: %v", err)
	}
	now := time.Now()
	tmpl := &x509.Certificate{
		SerialNumber: big.NewInt(time.Now().UnixNano()),
		Subject:      pkix.Name{CommonName: commonName},
		NotBefore:    now.Add(-time.Minute),
		NotAfter:     now.Add(time.Hour),
		KeyUsage:     x509.KeyUsageDigitalSignature,
		ExtKeyUsage:  []x509.ExtKeyUsage{x509.ExtKeyUsageClientAuth},
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, b.caCert, &key.PublicKey, b.caKey)
	if err != nil {
		t.Fatalf("create client cert: %v", err)
	}
	keyDER, err := x509.MarshalECPrivateKey(key)
	if err != nil {
		t.Fatalf("marshal client key: %v", err)
	}
	var combined []byte
	combined = append(combined, pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: keyDER})...)
	combined = append(combined, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})...)
	if err := os.WriteFile(b.clientFile, combined, 0o600); err != nil {
		t.Fatalf("write client bundle: %v", err)
	}
}

func issueLeafCert(t *testing.T, caCert *x509.Certificate, caKey *ecdsa.PrivateKey, cn string, dnsNames []string, ips []net.IP, eku []x509.ExtKeyUsage) tls.Certificate {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatalf("generate leaf key: %v", err)
	}
	now := time.Now()
	tmpl := &x509.Certificate{
		SerialNumber: big.NewInt(time.Now().UnixNano()),
		Subject:      pkix.Name{CommonName: cn},
		DNSNames:     dnsNames,
		IPAddresses:  ips,
		NotBefore:    now.Add(-time.Minute),
		NotAfter:     now.Add(time.Hour),
		KeyUsage:     x509.KeyUsageDigitalSignature,
		ExtKeyUsage:  eku,
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, caCert, &key.PublicKey, caKey)
	if err != nil {
		t.Fatalf("create leaf cert: %v", err)
	}
	keyDER, err := x509.MarshalECPrivateKey(key)
	if err != nil {
		t.Fatalf("marshal leaf key: %v", err)
	}
	certPEM := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})
	keyPEM := pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: keyDER})
	pair, err := tls.X509KeyPair(certPEM, keyPEM)
	if err != nil {
		t.Fatalf("load leaf pair: %v", err)
	}
	return pair
}

func serveGRPC(t *testing.T, srv *grpc.Server, addr string) string {
	t.Helper()
	ln, err := net.Listen("tcp", addr)
	if err != nil {
		t.Fatalf("listen %s: %v", addr, err)
	}
	go func() {
		_ = srv.Serve(ln)
	}()
	t.Cleanup(func() {
		srv.Stop()
		_ = ln.Close()
	})
	return ln.Addr().String()
}

func startTestAXAndRouter(t *testing.T, mtls *testMTLSBundle) (*fakeAXServer, string, *fakeProcessServer, string) {
	t.Helper()
	axSvc := newFakeAXServer()
	axSrv := grpc.NewServer()
	axv1alpha1.RegisterAXServer(axSrv, axSvc)
	axAddr := serveGRPC(t, axSrv, "127.0.0.1:0")

	procSvc := newFakeProcessServer()
	var routerOpts []grpc.ServerOption
	if mtls != nil {
		routerOpts = append(routerOpts, grpc.Creds(credentials.NewTLS(mtls.serverTLS)))
	}
	routerSrv := grpc.NewServer(routerOpts...)
	ateenvv1alpha.RegisterProcessServiceServer(routerSrv, procSvc)
	routerAddr := serveGRPC(t, routerSrv, "127.0.0.1:0")

	return axSvc, axAddr, procSvc, routerAddr
}

type testOIDCProvider struct {
	issuerURL string
	caFile    string
	signKey   *ecdsa.PrivateKey
	kid       string
}

func startTestOIDCProvider(t *testing.T) *testOIDCProvider {
	t.Helper()
	mtls := newTestMTLSBundle(t, "localhost", "unused-client")
	srvLeaf := issueLeafCert(t, mtls.caCert, mtls.caKey, "idp", []string{"localhost"}, []net.IP{net.ParseIP("127.0.0.1")}, []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth})

	signKey, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatalf("generate ES256 signing key: %v", err)
	}
	kid := "idp-es256-1"
	pub := signKey.PublicKey
	coordLen := 32
	xBytes := pub.X.FillBytes(make([]byte, coordLen))
	yBytes := pub.Y.FillBytes(make([]byte, coordLen))

	var srv *httptest.Server
	mux := http.NewServeMux()
	mux.HandleFunc("/.well-known/openid-configuration", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"issuer":   srv.URL,
			"jwks_uri": srv.URL + "/jwks",
		})
	})
	mux.HandleFunc("/jwks", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"keys": []map[string]any{{
				"kty": "EC",
				"use": "sig",
				"alg": "ES256",
				"kid": kid,
				"crv": "P-256",
				"x":   base64.RawURLEncoding.EncodeToString(xBytes),
				"y":   base64.RawURLEncoding.EncodeToString(yBytes),
			}},
		})
	})

	srv = httptest.NewUnstartedServer(mux)
	srv.TLS = &tls.Config{
		MinVersion:   tls.VersionTLS12,
		Certificates: []tls.Certificate{srvLeaf},
	}
	srv.StartTLS()
	t.Cleanup(srv.Close)

	return &testOIDCProvider{
		issuerURL: srv.URL,
		caFile:    mtls.caFile,
		signKey:   signKey,
		kid:       kid,
	}
}

func (p *testOIDCProvider) signJWT(t *testing.T, claims map[string]any) string {
	t.Helper()
	hdrJSON, err := json.Marshal(map[string]string{"alg": "ES256", "typ": "JWT", "kid": p.kid})
	if err != nil {
		t.Fatalf("marshal JWT header: %v", err)
	}
	payloadJSON, err := json.Marshal(claims)
	if err != nil {
		t.Fatalf("marshal JWT claims: %v", err)
	}
	signingInput := base64.RawURLEncoding.EncodeToString(hdrJSON) + "." + base64.RawURLEncoding.EncodeToString(payloadJSON)
	sum := sha256.Sum256([]byte(signingInput))
	r, s, err := ecdsa.Sign(rand.Reader, p.signKey, sum[:])
	if err != nil {
		t.Fatalf("sign JWT: %v", err)
	}
	sig := make([]byte, 64)
	r.FillBytes(sig[:32])
	s.FillBytes(sig[32:])
	return signingInput + "." + base64.RawURLEncoding.EncodeToString(sig)
}

func TestAX_PlatformAgentTurnOverMTLSAndAutoSuspend(t *testing.T) {
	_, natsURL := startServer(t)
	mtls := newTestMTLSBundle(t, DefaultAtenetServerName, "podidentity-gen-1")
	axSvc, axAddr, procSvc, routerAddr := startTestAXAndRouter(t, mtls)

	axSvc.putTask(&axv1alpha1.Task{
		Metadata: &axv1alpha1.ObjectMeta{Atespace: DefaultAXAtespace, Name: DefaultAXTaskName},
		Status: &axv1alpha1.TaskStatus{
			State: axv1alpha1.TaskState_TASK_STATE_SUSPENDED,
			Actor: DefaultAXTaskName,
		},
	})

	cfg := Config{
		NATSURL:          natsURL,
		Executor:         ExecutorAX,
		AXServerAddr:     axAddr,
		AtenetRouterAddr: routerAddr,
		AtenetCAFile:     mtls.caFile,
		AtenetCertFile:   mtls.clientFile,
		AtenetServerName: DefaultAtenetServerName,
		TaskDeadline:     20 * time.Second,
		Scope:            capability.NamespaceScope(""),
	}
	startBridgeConfig(t, cfg, nil)
	c := gatewayClient(t, natsURL)

	submitIn(t, c, "ax-task-1", "ctx-ax-1", "check cluster nodes")
	finished := waitTerminal(t, c, "ax-task-1")
	if finished.State != lib.StateCompleted {
		t.Fatalf("task state = %s, want completed", finished.State)
	}
	if got := finished.Artifact(lib.ArtifactResult).Parts[0].Text; got != "answer for check cluster nodes" {
		t.Fatalf("result = %q, want %q", got, "answer for check cluster nodes")
	}

	select {
	case suspendedKey := <-axSvc.suspendNotified:
		if suspendedKey != "default/platform-agent" {
			t.Fatalf("suspended task = %q, want default/platform-agent", suspendedKey)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("timed out waiting for auto-suspend after idle turn")
	}

	procSvc.mu.Lock()
	calls := slices.Clone(procSvc.calls)
	procSvc.mu.Unlock()
	if len(calls) != 1 {
		t.Fatalf("process calls = %d, want 1", len(calls))
	}
	if calls[0].targetActor != "default/platform-agent" {
		t.Errorf("ate-target-actor = %q, want default/platform-agent", calls[0].targetActor)
	}
	if calls[0].clientCN != "podidentity-gen-1" {
		t.Errorf("mTLS client CN = %q, want podidentity-gen-1", calls[0].clientCN)
	}
	if calls[0].env["HERMES_SESSION_ID"] != "a2a-ctx-ax-1" || calls[0].env["HERMES_SESSION_KEY"] != "a2a-ctx-ax-1" {
		t.Errorf("session env = %v, want a2a-ctx-ax-1", calls[0].env)
	}

	// Rotate the client bundle on disk and verify a new executor handshake picks up the rotated cert.
	mtls.writeClientBundle(t, "podidentity-gen-2")
	exec2, err := newAXExecutor(cfg)
	if err != nil {
		t.Fatalf("newAXExecutor: %v", err)
	}
	t.Cleanup(func() { _ = exec2.Close() })
	out, err := exec2.RunTurn(testCtx(t), "sess-rotated", "verify cert reload")
	if err != nil {
		t.Fatalf("RunTurn after cert rotation: %v", err)
	}
	if out != "answer for verify cert reload" {
		t.Fatalf("RunTurn output = %q, want %q", out, "answer for verify cert reload")
	}
	procSvc.mu.Lock()
	rotatedCN := procSvc.calls[len(procSvc.calls)-1].clientCN
	procSvc.mu.Unlock()
	if rotatedCN != "podidentity-gen-2" {
		t.Fatalf("rotated client CN = %q, want podidentity-gen-2", rotatedCN)
	}
}

func TestAX_GatewayAutoReconcileAndDriftUpdate(t *testing.T) {
	axSvc, axAddr, _, routerAddr := startTestAXAndRouter(t, nil)
	// Seed a drifted agent gateway to verify UpdateGateway repairs it, while sandbox gateway is created fresh.
	axSvc.putGateway(&axv1alpha1.Gateway{
		Metadata: &axv1alpha1.ObjectMeta{Atespace: DefaultAXAtespace, Name: DefaultAXAgentGatewayName},
		Spec: &axv1alpha1.GatewaySpec{
			Egress: []*ateapipb.EgressRule{{
				Http: &ateapipb.HttpRule{Hostnames: []string{"stale.example.com"}},
			}},
		},
	})

	exec, err := newAXExecutor(Config{
		AXServerAddr:      axAddr,
		AXAtespace:        DefaultAXAtespace,
		AXDefaultTask:     DefaultAXTaskName,
		AXAgentImage:      "registry.example.com/ax-kube-agent:test",
		AXSandboxImage:    "registry.example.com/ax-kube-sandbox:test",
		PlatformAgentName: "platform-agent",
		PodNamespace:      "kubeagents-system",
		AtenetRouterAddr:  routerAddr,
		AtenetInsecure:    true,
	})
	if err != nil {
		t.Fatalf("newAXExecutor: %v", err)
	}
	t.Cleanup(func() { _ = exec.Close() })

	if _, err := exec.RunTurn(testCtx(t), "sess-gw", "hello"); err != nil {
		t.Fatalf("RunTurn: %v", err)
	}

	axSvc.mu.Lock()
	createdGWs := slices.Clone(axSvc.createdGateways)
	updatedGWs := slices.Clone(axSvc.updatedGateways)
	agentGW := axSvc.gateways["default/"+DefaultAXAgentGatewayName]
	sandboxGW := axSvc.gateways["default/"+DefaultAXSandboxGatewayName]
	axSvc.mu.Unlock()

	if len(createdGWs) != 1 || createdGWs[0].GetMetadata().GetName() != DefaultAXSandboxGatewayName {
		t.Fatalf("createdGateways = %+v, want [%s]", createdGWs, DefaultAXSandboxGatewayName)
	}
	if len(updatedGWs) != 1 || updatedGWs[0].GetMetadata().GetName() != DefaultAXAgentGatewayName {
		t.Fatalf("updatedGateways = %+v, want [%s]", updatedGWs, DefaultAXAgentGatewayName)
	}
	if len(agentGW.GetSpec().GetEgress()) != 3 {
		t.Fatalf("agent gateway egress rules = %d, want 3", len(agentGW.GetSpec().GetEgress()))
	}
	brokerRule := agentGW.GetSpec().GetEgress()[2].GetHttps()
	if brokerRule == nil || len(brokerRule.GetEffects().GetReplaceHeaders()) != 1 {
		t.Fatalf("agent gateway broker rule = %+v", agentGW.GetSpec().GetEgress()[2])
	}
	brokerJWT := brokerRule.GetEffects().GetReplaceHeaders()[0].GetActorJwt()
	if !slices.Equal(brokerJWT.GetAudiences(), []string{DefaultAXSandboxBrokerAudience}) || brokerJWT.GetExpirationSeconds() != 900 {
		t.Fatalf("agent gateway broker ActorJwt = %+v", brokerJWT)
	}

	if len(sandboxGW.GetSpec().GetEgress()) != 2 {
		t.Fatalf("sandbox gateway egress rules = %d, want 2", len(sandboxGW.GetSpec().GetEgress()))
	}
	kpRule := sandboxGW.GetSpec().GetEgress()[0].GetHttps()
	if kpRule == nil || len(kpRule.GetEffects().GetReplaceHeaders()) != 1 {
		t.Fatalf("sandbox gateway kube-proxy rule = %+v", sandboxGW.GetSpec().GetEgress()[0])
	}
	kpJWT := kpRule.GetEffects().GetReplaceHeaders()[0].GetActorJwt()
	wantKPAud := "https://platform-agent-kube-proxy.kubeagents-system.svc:8443"
	if !slices.Equal(kpJWT.GetAudiences(), []string{wantKPAud}) || kpJWT.GetExpirationSeconds() != 900 {
		t.Fatalf("sandbox gateway kube-proxy ActorJwt = %+v, want aud %s", kpJWT, wantKPAud)
	}
	cpRule := sandboxGW.GetSpec().GetEgress()[1].GetHttps()
	if cpRule == nil || len(cpRule.GetEffects().GetReplaceHeaders()) != 1 {
		t.Fatalf("sandbox gateway credential-proxy rule = %+v", sandboxGW.GetSpec().GetEgress()[1])
	}
	cpJWT := cpRule.GetEffects().GetReplaceHeaders()[0].GetActorJwt()
	if !slices.Equal(cpJWT.GetAudiences(), []string{"kubeagents-credential-proxy"}) || cpJWT.GetExpirationSeconds() != 900 {
		t.Fatalf("sandbox gateway credential-proxy ActorJwt = %+v", cpJWT)
	}
}

func TestAX_ClusterAgentRoutingAndLazyStockAgentTaskCreate(t *testing.T) {
	axSvc, axAddr, procSvc, routerAddr := startTestAXAndRouter(t, nil)

	exec, err := newAXExecutor(Config{
		AXServerAddr:        axAddr,
		AXAtespace:          DefaultAXAtespace,
		AXDefaultTask:       DefaultAXTaskName,
		AXAgentGatewayName:  DefaultAXAgentGatewayName,
		AXAgentImage:        "registry.example.com/ax-kube-agent@sha256:aaaa",
		AXSandboxImage:      "registry.example.com/ax-kube-sandbox@sha256:bbbb",
		AXTaskCPU:           "2",
		AXTaskMemory:        "2Gi",
		PlatformAgentName:   "platform-agent",
		PodNamespace:        "kubeagents-system",
		AtenetRouterAddr:    routerAddr,
		AtenetInsecure:      true,
	})
	if err != nil {
		t.Fatalf("newAXExecutor: %v", err)
	}
	t.Cleanup(func() { _ = exec.Close() })

	ans, err := exec.RunTurn(testCtx(t), "sess-042", "  @cluster-agent-042 inspect kube-system pods  ")
	if err != nil {
		t.Fatalf("RunTurn: %v", err)
	}
	if ans != "answer for inspect kube-system pods" {
		t.Fatalf("answer = %q, want %q", ans, "answer for inspect kube-system pods")
	}

	axSvc.mu.Lock()
	created := slices.Clone(axSvc.createdTasks)
	axSvc.mu.Unlock()
	if len(created) != 1 {
		t.Fatalf("created tasks = %d, want 1 (sandbox stays lazy until first CLI call)", len(created))
	}
	gotTask := created[0]
	if gotTask.GetMetadata().GetName() != "cluster-agent-042" {
		t.Errorf("created task name = %q, want cluster-agent-042", gotTask.GetMetadata().GetName())
	}
	if !gotTask.GetSpec().GetDebug() {
		t.Errorf("created task spec.debug = false, want true")
	}
	if got := gotTask.GetSpec().GetProfile().GetAteTemplate().GetName(); got != "" {
		t.Errorf("created task spec.profile.ateTemplate = %q, want empty on stock AX", got)
	}
	if got := gotTask.GetSpec().GetProfile().GetImage(); got != "registry.example.com/ax-kube-agent@sha256:aaaa" {
		t.Errorf("created task image = %q", got)
	}
	if got := gotTask.GetSpec().GetProfile().GetResources().GetLimits().GetCpu(); got != "2" {
		t.Errorf("created task cpu = %q, want 2", got)
	}
	if got := gotTask.GetSpec().GetProfile().GetResources().GetLimits().GetMemory(); got != "2Gi" {
		t.Errorf("created task memory = %q, want 2Gi", got)
	}
	if got := gotTask.GetSpec().GetGateway().GetName(); got != DefaultAXAgentGatewayName {
		t.Errorf("created task gateway = %q, want %s", got, DefaultAXAgentGatewayName)
	}
	if !slices.Equal(gotTask.GetSpec().GetCommands(), []string{"/usr/local/bin/ax-sandbox-ssh-proxy"}) {
		t.Errorf("created task commands = %v, want [/usr/local/bin/ax-sandbox-ssh-proxy]", gotTask.GetSpec().GetCommands())
	}
	wantBrokerURL := "http://platform-agent-a2a-sandbox-broker.kubeagents-system.svc.cluster.local:8098/v1/sandbox/exec"
	if len(gotTask.GetSpec().GetEnv()) != 1 || gotTask.GetSpec().GetEnv()[0].GetName() != "SANDBOX_BROKER_URL" || gotTask.GetSpec().GetEnv()[0].GetValue() != wantBrokerURL {
		t.Errorf("created task env = %+v, want SANDBOX_BROKER_URL=%s", gotTask.GetSpec().GetEnv(), wantBrokerURL)
	}

	procSvc.mu.Lock()
	call := procSvc.calls[0]
	procSvc.mu.Unlock()
	if call.targetActor != "default/cluster-agent-042" {
		t.Errorf("ate-target-actor = %q, want default/cluster-agent-042", call.targetActor)
	}
	wantCmd := []string{"hermes", "chat", "-Q", "--yolo", "-q", "inspect kube-system pods"}
	if !slices.Equal(call.command, wantCmd) {
		t.Errorf("command = %v, want %v", call.command, wantCmd)
	}

	if _, err := exec.RunTurn(testCtx(t), "sess-empty", "@cluster-agent-042   "); err == nil || !strings.Contains(err.Error(), "empty") {
		t.Fatalf("RunTurn with empty prompt after prefix = %v, want empty-prompt error", err)
	}
	if _, err := exec.RunTurn(testCtx(t), "sess-sb", "@cluster-agent-042-sandbox hi"); err == nil || !strings.Contains(err.Error(), "sandbox") {
		t.Fatalf("RunTurn addressing -sandbox task = %v, want sandbox error", err)
	}
}

func TestAX_SandboxBrokerOIDC1To1PairingStdinAndPairedSuspend(t *testing.T) {
	axSvc, axAddr, procSvc, routerAddr := startTestAXAndRouter(t, nil)
	oidcProv := startTestOIDCProvider(t)

	cfg := Config{
		AXServerAddr:         axAddr,
		AXAtespace:           DefaultAXAtespace,
		AXDefaultTask:        DefaultAXTaskName,
		AXAgentGatewayName:   DefaultAXAgentGatewayName,
		AXSandboxGatewayName: DefaultAXSandboxGatewayName,
		AXAgentImage:         "registry.example.com/ax-kube-agent@sha256:aaaa",
		AXSandboxImage:       "registry.example.com/ax-kube-sandbox@sha256:bbbb",
		AXOIDCIssuerURL:      oidcProv.issuerURL,
		AXOIDCCAFile:         oidcProv.caFile,
		PlatformAgentName:    "platform-agent",
		PodNamespace:         "kubeagents-system",
		AtenetRouterAddr:     routerAddr,
		AtenetInsecure:       true,
	}
	exec, err := newAXExecutor(cfg)
	if err != nil {
		t.Fatalf("newAXExecutor: %v", err)
	}
	t.Cleanup(func() { _ = exec.Close() })

	handler, err := newSandboxBrokerHandler(exec, cfg)
	if err != nil {
		t.Fatalf("newSandboxBrokerHandler: %v", err)
	}
	brokerSrv := httptest.NewServer(handler)
	t.Cleanup(brokerSrv.Close)

	// Health check requires no auth.
	healthResp, err := http.Get(brokerSrv.URL + "/healthz")
	if err != nil {
		t.Fatalf("GET /healthz: %v", err)
	}
	_ = healthResp.Body.Close()
	if healthResp.StatusCode != http.StatusOK {
		t.Fatalf("/healthz status = %d, want 200", healthResp.StatusCode)
	}

	now := time.Now().Unix()
	postExec := func(token string, req SandboxExecRequest) (*http.Response, SandboxExecResponse) {
		t.Helper()
		raw, err := json.Marshal(req)
		if err != nil {
			t.Fatalf("marshal request: %v", err)
		}
		httpReq, err := http.NewRequest(http.MethodPost, brokerSrv.URL+SandboxBrokerExecPath, bytes.NewReader(raw))
		if err != nil {
			t.Fatalf("new request: %v", err)
		}
		httpReq.Header.Set("Content-Type", "application/json")
		if token != "" {
			httpReq.Header.Set("Authorization", "Bearer "+token)
		}
		resp, err := http.DefaultClient.Do(httpReq)
		if err != nil {
			t.Fatalf("POST /v1/sandbox/exec: %v", err)
		}
		defer func() { _ = resp.Body.Close() }()
		var parsed SandboxExecResponse
		if resp.StatusCode == http.StatusOK {
			if err := json.NewDecoder(resp.Body).Decode(&parsed); err != nil {
				t.Fatalf("decode SandboxExecResponse: %v", err)
			}
		}
		return resp, parsed
	}

	// Security rejections: missing token (401), wrong aud (401), expired (401), -sandbox caller (403), wrong atespace (403), non-actor sub (403).
	rejectCases := []struct {
		name       string
		token      string
		wantStatus int
	}{
		{name: "missing_token", token: "", wantStatus: http.StatusUnauthorized},
		{
			name: "wrong_audience",
			token: oidcProv.signJWT(t, map[string]any{
				"iss": oidcProv.issuerURL,
				"sub": "actor/default/cluster-agent-042",
				"aud": "kubeagents-credential-proxy",
				"exp": now + 300,
			}),
			wantStatus: http.StatusUnauthorized,
		},
		{
			name: "expired_token",
			token: oidcProv.signJWT(t, map[string]any{
				"iss": oidcProv.issuerURL,
				"sub": "actor/default/cluster-agent-042",
				"aud": DefaultAXSandboxBrokerAudience,
				"exp": now - 120,
			}),
			wantStatus: http.StatusUnauthorized,
		},
		{
			name: "sandbox_caller_rejected",
			token: oidcProv.signJWT(t, map[string]any{
				"iss": oidcProv.issuerURL,
				"sub": "actor/default/cluster-agent-042-sandbox",
				"aud": DefaultAXSandboxBrokerAudience,
				"exp": now + 300,
			}),
			wantStatus: http.StatusForbidden,
		},
		{
			name: "wrong_atespace_rejected",
			token: oidcProv.signJWT(t, map[string]any{
				"iss": oidcProv.issuerURL,
				"sub": "actor/other-space/cluster-agent-042",
				"aud": DefaultAXSandboxBrokerAudience,
				"exp": now + 300,
			}),
			wantStatus: http.StatusForbidden,
		},
		{
			name: "non_actor_subject_rejected",
			token: oidcProv.signJWT(t, map[string]any{
				"iss": oidcProv.issuerURL,
				"sub": "user/alice",
				"aud": DefaultAXSandboxBrokerAudience,
				"exp": now + 300,
			}),
			wantStatus: http.StatusForbidden,
		},
	}
	for _, tc := range rejectCases {
		t.Run(tc.name, func(t *testing.T) {
			resp, _ := postExec(tc.token, SandboxExecRequest{Command: "id"})
			if resp.StatusCode != tc.wantStatus {
				t.Fatalf("status = %d, want %d", resp.StatusCode, tc.wantStatus)
			}
		})
	}

	// Simulate an active Hermes turn on cluster-agent-042 that invokes the sandbox broker mid-turn!
	validToken := oidcProv.signJWT(t, map[string]any{
		"iss": oidcProv.issuerURL,
		"sub": "actor/default/cluster-agent-042",
		"aud": []string{DefaultAXSandboxBrokerAudience},
		"exp": now + 600,
		"nbf": now - 5,
	})
	binaryStdin := []byte{0x00, 0x01, 0xfe, 0xff, 'k', '8', 's'}
	var brokerResp SandboxExecResponse
	var brokerStatus int
	procSvc.onTurnRun = func(targetActor, _ string) {
		if targetActor != "default/cluster-agent-042" {
			return
		}
		resp, parsed := postExec(validToken, SandboxExecRequest{
			Command:  "kubectl get nodes && exit 42",
			StdinB64: base64.StdEncoding.EncodeToString(binaryStdin),
			Env: map[string]string{
				"CUSTOM_VAR":     "allowed-val",
				"PATH":           "/malicious/bin",
				"LD_PRELOAD":     "/tmp/evil.so",
				"KUBE_PROXY_URL": "https://evil.example.com",
			},
		})
		brokerStatus = resp.StatusCode
		brokerResp = parsed
	}

	ans, err := exec.RunTurn(testCtx(t), "sess-paired", "@cluster-agent-042 run node check")
	if err != nil {
		t.Fatalf("RunTurn: %v", err)
	}
	if ans != "answer for run node check" {
		t.Fatalf("RunTurn answer = %q", ans)
	}
	if brokerStatus != http.StatusOK {
		t.Fatalf("mid-turn sandbox broker status = %d, want 200", brokerStatus)
	}
	if brokerResp.ExitCode != 42 {
		t.Fatalf("sandbox exit_code = %d, want 42", brokerResp.ExitCode)
	}
	gotStdout, err := base64.StdEncoding.DecodeString(brokerResp.StdoutB64)
	if err != nil {
		t.Fatalf("decode stdout_b64: %v", err)
	}
	wantStdout := append([]byte("sandbox:kubectl get nodes && exit 42:"), binaryStdin...)
	if !bytes.Equal(gotStdout, wantStdout) {
		t.Fatalf("sandbox stdout = %x, want %x", gotStdout, wantStdout)
	}
	if brokerResp.Stderr != "sandbox-err\n" {
		t.Fatalf("sandbox stderr = %q, want %q", brokerResp.Stderr, "sandbox-err\n")
	}

	// Verify strict 1-to-1 sandbox Task spec AND paired auto-suspend of BOTH tasks when RunTurn finished!
	axSvc.mu.Lock()
	sandboxTask := axSvc.tasks["default/cluster-agent-042-sandbox"]
	agentSuspends := axSvc.suspendCalls["default/cluster-agent-042"]
	sandboxSuspends := axSvc.suspendCalls["default/cluster-agent-042-sandbox"]
	axSvc.mu.Unlock()

	if sandboxTask == nil {
		t.Fatal("expected paired task default/cluster-agent-042-sandbox to be created")
	}
	if got := sandboxTask.GetSpec().GetGateway().GetName(); got != DefaultAXSandboxGatewayName {
		t.Errorf("sandbox task gateway = %q, want %s", got, DefaultAXSandboxGatewayName)
	}
	if got := sandboxTask.GetSpec().GetProfile().GetImage(); got != "registry.example.com/ax-kube-sandbox@sha256:bbbb" {
		t.Errorf("sandbox task image = %q", got)
	}
	if len(sandboxTask.GetSpec().GetCommands()) != 0 {
		t.Errorf("sandbox task commands = %v, want empty (stock ax-task-runner)", sandboxTask.GetSpec().GetCommands())
	}
	if agentSuspends != 1 || sandboxSuspends != 1 {
		t.Fatalf("paired suspendCalls = (agent=%d, sandbox=%d), want (1, 1)", agentSuspends, sandboxSuspends)
	}
	if sandboxTask.GetStatus().GetState() != axv1alpha1.TaskState_TASK_STATE_SUSPENDED {
		t.Fatalf("sandbox final state = %v, want TASK_STATE_SUSPENDED", sandboxTask.GetStatus().GetState())
	}

	// Verify sandbox process executed in default/cluster-agent-042-sandbox with /workspace cwd and protected env intact.
	procSvc.mu.Lock()
	calls := slices.Clone(procSvc.calls)
	procSvc.mu.Unlock()
	if len(calls) != 2 {
		t.Fatalf("process calls = %d, want 2 (1 agent turn + 1 sandbox exec)", len(calls))
	}
	sbCall := calls[1]
	if sbCall.targetActor != "default/cluster-agent-042-sandbox" {
		t.Errorf("sandbox targetActor = %q, want default/cluster-agent-042-sandbox", sbCall.targetActor)
	}
	if sbCall.cwd != "/workspace" {
		t.Errorf("sandbox cwd = %q, want /workspace", sbCall.cwd)
	}
	if sbCall.env["CUSTOM_VAR"] != "allowed-val" {
		t.Errorf("sandbox CUSTOM_VAR = %q, want allowed-val", sbCall.env["CUSTOM_VAR"])
	}
	if sbCall.env["PATH"] != axSandboxDefaultPATH {
		t.Errorf("sandbox PATH was overwritten: %q", sbCall.env["PATH"])
	}
	if _, leaked := sbCall.env["LD_PRELOAD"]; leaked {
		t.Errorf("LD_PRELOAD must be stripped from caller env")
	}
	if sbCall.env["KUBE_PROXY_URL"] != "https://platform-agent-kube-proxy.kubeagents-system.svc:8443" {
		t.Errorf("sandbox KUBE_PROXY_URL = %q", sbCall.env["KUBE_PROXY_URL"])
	}
	if sbCall.env["CREDENTIAL_PROXY_URL"] != "https://platform-agent-credential-proxy.kubeagents-system.svc:8767" {
		t.Errorf("sandbox CREDENTIAL_PROXY_URL = %q", sbCall.env["CREDENTIAL_PROXY_URL"])
	}
}

func TestAX_ResumeRetriesOnResourceExhaustedAndPollsGetProcessFallback(t *testing.T) {
	axSvc, axAddr, procSvc, routerAddr := startTestAXAndRouter(t, nil)
	axSvc.putTask(&axv1alpha1.Task{
		Metadata: &axv1alpha1.ObjectMeta{Atespace: DefaultAXAtespace, Name: DefaultAXTaskName},
		Status: &axv1alpha1.TaskStatus{
			State: axv1alpha1.TaskState_TASK_STATE_SUSPENDED,
			Actor: DefaultAXTaskName,
		},
	})
	axSvc.resumeFailures = 2
	procSvc.omitExit = true

	exec, err := newAXExecutor(Config{
		AXServerAddr:     axAddr,
		AXAtespace:       DefaultAXAtespace,
		AXDefaultTask:    DefaultAXTaskName,
		AXGatewayName:    DefaultAXGatewayName,
		AtenetRouterAddr: "http://" + routerAddr,
	})
	if err != nil {
		t.Fatalf("newAXExecutor: %v", err)
	}
	exec.resumeInitialBackoff = 10 * time.Millisecond
	exec.resumeMaxBackoff = 25 * time.Millisecond
	exec.getProcessPollInterval = 10 * time.Millisecond
	t.Cleanup(func() { _ = exec.Close() })

	ans, err := exec.RunTurn(testCtx(t), "sess-retry", "retry wakeup")
	if err != nil {
		t.Fatalf("RunTurn: %v", err)
	}
	if ans != "answer for retry wakeup" {
		t.Fatalf("answer = %q, want %q", ans, "answer for retry wakeup")
	}

	axSvc.mu.Lock()
	resumes := axSvc.resumeCalls["default/platform-agent"]
	axSvc.mu.Unlock()
	if resumes != 3 {
		t.Fatalf("ResumeTask calls = %d, want 3 (2 ResourceExhausted + 1 success)", resumes)
	}
}

func TestAX_ConcurrentTurnsShareAwakeTaskAndSuspendOnce(t *testing.T) {
	axSvc, axAddr, procSvc, routerAddr := startTestAXAndRouter(t, nil)
	axSvc.putTask(&axv1alpha1.Task{
		Metadata: &axv1alpha1.ObjectMeta{Atespace: DefaultAXAtespace, Name: DefaultAXTaskName},
		Status: &axv1alpha1.TaskStatus{
			State: axv1alpha1.TaskState_TASK_STATE_SUSPENDED,
			Actor: DefaultAXTaskName,
		},
	})
	hold := make(chan struct{})
	turnActive := make(chan struct{}, 4)
	procSvc.holdTurn = hold
	procSvc.turnActive = turnActive

	exec, err := newAXExecutor(Config{
		AXServerAddr:     axAddr,
		AXAtespace:       DefaultAXAtespace,
		AXDefaultTask:    DefaultAXTaskName,
		AXGatewayName:    DefaultAXGatewayName,
		AtenetRouterAddr: routerAddr,
		AtenetInsecure:   true,
	})
	if err != nil {
		t.Fatalf("newAXExecutor: %v", err)
	}
	t.Cleanup(func() { _ = exec.Close() })

	var wg sync.WaitGroup
	errs := make(chan error, 2)
	for i := 1; i <= 2; i++ {
		wg.Add(1)
		go func(idx int) {
			defer wg.Done()
			_, turnErr := exec.RunTurn(testCtx(t), fmt.Sprintf("sess-%d", idx), fmt.Sprintf("concurrent-%d", idx))
			errs <- turnErr
		}(i)
	}

	for range 2 {
		select {
		case <-turnActive:
		case <-time.After(5 * time.Second):
			t.Fatal("timed out waiting for both turns to reach StreamProcessOutput")
		}
	}

	exec.lifecycleMu.Lock()
	inFlight := exec.activeTurns["default/platform-agent"]
	exec.lifecycleMu.Unlock()
	if inFlight != 2 {
		t.Fatalf("activeTurns while both turns run = %d, want 2", inFlight)
	}
	axSvc.mu.Lock()
	earlySuspends := axSvc.suspendCalls["default/platform-agent"]
	axSvc.mu.Unlock()
	if earlySuspends != 0 {
		t.Fatalf("suspendCalls while turns active = %d, want 0", earlySuspends)
	}

	close(hold)
	wg.Wait()
	close(errs)
	for turnErr := range errs {
		if turnErr != nil {
			t.Fatalf("concurrent RunTurn failed: %v", turnErr)
		}
	}

	axSvc.mu.Lock()
	finalSuspends := axSvc.suspendCalls["default/platform-agent"]
	axSvc.mu.Unlock()
	if finalSuspends != 1 {
		t.Fatalf("suspendCalls after both concurrent turns finished = %d, want 1", finalSuspends)
	}
}

func TestAX_CanceledTurnSignalsProcessWithSIGTERM(t *testing.T) {
	axSvc, axAddr, procSvc, routerAddr := startTestAXAndRouter(t, nil)
	axSvc.putTask(&axv1alpha1.Task{
		Metadata: &axv1alpha1.ObjectMeta{Atespace: DefaultAXAtespace, Name: DefaultAXTaskName},
		Status: &axv1alpha1.TaskStatus{
			State: axv1alpha1.TaskState_TASK_STATE_WORKING,
			Actor: DefaultAXTaskName,
			Conditions: []*axv1alpha1.Condition{
				{Type: axv1alpha1.ConditionWorkspaceReady, Status: "True"},
				{Type: axv1alpha1.ConditionReady, Status: "True"},
			},
		},
	})
	hold := make(chan struct{})
	turnActive := make(chan struct{}, 1)
	procSvc.holdTurn = hold
	procSvc.turnActive = turnActive

	exec, err := newAXExecutor(Config{
		AXServerAddr:     axAddr,
		AXAtespace:       DefaultAXAtespace,
		AXDefaultTask:    DefaultAXTaskName,
		AXGatewayName:    DefaultAXGatewayName,
		AtenetRouterAddr: routerAddr,
		AtenetInsecure:   true,
	})
	if err != nil {
		t.Fatalf("newAXExecutor: %v", err)
	}
	t.Cleanup(func() { _ = exec.Close() })
	t.Cleanup(func() { close(hold) })

	turnCtx, cancel := context.WithCancel(testCtx(t))
	done := make(chan error, 1)
	go func() {
		_, runErr := exec.RunTurn(turnCtx, "sess-cancel", "long command")
		done <- runErr
	}()

	select {
	case <-turnActive:
	case <-time.After(5 * time.Second):
		t.Fatal("timed out waiting for turn to start")
	}
	cancel()

	select {
	case sig := <-procSvc.signalCh:
		if sig != ateenvv1alpha.Signal_SIGNAL_TERM {
			t.Fatalf("SignalProcess signal = %v, want SIGNAL_TERM", sig)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("timed out waiting for SignalProcess(SIGNAL_TERM)")
	}

	select {
	case runErr := <-done:
		if runErr == nil {
			t.Fatal("canceled RunTurn returned nil error")
		}
	case <-time.After(5 * time.Second):
		t.Fatal("timed out waiting for canceled RunTurn to return")
	}
}

func TestAX_StreamReconnectsWithByteOffsets(t *testing.T) {
	axSvc, axAddr, procSvc, routerAddr := startTestAXAndRouter(t, nil)
	axSvc.putTask(&axv1alpha1.Task{
		Metadata: &axv1alpha1.ObjectMeta{Atespace: DefaultAXAtespace, Name: DefaultAXTaskName},
		Status: &axv1alpha1.TaskStatus{
			State: axv1alpha1.TaskState_TASK_STATE_WORKING,
			Actor: DefaultAXTaskName,
			Conditions: []*axv1alpha1.Condition{
				{Type: axv1alpha1.ConditionWorkspaceReady, Status: "True"},
				{Type: axv1alpha1.ConditionReady, Status: "True"},
			},
		},
	})
	procSvc.dropFirstStream = true

	exec, err := newAXExecutor(Config{
		AXServerAddr:     axAddr,
		AXAtespace:       DefaultAXAtespace,
		AXDefaultTask:    DefaultAXTaskName,
		AXGatewayName:    DefaultAXGatewayName,
		AtenetRouterAddr: routerAddr,
		AtenetInsecure:   true,
	})
	if err != nil {
		t.Fatalf("newAXExecutor: %v", err)
	}
	t.Cleanup(func() { _ = exec.Close() })

	out, err := exec.RunTurn(testCtx(t), "sess-reconnect", "long stream")
	if err != nil {
		t.Fatalf("RunTurn after stream reset: %v", err)
	}
	if out != "answer for long stream" {
		t.Fatalf("RunTurn output = %q, want %q", out, "answer for long stream")
	}

	procSvc.mu.Lock()
	reqs := slices.Clone(procSvc.streamRequests)
	procSvc.mu.Unlock()
	if len(reqs) != 2 {
		t.Fatalf("len(streamRequests) = %d, want 2", len(reqs))
	}
	if reqs[0].GetStdoutOffset() != 0 || reqs[1].GetStdoutOffset() <= 0 {
		t.Fatalf("stdout offsets = [%d, %d], want [0, >0]", reqs[0].GetStdoutOffset(), reqs[1].GetStdoutOffset())
	}
}

func TestAX_SelectsPersonaHermesHomeAndPropagatesGKEEnv(t *testing.T) {
	axSvc, axAddr, procSvc, routerAddr := startTestAXAndRouter(t, nil)
	exec, err := newAXExecutor(Config{
		AXServerAddr:         axAddr,
		AXAtespace:           DefaultAXAtespace,
		AXDefaultTask:        DefaultAXTaskName,
		AXAgentGatewayName:   DefaultAXAgentGatewayName,
		AXSandboxGatewayName: DefaultAXSandboxGatewayName,
		AXAgentImage:         "registry.example.com/ax-kube-agent:v1",
		AXSandboxImage:       "registry.example.com/ax-kube-sandbox:v1",
		GKEProjectID:         "demo-proj",
		GKEClusterName:       "kage-management",
		GKELocation:          "us-central1",
		AtenetRouterAddr:     routerAddr,
		AtenetInsecure:       true,
	})
	if err != nil {
		t.Fatalf("newAXExecutor: %v", err)
	}
	t.Cleanup(func() { _ = exec.Close() })

	if _, err := exec.RunTurn(testCtx(t), "sess-platform", "check platform"); err != nil {
		t.Fatalf("RunTurn(platform): %v", err)
	}
	if _, err := exec.RunTurn(testCtx(t), "sess-cluster", "@cluster-agent-042 inspect nodes"); err != nil {
		t.Fatalf("RunTurn(cluster): %v", err)
	}
	if _, _, _, err := exec.ExecInPairedSandbox(testCtx(t), DefaultAXAtespace, DefaultAXTaskName, "env", "", nil, nil); err != nil {
		t.Fatalf("ExecInPairedSandbox: %v", err)
	}

	procSvc.mu.Lock()
	calls := slices.Clone(procSvc.calls)
	procSvc.mu.Unlock()
	if len(calls) != 3 {
		t.Fatalf("len(calls) = %d, want 3", len(calls))
	}
	if got := calls[0].env["HERMES_HOME"]; got != axPlatformHermesHome {
		t.Errorf("platform-agent HERMES_HOME = %q, want %q", got, axPlatformHermesHome)
	}
	if got := calls[1].env["HERMES_HOME"]; got != axClusterHermesHome {
		t.Errorf("cluster-agent-042 HERMES_HOME = %q, want %q", got, axClusterHermesHome)
	}
	for i, name := range []string{"platform-agent", "cluster-agent-042", "platform-agent-sandbox"} {
		for k, want := range map[string]string{
			"GKE_PROJECT_ID":   "demo-proj",
			"GKE_CLUSTER_NAME": "kage-management",
			"GKE_LOCATION":     "us-central1",
		} {
			if got := calls[i].env[k]; got != want {
				t.Errorf("%s StartProcess env[%s] = %q, want %q", name, k, got, want)
			}
		}
	}

	axSvc.mu.Lock()
	created := slices.Clone(axSvc.createdTasks)
	axSvc.mu.Unlock()
	if len(created) != 3 {
		t.Fatalf("len(createdTasks) = %d, want 3", len(created))
	}
	for _, task := range created {
		envMap := map[string]string{}
		for _, ev := range task.GetSpec().GetEnv() {
			envMap[ev.GetName()] = ev.GetValue()
		}
		for k, want := range map[string]string{
			"GKE_PROJECT_ID":   "demo-proj",
			"GKE_CLUSTER_NAME": "kage-management",
			"GKE_LOCATION":     "us-central1",
		} {
			if got := envMap[k]; got != want {
				t.Errorf("task %s spec.env[%s] = %q, want %q", task.GetMetadata().GetName(), k, got, want)
			}
		}
	}
}
