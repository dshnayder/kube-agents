package hermesbridge

import (
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"fmt"
	"io"
	"os"
	"regexp"
	"strings"
	"sync"
	"sync/atomic"
	"time"
	"unicode"

	ateenvv1alpha "github.com/agent-substrate/env/proto/ateenv/v1alpha"
	axv1alpha1 "github.com/google/ax/pkg/apis/v1alpha1"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

const (
	// ExecutorAX runs each turn inside an AX task sandbox over atenet-router.
	ExecutorAX = "ax"

	// DefaultAXServerAddr is the in-cluster gRPC address for ax-server.
	DefaultAXServerAddr = "ax-server.ax-system.svc.cluster.local:8080"
	// DefaultAXAtespace is the default Substrate namespace for AX tasks.
	DefaultAXAtespace = "default"
	// DefaultAXTaskName is the default AX task name when the prompt names none.
	DefaultAXTaskName = "platform-agent"
	// DefaultAXGatewayName is the default AX egress gateway binding for created tasks.
	DefaultAXGatewayName = "kube-agents-cluster-gw"
	// DefaultAtenetRouterAddr is the in-cluster gRPC address for atenet-router.
	DefaultAtenetRouterAddr = "atenet-router.ate-system.svc.cluster.local:443"
	// DefaultAtenetServerName is the TLS server name for atenet-router.
	DefaultAtenetServerName = "atenet-router.ate-system.svc"
	// DefaultAtenetCertFile is the projected PodCertificate bundle path.
	DefaultAtenetCertFile = "/run/podidentity.podcert.ate.dev/credential-bundle.pem"
	// DefaultAtenetCAFile is the projected ClusterTrustBundle CA path.
	DefaultAtenetCAFile = "/run/servicedns-ca/trust-bundle.pem"

	axTargetActorHeader      = "ate-target-actor"
	axSeedTaskPrimary        = "cluster-agent-seed"
	axSeedTaskFallback       = "cluster-agent-001"
	axResumeInitialBackoff   = 200 * time.Millisecond
	axResumeMaxBackoff       = 2 * time.Second
	axResumeRetryWindow      = 30 * time.Second
	axReadyPollInterval      = 250 * time.Millisecond
	axReResumeInterval       = time.Second
	axSuspendTimeout         = 30 * time.Second
	axSignalTimeout          = 5 * time.Second
	axGetProcessPollInterval = 100 * time.Millisecond
)

var (
	clusterAgentPrefixRe = regexp.MustCompile(`^@(cluster-agent-[A-Za-z0-9-]+)(?:\s+(?s:(.*)))?$`)
	ansiEscapeRe         = regexp.MustCompile(`\x1b(?:\[[0-9;?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))`)
	hermesSessionLineRe  = regexp.MustCompile(`(?m)^session_id:[ \t]*\S+[ \t]*\r?\n?`)
)

// RouteClusterAgentPrompt extracts an optional @cluster-agent-NNN target prefix from prompt.
func RouteClusterAgentPrompt(defaultTask, prompt string) (targetTask, cleanPrompt string, err error) {
	trimmedLeft := strings.TrimLeftFunc(prompt, unicode.IsSpace)
	if !strings.HasPrefix(trimmedLeft, "@cluster-agent-") {
		return defaultTask, prompt, nil
	}
	matches := clusterAgentPrefixRe.FindStringSubmatch(trimmedLeft)
	if matches == nil {
		return "", "", fmt.Errorf("malformed @cluster-agent target prefix")
	}
	targetTask = matches[1]
	cleanPrompt = strings.TrimSpace(matches[2])
	if cleanPrompt == "" {
		return "", "", fmt.Errorf("prompt after @%s is empty", targetTask)
	}
	return targetTask, cleanPrompt, nil
}

type axExecutor struct {
	cfg Config

	axConn     *grpc.ClientConn
	routerConn *grpc.ClientConn
	axClient   axv1alpha1.AXClient
	procClient ateenvv1alpha.ProcessServiceClient

	lifecycleMu    sync.Mutex
	activeTurns    map[string]int
	lifecycleLocks map[string]*sync.Mutex

	resumeInitialBackoff   time.Duration
	resumeMaxBackoff       time.Duration
	resumeRetryWindow      time.Duration
	readyPollInterval      time.Duration
	reResumeInterval       time.Duration
	suspendTimeout         time.Duration
	getProcessPollInterval time.Duration
}

func newAXExecutor(cfg Config) (*axExecutor, error) {
	cfg.defaults()
	axAddr := stripGRPCScheme(cfg.AXServerAddr)
	if axAddr == "" {
		return nil, fmt.Errorf("executor %q requires AXServerAddr", ExecutorAX)
	}
	routerRaw := strings.TrimSpace(cfg.AtenetRouterAddr)
	useInsecure := cfg.AtenetInsecure || strings.HasPrefix(routerRaw, "http://")
	routerAddr := stripGRPCScheme(routerRaw)
	if routerAddr == "" {
		return nil, fmt.Errorf("executor %q requires AtenetRouterAddr", ExecutorAX)
	}

	axConn, err := grpc.NewClient(axAddr, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		return nil, fmt.Errorf("dial ax-server %q: %w", axAddr, err)
	}

	var routerCreds credentials.TransportCredentials
	if useInsecure {
		routerCreds = insecure.NewCredentials()
	} else {
		creds, err := buildAtenetTLSCredentials(cfg.AtenetCAFile, cfg.AtenetCertFile, cfg.AtenetServerName)
		if err != nil {
			_ = axConn.Close()
			return nil, err
		}
		routerCreds = creds
	}

	routerConn, err := grpc.NewClient(routerAddr, grpc.WithTransportCredentials(routerCreds))
	if err != nil {
		_ = axConn.Close()
		return nil, fmt.Errorf("dial atenet-router %q: %w", routerAddr, err)
	}

	return &axExecutor{
		cfg:                    cfg,
		axConn:                 axConn,
		routerConn:             routerConn,
		axClient:               axv1alpha1.NewAXClient(axConn),
		procClient:             ateenvv1alpha.NewProcessServiceClient(routerConn),
		activeTurns:            make(map[string]int),
		lifecycleLocks:         make(map[string]*sync.Mutex),
		resumeInitialBackoff:   axResumeInitialBackoff,
		resumeMaxBackoff:       axResumeMaxBackoff,
		resumeRetryWindow:      axResumeRetryWindow,
		readyPollInterval:      axReadyPollInterval,
		reResumeInterval:       axReResumeInterval,
		suspendTimeout:         axSuspendTimeout,
		getProcessPollInterval: axGetProcessPollInterval,
	}, nil
}

func buildAtenetTLSCredentials(caFile, certFile, serverName string) (credentials.TransportCredentials, error) {
	caPath := strings.TrimSpace(caFile)
	certPath := strings.TrimSpace(certFile)
	if caPath == "" {
		return nil, fmt.Errorf("executor %q requires AtenetCAFile when TLS is enabled", ExecutorAX)
	}
	if certPath == "" {
		return nil, fmt.Errorf("executor %q requires AtenetCertFile when TLS is enabled", ExecutorAX)
	}
	caPEM, err := os.ReadFile(caPath) // #nosec G304 -- path comes from operator configuration
	if err != nil {
		return nil, fmt.Errorf("read AtenetCAFile %q: %w", caPath, err)
	}
	rootCAs := x509.NewCertPool()
	if !rootCAs.AppendCertsFromPEM(caPEM) {
		return nil, fmt.Errorf("parse AtenetCAFile %q: no PEM certificates found", caPath)
	}
	tlsCfg := &tls.Config{
		MinVersion: tls.VersionTLS12,
		ServerName: strings.TrimSpace(serverName),
		RootCAs:    rootCAs,
		GetClientCertificate: func(*tls.CertificateRequestInfo) (*tls.Certificate, error) {
			cert, loadErr := tls.LoadX509KeyPair(certPath, certPath)
			if loadErr != nil {
				return nil, fmt.Errorf("load client certificate %q: %w", certPath, loadErr)
			}
			return &cert, nil
		},
	}
	return credentials.NewTLS(tlsCfg), nil
}

func stripGRPCScheme(addr string) string {
	trimmed := strings.TrimSpace(addr)
	trimmed = strings.TrimPrefix(trimmed, "http://")
	trimmed = strings.TrimPrefix(trimmed, "https://")
	return strings.TrimSpace(trimmed)
}

func (e *axExecutor) Close() error {
	var firstErr error
	if e.routerConn != nil {
		if err := e.routerConn.Close(); err != nil && firstErr == nil {
			firstErr = err
		}
	}
	if e.axConn != nil {
		if err := e.axConn.Close(); err != nil && firstErr == nil {
			firstErr = err
		}
	}
	return firstErr
}

func (e *axExecutor) taskKey(atespace, targetTask string) string {
	return atespace + "/" + targetTask
}

func (e *axExecutor) acquireTurn(key string) *sync.Mutex {
	e.lifecycleMu.Lock()
	defer e.lifecycleMu.Unlock()
	e.activeTurns[key]++
	lk, ok := e.lifecycleLocks[key]
	if !ok {
		lk = &sync.Mutex{}
		e.lifecycleLocks[key] = lk
	}
	return lk
}

func (e *axExecutor) releaseTurnAndMaybeSuspend(atespace, targetTask string, taskLock *sync.Mutex) {
	key := e.taskKey(atespace, targetTask)
	e.lifecycleMu.Lock()
	e.activeTurns[key]--
	remaining := e.activeTurns[key]
	if remaining < 0 {
		e.activeTurns[key] = 0
		remaining = 0
	}
	e.lifecycleMu.Unlock()
	if remaining > 0 {
		return
	}

	taskLock.Lock()
	defer taskLock.Unlock()

	e.lifecycleMu.Lock()
	stillIdle := e.activeTurns[key] == 0
	e.lifecycleMu.Unlock()
	if !stillIdle {
		return
	}

	suspendCtx, cancel := context.WithTimeout(context.Background(), e.suspendTimeout)
	defer cancel()
	if _, err := e.axClient.SuspendTask(suspendCtx, &axv1alpha1.SuspendTaskRequest{
		Atespace: atespace,
		Name:     targetTask,
	}); err != nil && e.cfg.Logger != nil {
		e.cfg.Logger.Warn("failed to suspend idle AX task", "atespace", atespace, "task", targetTask, "err", err)
	}
}

// RunTurn ensures the target AX task is active, runs one Hermes turn, and suspends idle tasks.
func (e *axExecutor) RunTurn(ctx context.Context, sessionID, prompt string) (string, error) {
	targetTask, cleanPrompt, err := RouteClusterAgentPrompt(e.cfg.AXDefaultTask, prompt)
	if err != nil {
		return "", err
	}
	atespace := e.cfg.AXAtespace
	key := e.taskKey(atespace, targetTask)
	taskLock := e.acquireTurn(key)
	defer e.releaseTurnAndMaybeSuspend(atespace, targetTask, taskLock)

	actor, err := e.ensureAndResumeTask(ctx, atespace, targetTask, taskLock)
	if err != nil {
		return "", err
	}
	return e.execInActor(ctx, atespace, actor, sessionID, cleanPrompt)
}

func (e *axExecutor) ensureAndResumeTask(ctx context.Context, atespace, targetTask string, taskLock *sync.Mutex) (string, error) {
	taskLock.Lock()
	defer taskLock.Unlock()

	task, err := e.axClient.GetTask(ctx, &axv1alpha1.GetTaskRequest{
		Atespace: atespace,
		Name:     targetTask,
	})
	if status.Code(err) == codes.NotFound {
		task, err = e.createLazyTask(ctx, atespace, targetTask)
	}
	if err != nil {
		return "", fmt.Errorf("get or create AX task %s/%s: %w", atespace, targetTask, err)
	}

	if isTaskWorkingAndReady(task) {
		return task.GetStatus().GetActor(), nil
	}

	if task.GetStatus().GetState() != axv1alpha1.TaskState_TASK_STATE_WORKING {
		resumed, err := e.resumeWithBackoff(ctx, atespace, targetTask)
		if err != nil {
			return "", fmt.Errorf("resume AX task %s/%s: %w", atespace, targetTask, err)
		}
		if isTaskWorkingAndReady(resumed) {
			return resumed.GetStatus().GetActor(), nil
		}
	}

	lastResume := time.Now()
	ticker := time.NewTicker(e.readyPollInterval)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return "", ctx.Err()
		case <-ticker.C:
		}

		task, err = e.axClient.GetTask(ctx, &axv1alpha1.GetTaskRequest{
			Atespace: atespace,
			Name:     targetTask,
		})
		if err != nil {
			if ctx.Err() != nil {
				return "", ctx.Err()
			}
			if isTransientAXCode(status.Code(err)) {
				continue
			}
			return "", fmt.Errorf("poll AX task %s/%s: %w", atespace, targetTask, err)
		}
		if isTaskWorkingAndReady(task) {
			return task.GetStatus().GetActor(), nil
		}

		needsResume := task.GetStatus().GetState() != axv1alpha1.TaskState_TASK_STATE_WORKING ||
			!hasTaskConditionTrue(task, axv1alpha1.ConditionWorkspaceReady)
		if needsResume && time.Since(lastResume) >= e.reResumeInterval {
			lastResume = time.Now()
			resumed, resumeErr := e.resumeWithBackoff(ctx, atespace, targetTask)
			if resumeErr != nil && status.Code(resumeErr) != codes.FailedPrecondition {
				return "", fmt.Errorf("re-resume AX task %s/%s: %w", atespace, targetTask, resumeErr)
			}
			if isTaskWorkingAndReady(resumed) {
				return resumed.GetStatus().GetActor(), nil
			}
		}
	}
}

func (e *axExecutor) createLazyTask(ctx context.Context, atespace, targetTask string) (*axv1alpha1.Task, error) {
	sharedTmpl, err := e.resolveSharedActorTemplate(ctx, atespace)
	if err != nil {
		return nil, err
	}
	spec := &axv1alpha1.TaskSpec{
		Debug: true,
		Profile: &axv1alpha1.Profile{
			AteActorTemplate: sharedTmpl,
		},
	}
	if gw := strings.TrimSpace(e.cfg.AXGatewayName); gw != "" {
		spec.Gateway = &axv1alpha1.GatewayRef{Name: gw}
	}
	created, err := e.axClient.CreateTask(ctx, &axv1alpha1.CreateTaskRequest{
		Task: &axv1alpha1.Task{
			Metadata: &axv1alpha1.ObjectMeta{
				Atespace: atespace,
				Name:     targetTask,
			},
			Spec: spec,
		},
	})
	if status.Code(err) == codes.AlreadyExists {
		return e.axClient.GetTask(ctx, &axv1alpha1.GetTaskRequest{
			Atespace: atespace,
			Name:     targetTask,
		})
	}
	return created, err
}

func (e *axExecutor) resolveSharedActorTemplate(ctx context.Context, atespace string) (string, error) {
	if configured := strings.TrimSpace(e.cfg.AXSharedActorTemplate); configured != "" {
		return configured, nil
	}
	for _, seedName := range []string{axSeedTaskPrimary, axSeedTaskFallback} {
		seed, err := e.axClient.GetTask(ctx, &axv1alpha1.GetTaskRequest{
			Atespace: atespace,
			Name:     seedName,
		})
		if err != nil {
			if status.Code(err) == codes.NotFound {
				continue
			}
			return "", fmt.Errorf("read seed AX task %s/%s: %w", atespace, seedName, err)
		}
		if tmpl := strings.TrimSpace(seed.GetStatus().GetAteActorTemplate()); tmpl != "" {
			return tmpl, nil
		}
		if tmpl := strings.TrimSpace(seed.GetSpec().GetProfile().GetAteActorTemplate()); tmpl != "" {
			return tmpl, nil
		}
	}
	return "", fmt.Errorf("no shared ActorTemplate found on %s or %s in atespace %q", axSeedTaskPrimary, axSeedTaskFallback, atespace)
}

func (e *axExecutor) resumeWithBackoff(ctx context.Context, atespace, targetTask string) (*axv1alpha1.Task, error) {
	deadline := time.Now().Add(e.resumeRetryWindow)
	backoff := e.resumeInitialBackoff
	for {
		task, err := e.axClient.ResumeTask(ctx, &axv1alpha1.ResumeTaskRequest{
			Atespace: atespace,
			Name:     targetTask,
		})
		if err == nil {
			return task, nil
		}
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
		if !isTransientAXCode(status.Code(err)) || time.Now().After(deadline) {
			return nil, err
		}
		wait := backoff
		if remaining := time.Until(deadline); wait > remaining {
			wait = remaining
		}
		timer := time.NewTimer(wait)
		select {
		case <-ctx.Done():
			timer.Stop()
			return nil, ctx.Err()
		case <-timer.C:
		}
		backoff *= 2
		if backoff > e.resumeMaxBackoff {
			backoff = e.resumeMaxBackoff
		}
	}
}

func isTransientAXCode(code codes.Code) bool {
	return code == codes.Unavailable || code == codes.ResourceExhausted || code == codes.Aborted
}

func isReconnectableStreamError(err error) bool {
	code := status.Code(err)
	return code == codes.Unavailable || code == codes.DeadlineExceeded || code == codes.Aborted
}

func isTaskWorkingAndReady(task *axv1alpha1.Task) bool {
	if task == nil || task.GetStatus() == nil {
		return false
	}
	st := task.GetStatus()
	return st.GetState() == axv1alpha1.TaskState_TASK_STATE_WORKING &&
		strings.TrimSpace(st.GetActor()) != "" &&
		hasTaskConditionTrue(task, axv1alpha1.ConditionReady)
}

func hasTaskConditionTrue(task *axv1alpha1.Task, condType string) bool {
	if task == nil || task.GetStatus() == nil {
		return false
	}
	for _, cond := range task.GetStatus().GetConditions() {
		if cond.GetType() == condType && strings.EqualFold(cond.GetStatus(), "True") {
			return true
		}
	}
	return false
}

func (e *axExecutor) execInActor(ctx context.Context, atespace, actor, sessionID, cleanPrompt string) (string, error) {
	targetHeader := atespace + "/" + actor
	outCtx := metadata.AppendToOutgoingContext(ctx, axTargetActorHeader, targetHeader)

	proc, err := e.procClient.StartProcess(outCtx, &ateenvv1alpha.StartProcessRequest{
		Command: []string{"hermes", "chat", "-Q", "--yolo", "-q", cleanPrompt},
		Env: map[string]string{
			"HERMES_DOCKER_EXEC_AS_ROOT": "1",
			"HOME":                       "/root",
			"HERMES_HOME":                "/root/.hermes",
			"HERMES_SESSION_ID":          sessionID,
			"HERMES_SESSION_KEY":         sessionID,
		},
	})
	if err != nil {
		return "", fmt.Errorf("start hermes process in actor %s: %w", targetHeader, err)
	}
	processID := proc.GetProcessId()

	var exited atomic.Bool
	done := make(chan struct{})
	defer close(done)
	go func() {
		select {
		case <-ctx.Done():
			if exited.Load() {
				return
			}
			sigBase := metadata.AppendToOutgoingContext(context.Background(), axTargetActorHeader, targetHeader)
			sigCtx, cancel := context.WithTimeout(sigBase, axSignalTimeout)
			defer cancel()
			_, _ = e.procClient.SignalProcess(sigCtx, &ateenvv1alpha.SignalProcessRequest{
				ProcessId: processID,
				Signal:    ateenvv1alpha.Signal_SIGNAL_TERM,
			})
		case <-done:
		}
	}()

	var stdoutBuf bytes.Buffer
	var stderrBuf bytes.Buffer
	sawExit := false
	var exitCode int32

streamLoop:
	for {
		stream, err := e.procClient.StreamProcessOutput(outCtx, &ateenvv1alpha.StreamProcessOutputRequest{
			ProcessId:    processID,
			StdoutOffset: int64(stdoutBuf.Len()),
			StderrOffset: int64(stderrBuf.Len()),
			Follow:       true,
		})
		if err != nil {
			if outCtx.Err() == nil && isReconnectableStreamError(err) {
				select {
				case <-outCtx.Done():
					return "", outCtx.Err()
				case <-time.After(e.getProcessPollInterval):
					continue streamLoop
				}
			}
			return "", fmt.Errorf("stream process %s output in actor %s: %w", processID, targetHeader, err)
		}

		for {
			msg, recvErr := stream.Recv()
			if msg != nil {
				if outBytes := msg.GetStdout(); len(outBytes) > 0 {
					_, _ = stdoutBuf.Write(outBytes)
				}
				if errBytes := msg.GetStderr(); len(errBytes) > 0 {
					_, _ = stderrBuf.Write(errBytes)
				}
				if exitProc := msg.GetExit(); exitProc != nil {
					sawExit = true
					exitCode = exitProc.GetExitCode()
					exited.Store(true)
				}
			}
			if errors.Is(recvErr, io.EOF) {
				break streamLoop
			}
			if recvErr != nil {
				if sawExit {
					break streamLoop
				}
				if outCtx.Err() == nil && isReconnectableStreamError(recvErr) {
					continue streamLoop
				}
				return "", fmt.Errorf("read process %s output in actor %s: %w", processID, targetHeader, recvErr)
			}
		}
	}

	if !sawExit {
		code, pollErr := e.waitForProcessExit(outCtx, processID, targetHeader)
		if pollErr != nil {
			return "", pollErr
		}
		exitCode = code
		exited.Store(true)
	}

	cleanedOut := cleanHermesOutput(stdoutBuf.String())
	cleanedErr := cleanHermesOutput(stderrBuf.String())
	if exitCode != 0 && cleanedOut == "" {
		if cleanedErr != "" {
			return "", fmt.Errorf("hermes exited with code %d: %s", exitCode, cleanedErr)
		}
		return "", fmt.Errorf("hermes exited with code %d", exitCode)
	}
	return cleanedOut, nil
}

func (e *axExecutor) waitForProcessExit(ctx context.Context, processID, targetHeader string) (int32, error) {
	ticker := time.NewTicker(e.getProcessPollInterval)
	defer ticker.Stop()
	for {
		proc, err := e.procClient.GetProcess(ctx, &ateenvv1alpha.GetProcessRequest{
			ProcessId: processID,
		})
		if err != nil {
			return 0, fmt.Errorf("poll process %s in actor %s: %w", processID, targetHeader, err)
		}
		if proc.GetState() == ateenvv1alpha.ProcessState_PROCESS_STATE_EXITED {
			return proc.GetExitCode(), nil
		}
		select {
		case <-ctx.Done():
			return 0, ctx.Err()
		case <-ticker.C:
		}
	}
}

func cleanHermesOutput(raw string) string {
	withoutANSI := ansiEscapeRe.ReplaceAllString(raw, "")
	withoutSession := hermesSessionLineRe.ReplaceAllString(withoutANSI, "")
	return strings.TrimSpace(withoutSession)
}
