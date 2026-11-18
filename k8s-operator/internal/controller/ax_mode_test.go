/*
Copyright 2026.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package controller

import (
	"context"
	"reflect"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	policyv1 "k8s.io/api/policy/v1"
	"k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

func TestAXModeHelpers(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "platform-agent", Namespace: "kubeagents-system"},
	}

	if axModeEnabled() {
		t.Fatal("expected axModeEnabled() to be false by default")
	}
	if got := kubeProxyServiceURL(agent); got != "https://platform-agent-kube-proxy.kubeagents-system.svc.cluster.local:8443" {
		t.Fatalf("kubeProxyServiceURL(default) = %q", got)
	}
	if got := a2aCalloutReplicas(); got != 2 {
		t.Fatalf("a2aCalloutReplicas(default) = %d, want 2", got)
	}
	if got := a2aVerifierReplicas(); got != 2 {
		t.Fatalf("a2aVerifierReplicas(default) = %d, want 2", got)
	}

	t.Setenv("AX_ENABLED", "true")
	if !axModeEnabled() {
		t.Fatal("expected axModeEnabled() to be true when AX_ENABLED=true")
	}
	if got := kubeProxyServiceURL(agent); got != "https://k8s-api-proxy.ax-system.svc.cluster.local:8443" {
		t.Fatalf("kubeProxyServiceURL(AX) = %q", got)
	}
	if got := kubeProxyURL(agent); got != "https://k8s-api-proxy.ax-system.svc.cluster.local:8443" {
		t.Fatalf("kubeProxyURL(AX) = %q", got)
	}
	if got := axServerAddr(); got != "ax-server.ax-system.svc.cluster.local:8080" {
		t.Fatalf("axServerAddr(default) = %q", got)
	}
	t.Setenv("AX_SERVER_ADDR", "custom-ax.ax-system.svc:9090")
	if got := axServerAddr(); got != "custom-ax.ax-system.svc:9090" {
		t.Fatalf("axServerAddr(custom) = %q", got)
	}
	if got := atenetRouterAddr(); got != "atenet-router.ate-system.svc.cluster.local:443" {
		t.Fatalf("atenetRouterAddr(default) = %q", got)
	}
	t.Setenv("ATENET_ROUTER_ADDR", "custom-router.ate-system.svc:8443")
	if got := atenetRouterAddr(); got != "custom-router.ate-system.svc:8443" {
		t.Fatalf("atenetRouterAddr(custom) = %q", got)
	}
	if got := a2aCalloutReplicas(); got != 1 {
		t.Fatalf("a2aCalloutReplicas(AX) = %d, want 1", got)
	}
	if got := a2aVerifierReplicas(); got != 1 {
		t.Fatalf("a2aVerifierReplicas(AX) = %d, want 1", got)
	}
}

func TestAXModeDeletesLegacyWorkloadsAndSkipsSandboxKeys(t *testing.T) {
	t.Setenv("AX_ENABLED", "true")
	ctx := context.Background()
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "platform-agent", Namespace: "kubeagents-system"},
		Spec: agentv1alpha1.PlatformAgentSpec{
			Security: &agentv1alpha1.SecuritySpec{EgressPolicy: "Allowlist"},
		},
	}

	preexisting := []client.Object{
		agent,
		&appsv1.Deployment{ObjectMeta: metav1.ObjectMeta{Name: kubeProxyName(agent), Namespace: agent.Namespace}},
		&corev1.Service{ObjectMeta: metav1.ObjectMeta{Name: kubeProxyName(agent), Namespace: agent.Namespace}},
		&policyv1.PodDisruptionBudget{ObjectMeta: metav1.ObjectMeta{Name: kubeProxyName(agent), Namespace: agent.Namespace}},
		&networkingv1.NetworkPolicy{ObjectMeta: metav1.ObjectMeta{Name: kubeProxyName(agent), Namespace: agent.Namespace}},
		&appsv1.StatefulSet{ObjectMeta: metav1.ObjectMeta{Name: shellSandboxName(agent), Namespace: agent.Namespace}},
		&corev1.Service{ObjectMeta: metav1.ObjectMeta{Name: shellSandboxName(agent), Namespace: agent.Namespace}},
		&networkingv1.NetworkPolicy{ObjectMeta: metav1.ObjectMeta{Name: shellSandboxName(agent), Namespace: agent.Namespace}},
		&appsv1.Deployment{ObjectMeta: metav1.ObjectMeta{Name: agent.Name + "-gateway", Namespace: agent.Namespace}},
		&appsv1.StatefulSet{ObjectMeta: metav1.ObjectMeta{Name: agent.Name + "-gateway", Namespace: agent.Namespace}},
		&corev1.PersistentVolumeClaim{ObjectMeta: metav1.ObjectMeta{Name: agent.Name + "-data", Namespace: agent.Namespace}},
		&corev1.Service{ObjectMeta: metav1.ObjectMeta{Name: agent.Name, Namespace: agent.Namespace}},
		&policyv1.PodDisruptionBudget{ObjectMeta: metav1.ObjectMeta{Name: agent.Name, Namespace: agent.Namespace}},
		&policyv1.PodDisruptionBudget{ObjectMeta: metav1.ObjectMeta{Name: agent.Name + "-gateway-pdb", Namespace: agent.Namespace}},
		&networkingv1.NetworkPolicy{ObjectMeta: metav1.ObjectMeta{Name: agent.Name + "-gateway-netpol", Namespace: agent.Namespace}},
	}

	scheme := setupScheme()
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(preexisting...).
		WithStatusSubresource(agent).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()
	r := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme}

	if err := r.reconcilePVC(ctx, agent); err != nil {
		t.Fatalf("reconcilePVC: %v", err)
	}
	if err := r.reconcileKubeProxy(ctx, agent, "", true); err != nil {
		t.Fatalf("reconcileKubeProxy: %v", err)
	}
	if err := r.reconcileShellSandbox(ctx, agent, "settings-hash"); err != nil {
		t.Fatalf("reconcileShellSandbox: %v", err)
	}
	if err := r.reconcileWorkload(ctx, agent, "cfg", "fb", "set", "pol", nil, "", true); err != nil {
		t.Fatalf("reconcileWorkload: %v", err)
	}
	if err := r.reconcileService(ctx, agent); err != nil {
		t.Fatalf("reconcileService: %v", err)
	}
	if err := r.reconcilePodDisruptionBudget(ctx, agent); err != nil {
		t.Fatalf("reconcilePodDisruptionBudget: %v", err)
	}
	if err := r.reconcileNetworkPolicy(ctx, agent, netpolProfile{Generated: true}, "", true); err != nil {
		t.Fatalf("reconcileNetworkPolicy: %v", err)
	}
	if err := r.reconcileAgentEgressPolicy(ctx, agent, nil, ""); err != nil {
		t.Fatalf("reconcileAgentEgressPolicy: %v", err)
	}

	for _, deleted := range preexisting[1:] {
		probe := deleted.DeepCopyObject().(client.Object)
		err := cl.Get(ctx, client.ObjectKeyFromObject(deleted), probe)
		if !errors.IsNotFound(err) {
			t.Errorf("expected %T %s/%s to be deleted in AX mode, got err=%v", deleted, deleted.GetNamespace(), deleted.GetName(), err)
		}
	}

	// reconcileAgentEgressPolicy must be a no-op and not create <agent>-sandbox-metadata-deny.
	egressNP := &networkingv1.NetworkPolicy{}
	if err := cl.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: agent.Name + "-sandbox-metadata-deny"}, egressNP); !errors.IsNotFound(err) {
		t.Errorf("expected egress NetworkPolicy not to be created in AX mode, got err=%v", err)
	}

	if reason, msg := r.checkShellSandboxKeys(ctx, agent); reason != "" || msg != "" {
		t.Errorf("checkShellSandboxKeys(AX) = (%q, %q), want empty", reason, msg)
	}
}

func TestAXModeInjectsBridgeIntoA2AGateway(t *testing.T) {
	t.Setenv("AX_ENABLED", "true")
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "platform-agent", Namespace: "kubeagents-system"},
	}

	dep := buildA2AGatewayDeployment(agent)
	var bridge *corev1.Container
	for i := range dep.Spec.Template.Spec.Containers {
		if dep.Spec.Template.Spec.Containers[i].Name == a2aBridgeContainerName {
			bridge = &dep.Spec.Template.Spec.Containers[i]
			break
		}
	}
	if bridge == nil {
		t.Fatalf("expected %s container in %s Deployment", a2aBridgeContainerName, dep.Name)
	}
	if bridge.WorkingDir != "/" {
		t.Errorf("bridge.WorkingDir = %q, want /", bridge.WorkingDir)
	}

	envByName := map[string]corev1.EnvVar{}
	for _, e := range bridge.Env {
		envByName[e.Name] = e
	}
	wantValues := map[string]string{
		"NATS_URL":                  a2aNATSClientURL(agent),
		"NATS_USER":                 "bridge",
		"BRIDGE_CONCURRENCY":        "2",
		"BRIDGE_DURABLE_PRECREATED": "true",
		"BRIDGE_EXECUTOR":           "ax",
		"AX_SERVER_ADDR":            "ax-server.ax-system.svc.cluster.local:8080",
		"ATENET_ROUTER_ADDR":        "atenet-router.ate-system.svc.cluster.local:443",
		"AX_ATESPACE":               "default",
		"AX_DEFAULT_TASK":           "platform-agent",
		"AX_GATEWAY_NAME":           "kube-agents-cluster-gw",
		"ATENET_CERT_FILE":          "/run/podidentity.podcert.ate.dev/credential-bundle.pem",
		"ATENET_CA_FILE":            "/run/servicedns-ca/trust-bundle.pem",
		"BRIDGE_ACTIVITY_LISTEN":    "off",
		"A2A_CAPABILITY_REQUIRED":   a2aCapabilityRequired(),
	}
	for k, want := range wantValues {
		if got := envByName[k].Value; got != want {
			t.Errorf("bridge env %s = %q, want %q", k, got, want)
		}
	}
	for _, removed := range []string{"BRIDGE_API_URL", "API_SERVER_KEY"} {
		if _, present := envByName[removed]; present {
			t.Errorf("bridge env %s must not be set in AX mode", removed)
		}
	}
	pw := envByName["NATS_PASSWORD"]
	if pw.ValueFrom == nil || pw.ValueFrom.SecretKeyRef == nil ||
		pw.ValueFrom.SecretKeyRef.Name != a2aCredsSecretName(agent) ||
		pw.ValueFrom.SecretKeyRef.Key != a2aBridgePasswordKey {
		t.Errorf("bridge NATS_PASSWORD = %+v, want secret %s/%s", pw, a2aCredsSecretName(agent), a2aBridgePasswordKey)
	}
	podNS := envByName["POD_NAMESPACE"]
	if podNS.ValueFrom == nil || podNS.ValueFrom.FieldRef == nil || podNS.ValueFrom.FieldRef.FieldPath != "metadata.namespace" {
		t.Errorf("bridge POD_NAMESPACE = %+v, want downward API metadata.namespace", podNS)
	}

	wantMounts := []corev1.VolumeMount{
		a2aNATSCABundleMount(),
		{Name: "servicedns-ca", MountPath: "/run/servicedns-ca", ReadOnly: true},
		{Name: "podidentity", MountPath: "/run/podidentity.podcert.ate.dev", ReadOnly: true},
	}
	if !reflect.DeepEqual(bridge.VolumeMounts, wantMounts) {
		t.Errorf("bridge.VolumeMounts = %+v, want %+v", bridge.VolumeMounts, wantMounts)
	}

	var ctb *corev1.ClusterTrustBundleProjection
	var podCert *corev1.PodCertificateProjection
	var podCertMode *int32
	for _, v := range dep.Spec.Template.Spec.Volumes {
		if v.Name == "servicedns-ca" && v.Projected != nil && len(v.Projected.Sources) == 1 {
			ctb = v.Projected.Sources[0].ClusterTrustBundle
		}
		if v.Name == "podidentity" && v.Projected != nil && len(v.Projected.Sources) == 1 {
			podCertMode = v.Projected.DefaultMode
			podCert = v.Projected.Sources[0].PodCertificate
		}
	}
	if ctb == nil {
		t.Fatalf("expected projected ClusterTrustBundle volume servicedns-ca on %s, got %+v", dep.Name, dep.Spec.Template.Spec.Volumes)
	}
	if ctb.SignerName == nil || *ctb.SignerName != "servicedns.podcert.ate.dev/identity" || ctb.Path != "trust-bundle.pem" {
		t.Errorf("gateway servicedns-ca ClusterTrustBundle = %+v", ctb)
	}
	if podCert == nil {
		t.Fatalf("expected projected PodCertificate volume podidentity on %s, got %+v", dep.Name, dep.Spec.Template.Spec.Volumes)
	}
	if podCertMode == nil || *podCertMode != int32(0444) {
		t.Errorf("podidentity DefaultMode = %v, want 0444", podCertMode)
	}
	if podCert.SignerName != "podidentity.podcert.ate.dev/identity" ||
		podCert.KeyType != "ECDSAP256" ||
		podCert.CredentialBundlePath != "credential-bundle.pem" {
		t.Errorf("podidentity PodCertificate = %+v", podCert)
	}
}

func TestAXModeCredentialProxyOIDCAndNetworkPolicy(t *testing.T) {
	t.Setenv("AX_ENABLED", "true")
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "platform-agent", Namespace: "kubeagents-system"},
	}

	dep := buildCredentialProxyDeployment(agent, "policy-hash")
	container := dep.Spec.Template.Spec.Containers[0]

	envByName := map[string]string{}
	for _, e := range container.Env {
		envByName[e.Name] = e.Value
	}
	if got := envByName["CREDENTIAL_PROXY_OIDC_ISSUER_URL"]; got != "https://idp.ate-system.svc" {
		t.Errorf("CREDENTIAL_PROXY_OIDC_ISSUER_URL = %q, want https://idp.ate-system.svc", got)
	}
	if got := envByName["CREDENTIAL_PROXY_OIDC_CA_FILE"]; got != "/run/servicedns-ca/trust-bundle.pem" {
		t.Errorf("CREDENTIAL_PROXY_OIDC_CA_FILE = %q, want /run/servicedns-ca/trust-bundle.pem", got)
	}
	if got := envByName["CREDENTIAL_PROXY_OIDC_REQUIRED_SUB_PREFIX"]; got != "actor/" {
		t.Errorf("CREDENTIAL_PROXY_OIDC_REQUIRED_SUB_PREFIX = %q, want actor/", got)
	}
	if got := envByName["CREDENTIAL_PROXY_OIDC_UID_CLAIM"]; got != `actor\.ate\.dev.actorUID` {
		t.Errorf("CREDENTIAL_PROXY_OIDC_UID_CLAIM = %q, want %q", got, `actor\.ate\.dev.actorUID`)
	}

	foundMount := false
	for _, m := range container.VolumeMounts {
		if m.Name == "servicedns-ca" && m.MountPath == "/run/servicedns-ca" && m.ReadOnly {
			foundMount = true
			break
		}
	}
	if !foundMount {
		t.Errorf("expected read-only servicedns-ca mount at /run/servicedns-ca on %s, got %+v", container.Name, container.VolumeMounts)
	}

	var ctb *corev1.ClusterTrustBundleProjection
	for _, v := range dep.Spec.Template.Spec.Volumes {
		if v.Name == "servicedns-ca" && v.Projected != nil && len(v.Projected.Sources) == 1 {
			ctb = v.Projected.Sources[0].ClusterTrustBundle
			break
		}
	}
	if ctb == nil {
		t.Fatalf("expected projected ClusterTrustBundle volume servicedns-ca, got %+v", dep.Spec.Template.Spec.Volumes)
	}
	if ctb.SignerName == nil || *ctb.SignerName != "servicedns.podcert.ate.dev/identity" || ctb.Path != "trust-bundle.pem" {
		t.Errorf("ClusterTrustBundle = %+v", ctb)
	}
	if ctb.LabelSelector == nil || ctb.LabelSelector.MatchLabels["podcert.ate.dev/canarying"] != "live" {
		t.Errorf("ClusterTrustBundle.LabelSelector = %+v", ctb.LabelSelector)
	}

	np := buildCredentialProxyNetworkPolicy(agent)
	admitsATEEgress := false
	for _, peer := range np.Spec.Ingress[0].From {
		if peer.NamespaceSelector != nil &&
			peer.NamespaceSelector.MatchLabels[labelMetadataName] == "ate-system" &&
			peer.PodSelector != nil &&
			peer.PodSelector.MatchLabels["app"] == "atenet-egress" {
			admitsATEEgress = true
			break
		}
	}
	if !admitsATEEgress {
		t.Errorf("expected credential-proxy NetworkPolicy port 8080 ingress to admit ate-system/atenet-egress, got %+v", np.Spec.Ingress[0].From)
	}
}

func TestAXModeReadinessTransitionsReadyWithoutLegacyGatewayOrShell(t *testing.T) {
	t.Setenv("AX_ENABLED", "true")
	agent := splitReadinessAgent()

	a2aGw := &appsv1.Deployment{
		ObjectMeta: metav1.ObjectMeta{Name: a2aGatewayName(agent), Namespace: agent.Namespace},
		Status:     appsv1.DeploymentStatus{ReadyReplicas: 1},
	}
	phase, msg := settleStatus(t, agent, append(a2aStackUp(agent), credentialBroker(agent, 1), a2aGw)...)
	if phase != "Ready" {
		t.Fatalf("updateStatusReady(AX) phase = %q (msg = %q), want Ready", phase, msg)
	}
	if agent.Status.DeploymentStatus.Name != a2aGatewayName(agent) || agent.Status.DeploymentStatus.ReadyReplicas != 1 {
		t.Errorf("agent.Status.DeploymentStatus = %+v, want %s with 1 ready replica", agent.Status.DeploymentStatus, a2aGatewayName(agent))
	}
}
