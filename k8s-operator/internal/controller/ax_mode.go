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
	"fmt"
	"os"
	"strings"

	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/intstr"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const (
	axEnabledEnvVar            = "AX_ENABLED"
	axServerAddrEnvVar         = "AX_SERVER_ADDR"
	atenetRouterAddrEnvVar     = "ATENET_ROUTER_ADDR"
	axAgentImageEnvVar         = "AX_AGENT_IMAGE"
	axSandboxImageEnvVar       = "AX_SANDBOX_IMAGE"
	axAgentGatewayNameEnvVar   = "AX_AGENT_GATEWAY_NAME"
	axSandboxGatewayNameEnvVar = "AX_SANDBOX_GATEWAY_NAME"
	axTaskCPUEnvVar            = "AX_TASK_CPU"
	axTaskMemoryEnvVar         = "AX_TASK_MEMORY"

	defaultAXServerAddr     = "ax-server.ax-system.svc.cluster.local:8080"
	defaultAtenetRouterAddr = "atenet-router.ate-system.svc.cluster.local:443"

	credentialProxyOIDCIssuerURLEnv         = "CREDENTIAL_PROXY_OIDC_ISSUER_URL"
	credentialProxyOIDCCAFileEnv            = "CREDENTIAL_PROXY_OIDC_CA_FILE"
	credentialProxyOIDCRequiredSubPrefixEnv = "CREDENTIAL_PROXY_OIDC_REQUIRED_SUB_PREFIX"
	credentialProxyOIDCUIDClaimEnv          = "CREDENTIAL_PROXY_OIDC_UID_CLAIM"
	credentialProxyTLSPortEnv               = "CREDENTIAL_PROXY_TLS_PORT"
	credentialProxyTLSCertFileEnv           = "CREDENTIAL_PROXY_TLS_CERT_FILE"
	credentialProxyTLSPort                  = int32(8767)
	credentialProxyTLSPortName              = "cred-proxy-tls"

	kubeProxyAuthModeEnv             = "KUBE_PROXY_AUTH_MODE"
	kubeProxyJWTIssuerEnv            = "KUBE_PROXY_JWT_ISSUER"
	kubeProxyJWTAudiencesEnv         = "KUBE_PROXY_JWT_AUDIENCES"
	kubeProxyJWTCAFileEnv            = "KUBE_PROXY_JWT_CA_FILE"
	kubeProxyJWTRequiredSubPrefixEnv = "KUBE_PROXY_JWT_REQUIRED_SUB_PREFIX"
	kubeProxyJWTPartitionClaimEnv    = "KUBE_PROXY_JWT_PARTITION_CLAIM"
	kubeProxyJWTUIDClaimEnv          = "KUBE_PROXY_JWT_UID_CLAIM"
	kubeProxyJWTAllowedPartitionsEnv = "KUBE_PROXY_JWT_ALLOWED_PARTITIONS"
	kubeProxyPartitionByCallerEnv    = "KUBE_PROXY_PARTITION_BY_CALLER"
	axKubeProxyAuthMode              = "jwt,token-review"
	axKubeProxyPartitionByCaller     = "partition"

	axOIDCIssuerURL            = "https://idp.ate-system.svc"
	axOIDCRequiredSubPrefix    = "actor/"
	axOIDCPartitionClaim       = `actor\.ate\.dev.atespace`
	axOIDCUIDClaim             = `actor\.ate\.dev.actorUID`
	axServiceDNSCAName         = "servicedns-ca"
	axServiceDNSCAMountPath    = "/run/servicedns-ca"
	axServiceDNSCAPath         = "trust-bundle.pem"
	axOIDCCAFile               = axServiceDNSCAMountPath + "/" + axServiceDNSCAPath
	axServiceDNSSignerName     = "servicedns.podcert.ate.dev/identity"
	axServiceDNSCertVolumeName = "servicedns-cert"
	axServiceDNSCertMountPath  = "/run/servicedns-cert"
	axServiceDNSCertBundlePath = "credential-bundle.pem"
	axServiceDNSCertFile       = axServiceDNSCertMountPath + "/" + axServiceDNSCertBundlePath
	axCanaryingLabelKey        = "podcert.ate.dev/canarying"
	axCanaryingLabelLive       = "live"
	axPodIdentityVolumeName    = "podidentity"
	axPodIdentityMountPath     = "/run/podidentity.podcert.ate.dev"
	axPodIdentityBundlePath    = "credential-bundle.pem"
	axPodIdentityCertFile      = axPodIdentityMountPath + "/" + axPodIdentityBundlePath
	axPodIdentitySignerName    = "podidentity.podcert.ate.dev/identity"
	axPodIdentityKeyType       = "ECDSAP256"
	ateSystemNamespace         = "ate-system"
	ateEgressAppLabel          = "atenet-egress"
	a2aBridgeExecutorAX        = "ax"
	axDefaultAtespace          = "default"
	axDefaultAgentGatewayName   = "kube-agents-agent-gw"
	axDefaultSandboxGatewayName = "kube-agents-sandbox-gw"
	axDefaultTaskCPU            = "500m"
	axDefaultTaskMemory         = "1Gi"
	axBridgeConcurrencyValue    = "2"
	axSandboxBrokerPort         = int32(8098)
	axSandboxBrokerPortName     = "sandbox-broker"
	axSandboxBrokerListenAddr   = ":8098"
	axSandboxBrokerExecPath     = "/v1/sandbox/exec"
)

func axModeEnabled() bool {
	return os.Getenv(axEnabledEnvVar) == "true"
}

func axServerAddr() string {
	if v := strings.TrimSpace(os.Getenv(axServerAddrEnvVar)); v != "" {
		return v
	}
	return defaultAXServerAddr
}

func atenetRouterAddr() string {
	if v := strings.TrimSpace(os.Getenv(atenetRouterAddrEnvVar)); v != "" {
		return v
	}
	return defaultAtenetRouterAddr
}

func axAgentGatewayName() string {
	if v := strings.TrimSpace(os.Getenv(axAgentGatewayNameEnvVar)); v != "" {
		return v
	}
	return axDefaultAgentGatewayName
}

func axSandboxGatewayName() string {
	if v := strings.TrimSpace(os.Getenv(axSandboxGatewayNameEnvVar)); v != "" {
		return v
	}
	return axDefaultSandboxGatewayName
}

func axTaskCPU() string {
	if v := strings.TrimSpace(os.Getenv(axTaskCPUEnvVar)); v != "" {
		return v
	}
	return axDefaultTaskCPU
}

func axTaskMemory() string {
	if v := strings.TrimSpace(os.Getenv(axTaskMemoryEnvVar)); v != "" {
		return v
	}
	return axDefaultTaskMemory
}

func resolveAXAgentImage(agentImage string) string {
	if v := strings.TrimSpace(os.Getenv(axAgentImageEnvVar)); v != "" {
		return v
	}
	return deriveImageFromOperator(agentImage, "ax-kube-agent")
}

func resolveAXSandboxImage(agentImage string) string {
	if v := strings.TrimSpace(os.Getenv(axSandboxImageEnvVar)); v != "" {
		return v
	}
	return deriveImageFromOperator(agentImage, "ax-kube-sandbox")
}

func a2aSandboxBrokerName(agent *agentv1alpha1.PlatformAgent) string {
	return agent.Name + "-a2a-sandbox-broker"
}

func axSandboxBrokerServiceURL(agent *agentv1alpha1.PlatformAgent) string {
	return fmt.Sprintf("https://%s.%s.svc:%d%s",
		a2aSandboxBrokerName(agent), agent.Namespace, axSandboxBrokerPort, axSandboxBrokerExecPath)
}

func axKubeProxyServiceURL(agent *agentv1alpha1.PlatformAgent) string {
	return fmt.Sprintf("https://%s.%s.svc:%d",
		kubeProxyName(agent), agent.Namespace, kubeProxyPort)
}

func a2aCalloutReplicas() int32 {
	if axModeEnabled() {
		return 1
	}
	return 2
}

func a2aVerifierReplicas() int32 {
	if axModeEnabled() {
		return 1
	}
	return 2
}

func buildServiceDNSClusterTrustBundleVolume() corev1.Volume {
	return corev1.Volume{
		Name: axServiceDNSCAName,
		VolumeSource: corev1.VolumeSource{
			Projected: &corev1.ProjectedVolumeSource{
				DefaultMode: ptr.To(int32(0444)),
				Sources: []corev1.VolumeProjection{{
					ClusterTrustBundle: &corev1.ClusterTrustBundleProjection{
						SignerName: ptr.To(axServiceDNSSignerName),
						LabelSelector: &metav1.LabelSelector{
							MatchLabels: map[string]string{axCanaryingLabelKey: axCanaryingLabelLive},
						},
						Path: axServiceDNSCAPath,
					},
				}},
			},
		},
	}
}

func buildServiceDNSCertificateVolume() corev1.Volume {
	return corev1.Volume{
		Name: axServiceDNSCertVolumeName,
		VolumeSource: corev1.VolumeSource{
			Projected: &corev1.ProjectedVolumeSource{
				DefaultMode: ptr.To(int32(0444)),
				Sources: []corev1.VolumeProjection{{
					PodCertificate: &corev1.PodCertificateProjection{
						SignerName:           axServiceDNSSignerName,
						KeyType:              axPodIdentityKeyType,
						CredentialBundlePath: axServiceDNSCertBundlePath,
					},
				}},
			},
		},
	}
}

func buildPodIdentityCertificateVolume() corev1.Volume {
	return corev1.Volume{
		Name: axPodIdentityVolumeName,
		VolumeSource: corev1.VolumeSource{
			Projected: &corev1.ProjectedVolumeSource{
				DefaultMode: ptr.To(int32(0444)),
				Sources: []corev1.VolumeProjection{{
					PodCertificate: &corev1.PodCertificateProjection{
						SignerName:           axPodIdentitySignerName,
						KeyType:              axPodIdentityKeyType,
						CredentialBundlePath: axPodIdentityBundlePath,
					},
				}},
			},
		},
	}
}

func buildA2ASandboxBrokerService(agent *agentv1alpha1.PlatformAgent) *corev1.Service {
	svc := &corev1.Service{
		TypeMeta: metav1.TypeMeta{APIVersion: "v1", Kind: "Service"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      a2aSandboxBrokerName(agent),
			Namespace: agent.Namespace,
			Labels:    a2aLabels(agent, "sandbox-broker"),
		},
		Spec: corev1.ServiceSpec{
			Selector: map[string]string{"app": a2aGatewayName(agent)},
			Ports: []corev1.ServicePort{{
				Name:       "https",
				Port:       axSandboxBrokerPort,
				TargetPort: intstr.FromInt32(axSandboxBrokerPort),
				Protocol:   corev1.ProtocolTCP,
			}},
		},
	}
	withCommonLabels(svc, agent)
	return svc
}

func buildA2ASandboxBrokerNetworkPolicy(agent *agentv1alpha1.PlatformAgent) *networkingv1.NetworkPolicy {
	np := &networkingv1.NetworkPolicy{
		TypeMeta: metav1.TypeMeta{APIVersion: "networking.k8s.io/v1", Kind: "NetworkPolicy"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      a2aSandboxBrokerName(agent),
			Namespace: agent.Namespace,
			Labels:    a2aLabels(agent, "sandbox-broker-netpol"),
		},
		Spec: networkingv1.NetworkPolicySpec{
			PodSelector: metav1.LabelSelector{
				MatchLabels: map[string]string{"app": a2aGatewayName(agent)},
			},
			PolicyTypes: []networkingv1.PolicyType{networkingv1.PolicyTypeIngress},
			Ingress: []networkingv1.NetworkPolicyIngressRule{{
				From: []networkingv1.NetworkPolicyPeer{{
					NamespaceSelector: &metav1.LabelSelector{
						MatchLabels: map[string]string{labelMetadataName: ateSystemNamespace},
					},
					PodSelector: &metav1.LabelSelector{
						MatchLabels: map[string]string{"app": ateEgressAppLabel},
					},
				}},
				Ports: []networkingv1.NetworkPolicyPort{tcpPort(axSandboxBrokerPort)},
			}},
		},
	}
	withCommonLabels(np, agent)
	return np
}

func (r *PlatformAgentReconciler) reconcileAXSandboxBroker(ctx context.Context, agent *agentv1alpha1.PlatformAgent) error {
	if !axModeEnabled() {
		meta := metav1.ObjectMeta{Name: a2aSandboxBrokerName(agent), Namespace: agent.Namespace}
		for _, obj := range []client.Object{
			&corev1.Service{ObjectMeta: meta},
			&networkingv1.NetworkPolicy{ObjectMeta: meta},
		} {
			if err := r.deleteA2AObjectIfOwned(ctx, agent, obj, r.Client, nil); err != nil {
				return fmt.Errorf("failed to remove AX sandbox broker %T: %w", obj, err)
			}
		}
		return nil
	}

	for _, obj := range []client.Object{
		buildA2ASandboxBrokerService(agent),
		buildA2ASandboxBrokerNetworkPolicy(agent),
	} {
		if err := ctrl.SetControllerReference(agent, obj, r.Scheme); err != nil {
			return err
		}
		if err := r.applyManaged(ctx, agent, obj); err != nil {
			return fmt.Errorf("failed to apply AX sandbox broker %T %s/%s: %w", obj, obj.GetNamespace(), obj.GetName(), err)
		}
	}
	return nil
}

func buildAXGatewayBridgeContainer(agent *agentv1alpha1.PlatformAgent) corev1.Container {
	agentImage := resolveAgentImage(agent.Spec.Deployment, defaultPlatformAgentImage())
	return corev1.Container{
		Name:            a2aBridgeContainerName,
		Image:           a2aBridgeImage(agentImage),
		Command:         []string{"/usr/local/bin/hermes-bridge"},
		WorkingDir:      "/",
		Resources:       a2aRenderedBridgeDefaultResources(),
		SecurityContext: hardenedSecurityContext(),
		Ports: []corev1.ContainerPort{{
			Name:          axSandboxBrokerPortName,
			ContainerPort: axSandboxBrokerPort,
			Protocol:      corev1.ProtocolTCP,
		}},
		Env: []corev1.EnvVar{
			{Name: a2aBridgeNATSURLEnvVar, Value: a2aNATSClientURL(agent)},
			a2aNATSCAFileEnvVar(),
			{Name: a2aBridgeNATSUserEnvVar, Value: a2aBridgeUser},
			{Name: a2aBridgeNATSPasswordEnvVar, ValueFrom: &corev1.EnvVarSource{SecretKeyRef: &corev1.SecretKeySelector{
				LocalObjectReference: corev1.LocalObjectReference{Name: a2aCredsSecretName(agent)},
				Key:                  a2aBridgePasswordKey,
			}}},
			{Name: a2aBridgeConcurrencyEnvVar, Value: axBridgeConcurrencyValue},
			{Name: a2aBridgeDurablePrecreatedEnvVar, Value: "true"},
			{Name: a2aBridgeExecutorEnvVar, Value: a2aBridgeExecutorAX},
			{Name: axServerAddrEnvVar, Value: axServerAddr()},
			{Name: atenetRouterAddrEnvVar, Value: atenetRouterAddr()},
			{Name: "AX_ATESPACE", Value: axDefaultAtespace},
			{Name: "AX_DEFAULT_TASK", Value: agent.Name},
			{Name: "PLATFORM_AGENT_NAME", Value: agent.Name},
			{Name: axAgentGatewayNameEnvVar, Value: axAgentGatewayName()},
			{Name: axSandboxGatewayNameEnvVar, Value: axSandboxGatewayName()},
			{Name: axAgentImageEnvVar, Value: resolveAXAgentImage(agentImage)},
			{Name: axSandboxImageEnvVar, Value: resolveAXSandboxImage(agentImage)},
			{Name: axTaskCPUEnvVar, Value: axTaskCPU()},
			{Name: axTaskMemoryEnvVar, Value: axTaskMemory()},
			{Name: "AX_SANDBOX_BROKER_LISTEN", Value: axSandboxBrokerListenAddr},
			{Name: "AX_SANDBOX_BROKER_TLS_CERT_FILE", Value: axServiceDNSCertFile},
			{Name: "AX_SANDBOX_BROKER_TLS_KEY_FILE", Value: axServiceDNSCertFile},
			{Name: "AX_SANDBOX_BROKER_URL", Value: axSandboxBrokerServiceURL(agent)},
			{Name: "AX_OIDC_ISSUER_URL", Value: axOIDCIssuerURL},
			{Name: "AX_OIDC_CA_FILE", Value: axOIDCCAFile},
			{Name: "ATENET_CERT_FILE", Value: axPodIdentityCertFile},
			{Name: "ATENET_CA_FILE", Value: axOIDCCAFile},
			{Name: "BRIDGE_ACTIVITY_LISTEN", Value: "off"},
			{Name: "POD_NAMESPACE", ValueFrom: &corev1.EnvVarSource{FieldRef: &corev1.ObjectFieldSelector{
				FieldPath: "metadata.namespace",
			}}},
			{Name: a2aCapabilityRequiredEnvVar, Value: a2aCapabilityRequired()},
		},
		VolumeMounts: []corev1.VolumeMount{
			a2aNATSCABundleMount(),
			{Name: axServiceDNSCAName, MountPath: axServiceDNSCAMountPath, ReadOnly: true},
			{Name: axServiceDNSCertVolumeName, MountPath: axServiceDNSCertMountPath, ReadOnly: true},
			{Name: axPodIdentityVolumeName, MountPath: axPodIdentityMountPath, ReadOnly: true},
		},
	}
}
