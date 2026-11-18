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
	"os"
	"strings"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const (
	axEnabledEnvVar        = "AX_ENABLED"
	axServerAddrEnvVar     = "AX_SERVER_ADDR"
	atenetRouterAddrEnvVar = "ATENET_ROUTER_ADDR"

	defaultAXServerAddr     = "ax-server.ax-system.svc.cluster.local:8080"
	defaultAtenetRouterAddr = "atenet-router.ate-system.svc.cluster.local:443"
	axKubeProxyServiceURL   = "https://k8s-api-proxy.ax-system.svc.cluster.local:8443"

	credentialProxyOIDCIssuerURLEnv         = "CREDENTIAL_PROXY_OIDC_ISSUER_URL"
	credentialProxyOIDCCAFileEnv            = "CREDENTIAL_PROXY_OIDC_CA_FILE"
	credentialProxyOIDCRequiredSubPrefixEnv = "CREDENTIAL_PROXY_OIDC_REQUIRED_SUB_PREFIX"
	credentialProxyOIDCUIDClaimEnv          = "CREDENTIAL_PROXY_OIDC_UID_CLAIM"

	axOIDCIssuerURL            = "https://idp.ate-system.svc"
	axOIDCRequiredSubPrefix    = "actor/"
	axOIDCUIDClaim             = `actor\.ate\.dev.actorUID`
	axServiceDNSCAName         = "servicedns-ca"
	axServiceDNSCAMountPath    = "/run/servicedns-ca"
	axServiceDNSCAPath         = "trust-bundle.pem"
	axOIDCCAFile               = axServiceDNSCAMountPath + "/" + axServiceDNSCAPath
	axServiceDNSSignerName     = "servicedns.podcert.ate.dev/identity"
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
	axDefaultGatewayName       = "kube-agents-cluster-gw"
	axBridgeConcurrencyValue   = "2"
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

func buildAXGatewayBridgeContainer(agent *agentv1alpha1.PlatformAgent) corev1.Container {
	agentImage := resolveAgentImage(agent.Spec.Deployment, defaultPlatformAgentImage())
	return corev1.Container{
		Name:            a2aBridgeContainerName,
		Image:           a2aBridgeImage(agentImage),
		Command:         []string{"/usr/local/bin/hermes-bridge"},
		WorkingDir:      "/",
		Resources:       a2aRenderedBridgeDefaultResources(),
		SecurityContext: hardenedSecurityContext(),
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
			{Name: "AX_GATEWAY_NAME", Value: axDefaultGatewayName},
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
			{Name: axPodIdentityVolumeName, MountPath: axPodIdentityMountPath, ReadOnly: true},
		},
	}
}
