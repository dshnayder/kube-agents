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
	ateapipb "go.ate.dev/substrate/api/ateapi/v1"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
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
	// DefaultAXAgentGatewayName is the default AX Gateway bound to agent tasks.
	DefaultAXAgentGatewayName = "kube-agents-agent-gw"
	// DefaultAXSandboxGatewayName is the default AX Gateway bound to paired sandbox tasks.
	DefaultAXSandboxGatewayName = "kube-agents-sandbox-gw"
	// DefaultAXGatewayName aliases DefaultAXAgentGatewayName.
	DefaultAXGatewayName = DefaultAXAgentGatewayName
	// DefaultAXTaskCPU is the default CPU limit for created AX tasks.
	DefaultAXTaskCPU = "500m"
	// DefaultAXTaskMemory is the default memory limit for created AX tasks.
	DefaultAXTaskMemory = "1Gi"
	// DefaultAXNamespace is the fallback Kubernetes namespace for in-cluster service DNS names.
	DefaultAXNamespace = "kubeagents-system"
	// DefaultAtenetRouterAddr is the in-cluster gRPC address for atenet-router.
	DefaultAtenetRouterAddr = "atenet-router.ate-system.svc.cluster.local:443"
	// DefaultAtenetServerName is the TLS server name for atenet-router.
	DefaultAtenetServerName = "atenet-router.ate-system.svc"
	// DefaultAtenetCertFile is the projected PodCertificate bundle path.
	DefaultAtenetCertFile = "/run/podidentity.podcert.ate.dev/credential-bundle.pem"
	// DefaultAtenetCAFile is the projected ClusterTrustBundle CA path.
	DefaultAtenetCAFile = "/run/servicedns-ca/trust-bundle.pem"

	axTargetActorHeader       = "ate-target-actor"
	axSandboxTaskSuffix       = "-sandbox"
	axAgentSSHProxyBinary     = "/usr/local/bin/ax-sandbox-ssh-proxy"
	axSandboxDefaultWorkdir   = "/workspace"
	axSandboxDefaultPATH      = "/opt/vcs/bin:/opt/credential-proxy/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
	axSandboxEgressCAPath     = "/run/ate/egress-trust-bundle/trust-bundle.pem"
	axSandboxTokenFile        = "/ax/token"
	axSandboxKubeconfigPath   = "/home/agent/.kube/config"
	axActorJWTExpirationSecs  = int64(900)
	axStdinChunkBytes         = 64 << 10
	axResumeInitialBackoff    = 200 * time.Millisecond
	axResumeMaxBackoff        = 2 * time.Second
	axResumeRetryWindow       = 30 * time.Second
	axReadyPollInterval       = 250 * time.Millisecond
	axReResumeInterval        = time.Second
	axSuspendTimeout          = 30 * time.Second
	axSignalTimeout           = 5 * time.Second
	axGetProcessPollInterval  = 100 * time.Millisecond
	credentialProxyTLSPort    = 8767
	credentialProxyAudienceID = "kubeagents-credential-proxy"
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
		if strings.HasSuffix(defaultTask, axSandboxTaskSuffix) {
			return "", "", fmt.Errorf("default task %q must not end with %s", defaultTask, axSandboxTaskSuffix)
		}
		return defaultTask, prompt, nil
	}
	matches := clusterAgentPrefixRe.FindStringSubmatch(trimmedLeft)
	if matches == nil {
		return "", "", fmt.Errorf("malformed @cluster-agent target prefix")
	}
	targetTask = matches[1]
	if strings.HasSuffix(targetTask, axSandboxTaskSuffix) {
		return "", "", fmt.Errorf("target task %q must not address a sandbox task", targetTask)
	}
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

	gatewayMu       sync.Mutex
	gatewaysEnsured map[string]bool

	lifecycleMu        sync.Mutex
	activeTurns        map[string]int
	activeSandboxExecs map[string]int
	touchedSandboxes   map[string]bool
	lifecycleLocks     map[string]*sync.Mutex

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
		gatewaysEnsured:        make(map[string]bool),
		activeTurns:            make(map[string]int),
		activeSandboxExecs:     make(map[string]int),
		touchedSandboxes:       make(map[string]bool),
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

func (e *axExecutor) serviceNamespace() string {
	if ns := strings.TrimSpace(e.cfg.PodNamespace); ns != "" {
		return ns
	}
	return DefaultAXNamespace
}

func (e *axExecutor) platformAgentName() string {
	if name := strings.TrimSpace(e.cfg.PlatformAgentName); name != "" {
		return name
	}
	if name := strings.TrimSpace(e.cfg.AXDefaultTask); name != "" {
		return name
	}
	return DefaultAXTaskName
}

func (e *axExecutor) kubeProxyURL() string {
	return fmt.Sprintf("https://%s-kube-proxy.%s.svc:8443", e.platformAgentName(), e.serviceNamespace())
}

func (e *axExecutor) credentialProxyURL() string {
	return fmt.Sprintf("https://%s-credential-proxy.%s.svc:%d", e.platformAgentName(), e.serviceNamespace(), credentialProxyTLSPort)
}

func (e *axExecutor) sandboxBrokerURL() string {
	if configured := strings.TrimSpace(e.cfg.AXSandboxBrokerURL); configured != "" {
		return configured
	}
	return fmt.Sprintf("https://%s-a2a-sandbox-broker.%s.svc:8098%s", e.platformAgentName(), e.serviceNamespace(), SandboxBrokerExecPath)
}

func (e *axExecutor) buildAgentGateway(atespace string) *axv1alpha1.Gateway {
	ns := e.serviceNamespace()
	agentName := e.platformAgentName()
	brokerSvc := agentName + "-a2a-sandbox-broker"
	return &axv1alpha1.Gateway{
		Metadata: &axv1alpha1.ObjectMeta{
			Atespace: atespace,
			Name:     e.cfg.AXAgentGatewayName,
		},
		Spec: &axv1alpha1.GatewaySpec{
			Egress: []*ateapipb.EgressRule{
				{
					Http: &ateapipb.HttpRule{
						Hostnames: []string{
							"inference-gateway." + ns + ".svc.cluster.local",
							"inference-gateway." + ns + ".svc",
						},
						Ports: &ateapipb.Ports{Numbers: []int32{80}},
					},
				},
				{
					Http: &ateapipb.HttpRule{
						Hostnames: []string{
							"hindsight-api." + ns + ".svc.cluster.local",
							"hindsight-api." + ns + ".svc",
						},
						Ports: &ateapipb.Ports{Numbers: []int32{8888}},
					},
				},
				{
					Https: &ateapipb.HttpsRule{
						Hostnames: []string{
							brokerSvc + "." + ns + ".svc",
							brokerSvc + "." + ns + ".svc.cluster.local",
						},
						Ports: &ateapipb.Ports{Numbers: []int32{8098}},
						Effects: &ateapipb.HttpRuleEffects{
							ReplaceHeaders: []*ateapipb.CredentialHeader{{
								Header: "Authorization",
								Prefix: "Bearer ",
								ActorJwt: &ateapipb.ActorJwtSource{
									Audiences:         []string{DefaultAXSandboxBrokerAudience},
									ExpirationSeconds: axActorJWTExpirationSecs,
								},
							}},
						},
					},
				},
			},
		},
	}
}

func (e *axExecutor) buildSandboxGateway(atespace string) *axv1alpha1.Gateway {
	ns := e.serviceNamespace()
	agentName := e.platformAgentName()
	kubeProxySvc := agentName + "-kube-proxy"
	credProxySvc := agentName + "-credential-proxy"
	return &axv1alpha1.Gateway{
		Metadata: &axv1alpha1.ObjectMeta{
			Atespace: atespace,
			Name:     e.cfg.AXSandboxGatewayName,
		},
		Spec: &axv1alpha1.GatewaySpec{
			Egress: []*ateapipb.EgressRule{
				{
					Https: &ateapipb.HttpsRule{
						Hostnames: []string{
							kubeProxySvc + "." + ns + ".svc",
							kubeProxySvc + "." + ns + ".svc.cluster.local",
						},
						Ports: &ateapipb.Ports{Numbers: []int32{8443}},
						Effects: &ateapipb.HttpRuleEffects{
							ReplaceHeaders: []*ateapipb.CredentialHeader{{
								Header: "Authorization",
								Prefix: "Bearer ",
								ActorJwt: &ateapipb.ActorJwtSource{
									Audiences:         []string{e.kubeProxyURL()},
									ExpirationSeconds: axActorJWTExpirationSecs,
								},
							}},
						},
					},
				},
				{
					Https: &ateapipb.HttpsRule{
						Hostnames: []string{
							credProxySvc + "." + ns + ".svc",
							credProxySvc + "." + ns + ".svc.cluster.local",
						},
						Ports: &ateapipb.Ports{Numbers: []int32{credentialProxyTLSPort}},
						Effects: &ateapipb.HttpRuleEffects{
							ReplaceHeaders: []*ateapipb.CredentialHeader{{
								Header: "Authorization",
								Prefix: "Bearer ",
								ActorJwt: &ateapipb.ActorJwtSource{
									Audiences:         []string{credentialProxyAudienceID},
									ExpirationSeconds: axActorJWTExpirationSecs,
								},
							}},
						},
					},
				},
			},
		},
	}
}

func (e *axExecutor) ensureGateways(ctx context.Context, atespace string) error {
	e.gatewayMu.Lock()
	defer e.gatewayMu.Unlock()
	if e.gatewaysEnsured[atespace] {
		return nil
	}
	for _, desired := range []*axv1alpha1.Gateway{
		e.buildAgentGateway(atespace),
		e.buildSandboxGateway(atespace),
	} {
		if strings.TrimSpace(desired.GetMetadata().GetName()) == "" {
			continue
		}
		if err := e.reconcileGateway(ctx, desired); err != nil {
			return err
		}
	}
	e.gatewaysEnsured[atespace] = true
	return nil
}

func (e *axExecutor) reconcileGateway(ctx context.Context, desired *axv1alpha1.Gateway) error {
	atespace := desired.GetMetadata().GetAtespace()
	name := desired.GetMetadata().GetName()
	existing, err := e.axClient.GetGateway(ctx, &axv1alpha1.GetGatewayRequest{
		Atespace: atespace,
		Name:     name,
	})
	if status.Code(err) == codes.NotFound {
		_, createErr := e.axClient.CreateGateway(ctx, &axv1alpha1.CreateGatewayRequest{
			Gateway: desired,
		})
		if createErr == nil {
			return nil
		}
		if status.Code(createErr) != codes.AlreadyExists {
			return fmt.Errorf("create AX gateway %s/%s: %w", atespace, name, createErr)
		}
		existing, err = e.axClient.GetGateway(ctx, &axv1alpha1.GetGatewayRequest{
			Atespace: atespace,
			Name:     name,
		})
	}
	if err != nil {
		return fmt.Errorf("get AX gateway %s/%s: %w", atespace, name, err)
	}
	if proto.Equal(existing.GetSpec(), desired.GetSpec()) {
		return nil
	}
	if _, err := e.axClient.UpdateGateway(ctx, &axv1alpha1.UpdateGatewayRequest{
		Gateway: desired,
	}); err != nil {
		return fmt.Errorf("update AX gateway %s/%s: %w", atespace, name, err)
	}
	return nil
}

func (e *axExecutor) taskKey(atespace, targetTask string) string {
	return atespace + "/" + targetTask
}

func (e *axExecutor) getTaskLock(key string) *sync.Mutex {
	e.lifecycleMu.Lock()
	defer e.lifecycleMu.Unlock()
	lk, ok := e.lifecycleLocks[key]
	if !ok {
		lk = &sync.Mutex{}
		e.lifecycleLocks[key] = lk
	}
	return lk
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

func (e *axExecutor) acquireSandboxExec(atespace, callerTask, sandboxTask string) *sync.Mutex {
	agentKey := e.taskKey(atespace, callerTask)
	sandboxKey := e.taskKey(atespace, sandboxTask)
	e.lifecycleMu.Lock()
	defer e.lifecycleMu.Unlock()
	e.touchedSandboxes[agentKey] = true
	e.activeSandboxExecs[sandboxKey]++
	lk, ok := e.lifecycleLocks[sandboxKey]
	if !ok {
		lk = &sync.Mutex{}
		e.lifecycleLocks[sandboxKey] = lk
	}
	return lk
}

func (e *axExecutor) releaseSandboxExec(atespace, callerTask, sandboxTask string, sandboxLock *sync.Mutex) {
	agentKey := e.taskKey(atespace, callerTask)
	sandboxKey := e.taskKey(atespace, sandboxTask)

	e.lifecycleMu.Lock()
	e.activeSandboxExecs[sandboxKey]--
	if e.activeSandboxExecs[sandboxKey] < 0 {
		e.activeSandboxExecs[sandboxKey] = 0
	}
	shouldSuspendOrphan := e.activeSandboxExecs[sandboxKey] == 0 && e.activeTurns[agentKey] == 0
	e.lifecycleMu.Unlock()
	if !shouldSuspendOrphan {
		return
	}

	sandboxLock.Lock()
	defer sandboxLock.Unlock()

	e.lifecycleMu.Lock()
	stillIdle := e.activeSandboxExecs[sandboxKey] == 0 && e.activeTurns[agentKey] == 0
	if stillIdle {
		delete(e.touchedSandboxes, agentKey)
	}
	e.lifecycleMu.Unlock()
	if !stillIdle {
		return
	}

	// When multiple bridge replicas serve the sandbox-broker Service, another
	// replica may currently own an active turn for callerTask. Keep the paired
	// sandbox warm while callerTask is WORKING; the turn owner suspends both on
	// turn completion.
	checkCtx, cancel := context.WithTimeout(context.Background(), e.suspendTimeout)
	caller, err := e.axClient.GetTask(checkCtx, &axv1alpha1.GetTaskRequest{
		Atespace: atespace,
		Name:     callerTask,
	})
	cancel()
	if err == nil && caller.GetStatus().GetState() == axv1alpha1.TaskState_TASK_STATE_WORKING {
		return
	}

	e.suspendSingleTask(atespace, sandboxTask)
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

	e.suspendSingleTask(atespace, targetTask)
	e.maybeSuspendPairedSandbox(atespace, targetTask)
}

func (e *axExecutor) maybeSuspendPairedSandbox(atespace, targetTask string) {
	agentKey := e.taskKey(atespace, targetTask)
	sandboxTask := targetTask + axSandboxTaskSuffix
	sandboxKey := e.taskKey(atespace, sandboxTask)
	sandboxLock := e.getTaskLock(sandboxKey)

	sandboxLock.Lock()
	defer sandboxLock.Unlock()

	e.lifecycleMu.Lock()
	agentIdle := e.activeTurns[agentKey] == 0
	sandboxExecIdle := e.activeSandboxExecs[sandboxKey] == 0
	wasTouched := e.touchedSandboxes[agentKey]
	if agentIdle && sandboxExecIdle {
		delete(e.touchedSandboxes, agentKey)
	}
	e.lifecycleMu.Unlock()
	if !agentIdle || !sandboxExecIdle {
		return
	}

	if wasTouched {
		e.suspendSingleTask(atespace, sandboxTask)
		return
	}

	checkCtx, cancel := context.WithTimeout(context.Background(), e.suspendTimeout)
	defer cancel()
	sbTask, err := e.axClient.GetTask(checkCtx, &axv1alpha1.GetTaskRequest{
		Atespace: atespace,
		Name:     sandboxTask,
	})
	if err == nil && sbTask.GetStatus().GetState() == axv1alpha1.TaskState_TASK_STATE_WORKING {
		e.suspendSingleTask(atespace, sandboxTask)
	}
}

func (e *axExecutor) suspendSingleTask(atespace, taskName string) {
	suspendCtx, cancel := context.WithTimeout(context.Background(), e.suspendTimeout)
	defer cancel()
	if _, err := e.axClient.SuspendTask(suspendCtx, &axv1alpha1.SuspendTaskRequest{
		Atespace: atespace,
		Name:     taskName,
	}); err != nil && status.Code(err) != codes.NotFound && e.cfg.Logger != nil {
		e.cfg.Logger.Warn("failed to suspend idle AX task", "atespace", atespace, "task", taskName, "err", err)
	}
}

// RunTurn ensures the target AX agent task is active, runs one Hermes turn, and suspends idle paired tasks.
func (e *axExecutor) RunTurn(ctx context.Context, sessionID, prompt string) (string, error) {
	targetTask, cleanPrompt, err := RouteClusterAgentPrompt(e.cfg.AXDefaultTask, prompt)
	if err != nil {
		return "", err
	}
	atespace := e.cfg.AXAtespace
	key := e.taskKey(atespace, targetTask)
	taskLock := e.acquireTurn(key)
	defer e.releaseTurnAndMaybeSuspend(atespace, targetTask, taskLock)

	actor, err := e.ensureAndResumeTask(ctx, atespace, targetTask, taskLock, e.createLazyAgentTask)
	if err != nil {
		return "", err
	}
	return e.execInActor(ctx, atespace, actor, sessionID, cleanPrompt)
}

// ExecInPairedSandbox ensures callerTask's 1-to-1 paired sandbox task is active and executes command inside it.
func (e *axExecutor) ExecInPairedSandbox(
	ctx context.Context,
	atespace, callerTask, command, cwd string,
	callerEnv map[string]string,
	stdinBytes []byte,
) ([]byte, []byte, int32, error) {
	if strings.HasSuffix(callerTask, axSandboxTaskSuffix) {
		return nil, nil, 0, fmt.Errorf("caller task %q must not be a sandbox task", callerTask)
	}
	sandboxTask := callerTask + axSandboxTaskSuffix
	sandboxLock := e.acquireSandboxExec(atespace, callerTask, sandboxTask)
	defer e.releaseSandboxExec(atespace, callerTask, sandboxTask, sandboxLock)

	actor, err := e.ensureAndResumeTask(ctx, atespace, sandboxTask, sandboxLock, e.createLazySandboxTask)
	if err != nil {
		return nil, nil, 0, err
	}

	workdir := strings.TrimSpace(cwd)
	if workdir == "" {
		workdir = axSandboxDefaultWorkdir
	}
	mergedEnv := map[string]string{
		"HOME":                        "/home/agent",
		"HERMES_HOME":                 "/opt/data",
		"PLATFORM_AGENT_HOME":         "/opt/data",
		"KUBECONFIG":                  axSandboxKubeconfigPath,
		"KUBE_PROXY_URL":              e.kubeProxyURL(),
		"KUBE_PROXY_CA_FILE":          axSandboxEgressCAPath,
		"SSL_CERT_FILE":               axSandboxEgressCAPath,
		"CREDENTIAL_PROXY_URL":        e.credentialProxyURL(),
		"CREDENTIAL_PROXY_TOKEN_FILE": axSandboxTokenFile,
		"PATH":                        axSandboxDefaultPATH,
	}
	for k, v := range callerEnv {
		mergedEnv[k] = v
	}

	return e.runProcessInActor(ctx, atespace, actor, actorProcessSpec{
		Command: []string{"/bin/bash", "-c", command},
		Cwd:     workdir,
		Env:     mergedEnv,
		Stdin:   stdinBytes,
	})
}

type lazyTaskCreator func(ctx context.Context, atespace, taskName string) (*axv1alpha1.Task, error)

func (e *axExecutor) ensureAndResumeTask(
	ctx context.Context,
	atespace, targetTask string,
	taskLock *sync.Mutex,
	createFn lazyTaskCreator,
) (string, error) {
	if err := e.ensureGateways(ctx, atespace); err != nil {
		return "", err
	}

	taskLock.Lock()
	defer taskLock.Unlock()

	task, err := e.axClient.GetTask(ctx, &axv1alpha1.GetTaskRequest{
		Atespace: atespace,
		Name:     targetTask,
	})
	if status.Code(err) == codes.NotFound {
		task, err = createFn(ctx, atespace, targetTask)
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

func (e *axExecutor) buildTaskResources() *axv1alpha1.ResourcesSpec {
	cpu := strings.TrimSpace(e.cfg.AXTaskCPU)
	mem := strings.TrimSpace(e.cfg.AXTaskMemory)
	if cpu == "" && mem == "" {
		return nil
	}
	return &axv1alpha1.ResourcesSpec{
		Limits: &axv1alpha1.ResourceList{
			Cpu:    cpu,
			Memory: mem,
		},
	}
}

func (e *axExecutor) createLazyAgentTask(ctx context.Context, atespace, targetTask string) (*axv1alpha1.Task, error) {
	spec := &axv1alpha1.TaskSpec{
		Debug: true,
		Profile: &axv1alpha1.Profile{
			Image:     strings.TrimSpace(e.cfg.AXAgentImage),
			Resources: e.buildTaskResources(),
		},
		Commands: []string{axAgentSSHProxyBinary},
		Env: []*axv1alpha1.EnvVar{{
			Name:  "SANDBOX_BROKER_URL",
			Value: e.sandboxBrokerURL(),
		}},
	}
	if gw := strings.TrimSpace(e.cfg.AXAgentGatewayName); gw != "" {
		spec.Gateway = &axv1alpha1.GatewayRef{Name: gw}
	}
	return e.createOrGetTask(ctx, atespace, targetTask, spec)
}

func (e *axExecutor) createLazySandboxTask(ctx context.Context, atespace, sandboxTask string) (*axv1alpha1.Task, error) {
	spec := &axv1alpha1.TaskSpec{
		Debug: true,
		Profile: &axv1alpha1.Profile{
			Image:     strings.TrimSpace(e.cfg.AXSandboxImage),
			Resources: e.buildTaskResources(),
		},
		Env: []*axv1alpha1.EnvVar{
			{Name: "KUBE_PROXY_URL", Value: e.kubeProxyURL()},
			{Name: "CREDENTIAL_PROXY_URL", Value: e.credentialProxyURL()},
		},
	}
	if gw := strings.TrimSpace(e.cfg.AXSandboxGatewayName); gw != "" {
		spec.Gateway = &axv1alpha1.GatewayRef{Name: gw}
	}
	return e.createOrGetTask(ctx, atespace, sandboxTask, spec)
}

func (e *axExecutor) createOrGetTask(ctx context.Context, atespace, taskName string, spec *axv1alpha1.TaskSpec) (*axv1alpha1.Task, error) {
	created, err := e.axClient.CreateTask(ctx, &axv1alpha1.CreateTaskRequest{
		Task: &axv1alpha1.Task{
			Metadata: &axv1alpha1.ObjectMeta{
				Atespace: atespace,
				Name:     taskName,
			},
			Spec: spec,
		},
	})
	if status.Code(err) == codes.AlreadyExists {
		return e.axClient.GetTask(ctx, &axv1alpha1.GetTaskRequest{
			Atespace: atespace,
			Name:     taskName,
		})
	}
	return created, err
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
		if !isTransientAXError(err) || time.Now().After(deadline) {
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

func isTransientAXError(err error) bool {
	if err == nil {
		return false
	}
	if isTransientAXCode(status.Code(err)) {
		return true
	}
	msg := err.Error()
	return strings.Contains(msg, "ResourceExhausted") || strings.Contains(msg, "no worker has room")
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

type actorProcessSpec struct {
	Command []string
	Cwd     string
	Env     map[string]string
	Stdin   []byte
}

func (e *axExecutor) execInActor(ctx context.Context, atespace, actor, sessionID, cleanPrompt string) (string, error) {
	stdoutBytes, stderrBytes, exitCode, err := e.runProcessInActor(ctx, atespace, actor, actorProcessSpec{
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
		return "", err
	}

	cleanedOut := cleanHermesOutput(string(stdoutBytes))
	cleanedErr := cleanHermesOutput(string(stderrBytes))
	if exitCode != 0 && cleanedOut == "" {
		if cleanedErr != "" {
			return "", fmt.Errorf("hermes exited with code %d: %s", exitCode, cleanedErr)
		}
		return "", fmt.Errorf("hermes exited with code %d", exitCode)
	}
	return cleanedOut, nil
}

func (e *axExecutor) runProcessInActor(ctx context.Context, atespace, actor string, spec actorProcessSpec) ([]byte, []byte, int32, error) {
	targetHeader := atespace + "/" + actor
	outCtx := metadata.AppendToOutgoingContext(ctx, axTargetActorHeader, targetHeader)

	useStdin := len(spec.Stdin) > 0
	proc, err := e.procClient.StartProcess(outCtx, &ateenvv1alpha.StartProcessRequest{
		Command: spec.Command,
		Cwd:     spec.Cwd,
		Env:     spec.Env,
		Stdin:   useStdin,
	})
	if err != nil {
		return nil, nil, 0, fmt.Errorf("start process in actor %s: %w", targetHeader, err)
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

	var stdinErrCh chan error
	if useStdin {
		stdinErrCh = make(chan error, 1)
		go func() {
			stdinErrCh <- e.writeProcessStdin(outCtx, processID, spec.Stdin)
		}()
	}

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
					return nil, nil, 0, outCtx.Err()
				case <-time.After(e.getProcessPollInterval):
					continue streamLoop
				}
			}
			return nil, nil, 0, fmt.Errorf("stream process %s output in actor %s: %w", processID, targetHeader, err)
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
				return nil, nil, 0, fmt.Errorf("read process %s output in actor %s: %w", processID, targetHeader, recvErr)
			}
		}
	}

	if stdinErrCh != nil {
		if writeErr := <-stdinErrCh; writeErr != nil && !sawExit && outCtx.Err() != nil {
			return nil, nil, 0, writeErr
		}
	}

	if !sawExit {
		code, pollErr := e.waitForProcessExit(outCtx, processID, targetHeader)
		if pollErr != nil {
			return nil, nil, 0, pollErr
		}
		exitCode = code
		exited.Store(true)
	}

	return stdoutBuf.Bytes(), stderrBuf.Bytes(), exitCode, nil
}

func (e *axExecutor) writeProcessStdin(ctx context.Context, processID string, data []byte) error {
	stream, err := e.procClient.WriteProcessInput(ctx)
	if err != nil {
		return fmt.Errorf("open WriteProcessInput stream for %s: %w", processID, err)
	}
	for offset := 0; offset < len(data); offset += axStdinChunkBytes {
		end := min(offset+axStdinChunkBytes, len(data))
		isLast := end == len(data)
		msg := &ateenvv1alpha.WriteProcessInputRequest{
			Data:  data[offset:end],
			Close: isLast,
		}
		if offset == 0 {
			msg.ProcessId = processID
		}
		if err := stream.Send(msg); err != nil {
			return err
		}
	}
	_, err = stream.CloseAndRecv()
	return err
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
