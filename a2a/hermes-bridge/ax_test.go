package hermesbridge

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"fmt"
	"math/big"
	"net"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	ateenvv1alpha "github.com/agent-substrate/env/proto/ateenv/v1alpha"
	axv1alpha1 "github.com/google/ax/pkg/apis/v1alpha1"
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
	resumeFailures  int
	resumeCalls     map[string]int
	suspendCalls    map[string]int
	createdTasks    []*axv1alpha1.Task
	suspendNotified chan string
}

func newFakeAXServer() *fakeAXServer {
	return &fakeAXServer{
		tasks:           make(map[string]*axv1alpha1.Task),
		resumeCalls:     make(map[string]int),
		suspendCalls:    make(map[string]int),
		suspendNotified: make(chan string, 16),
	}
}

func (s *fakeAXServer) putTask(task *axv1alpha1.Task) {
	s.mu.Lock()
	defer s.mu.Unlock()
	key := task.GetMetadata().GetAtespace() + "/" + task.GetMetadata().GetName()
	s.tasks[key] = proto.Clone(task).(*axv1alpha1.Task)
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
	stored.Status.AteActorTemplate = stored.GetSpec().GetProfile().GetAteActorTemplate()
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
	s.suspendCalls[key]++
	t, ok := s.tasks[key]
	if !ok {
		s.mu.Unlock()
		return nil, status.Errorf(codes.NotFound, "task %s not found", key)
	}
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
	targetActor string
	clientCN    string
	command     []string
	env         map[string]string
}

type fakeProcessServer struct {
	ateenvv1alpha.UnimplementedProcessServiceServer

	mu              sync.Mutex
	nextID          int
	calls           []recordedProcessCall
	streamRequests  []*ateenvv1alpha.StreamProcessOutputRequest
	dropFirstStream bool
	signals         []ateenvv1alpha.Signal
	omitExit        bool
	holdTurn        chan struct{}
	turnActive      chan struct{}
	signalCh        chan ateenvv1alpha.Signal
}

func newFakeProcessServer() *fakeProcessServer {
	return &fakeProcessServer{
		signalCh: make(chan ateenvv1alpha.Signal, 8),
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
	p.calls = append(p.calls, recordedProcessCall{
		targetActor: incomingTargetActor(ctx),
		clientCN:    peerCommonName(ctx),
		command:     slices.Clone(req.GetCommand()),
		env:         envCopy,
	})
	return &ateenvv1alpha.Process{
		ProcessId: id,
		Command:   slices.Clone(req.GetCommand()),
		Pid:       int32(1000 + p.nextID),
		State:     ateenvv1alpha.ProcessState_PROCESS_STATE_RUNNING,
	}, nil
}

func (p *fakeProcessServer) StreamProcessOutput(req *ateenvv1alpha.StreamProcessOutputRequest, stream grpc.ServerStreamingServer[ateenvv1alpha.ProcessOutput]) error {
	p.mu.Lock()
	var prompt string
	var sessionID string
	if len(p.calls) > 0 {
		last := p.calls[len(p.calls)-1]
		if len(last.command) > 0 {
			prompt = last.command[len(last.command)-1]
		}
		sessionID = last.env["HERMES_SESSION_ID"]
	}
	hold := p.holdTurn
	active := p.turnActive
	omitExit := p.omitExit
	dropNow := false
	p.streamRequests = append(p.streamRequests, proto.Clone(req).(*ateenvv1alpha.StreamProcessOutputRequest))
	if p.dropFirstStream {
		p.dropFirstStream = false
		dropNow = true
	}
	p.mu.Unlock()

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

	srvCert := issueLeafCert(t, caCert, caKey, "atenet-router", []string{serverDNS}, []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth})
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

func issueLeafCert(t *testing.T, caCert *x509.Certificate, caKey *ecdsa.PrivateKey, cn string, dnsNames []string, eku []x509.ExtKeyUsage) tls.Certificate {
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

func TestAX_ClusterAgentRoutingAndLazyCreateFromSeed(t *testing.T) {
	axSvc, axAddr, procSvc, routerAddr := startTestAXAndRouter(t, nil)
	axSvc.putTask(&axv1alpha1.Task{
		Metadata: &axv1alpha1.ObjectMeta{Atespace: DefaultAXAtespace, Name: "cluster-agent-seed"},
		Status: &axv1alpha1.TaskStatus{
			State:            axv1alpha1.TaskState_TASK_STATE_SUSPENDED,
			Actor:            "cluster-agent-seed",
			AteActorTemplate: "ax-shared-tmpl-abcd1234",
		},
	})

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
		t.Fatalf("created tasks = %d, want 1", len(created))
	}
	gotTask := created[0]
	if gotTask.GetMetadata().GetName() != "cluster-agent-042" {
		t.Errorf("created task name = %q, want cluster-agent-042", gotTask.GetMetadata().GetName())
	}
	if !gotTask.GetSpec().GetDebug() {
		t.Errorf("created task spec.debug = false, want true")
	}
	if got := gotTask.GetSpec().GetProfile().GetAteActorTemplate(); got != "ax-shared-tmpl-abcd1234" {
		t.Errorf("created task template = %q, want ax-shared-tmpl-abcd1234", got)
	}
	if got := gotTask.GetSpec().GetGateway().GetName(); got != DefaultAXGatewayName {
		t.Errorf("created task gateway = %q, want %s", got, DefaultAXGatewayName)
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
