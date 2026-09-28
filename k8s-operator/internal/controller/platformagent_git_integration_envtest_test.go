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
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// TestIntegrationSpellingsAreExclusiveInTheSchemaEnvtest pins the CEL rule on
// PlatformAgentIntegrationSpec against a real API server. The webhook refuses
// both spellings too, but the chart ships it off, so the schema is the refusal
// every install has; a dropped or mistyped marker would otherwise go unnoticed,
// because no unit test evaluates CEL.
func TestIntegrationSpellingsAreExclusiveInTheSchemaEnvtest(t *testing.T) {
	cl, _ := startEnvtest(t)
	ctx := context.Background()

	const namespace = "git-integration"
	if err := cl.Create(ctx, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: namespace}}); err != nil {
		t.Fatalf("creating namespace: %v", err)
	}
	newAgent := func(name string, integration agentv1alpha1.IntegrationSpec) *agentv1alpha1.PlatformAgent {
		agent := &agentv1alpha1.PlatformAgent{
			ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: namespace},
			Spec: agentv1alpha1.PlatformAgentSpec{
				Harness: &agentv1alpha1.HarnessSpec{
					ProjectID:   envtestHarnessProject,
					Location:    envtestHarnessLocation,
					ClusterName: envtestHarnessCluster,
				},
			},
		}
		agent.Spec.Integration = &agentv1alpha1.PlatformAgentIntegrationSpec{IntegrationSpec: integration}
		return agent
	}

	both := newAgent("both", agentv1alpha1.IntegrationSpec{
		Git:    &agentv1alpha1.GitSpec{Repository: "gke-labs/kube-agents"},
		GitHub: &agentv1alpha1.GitHubSpec{GitRepo: "gke-labs/kube-agents"},
	})
	err := cl.Create(ctx, both)
	if !apierrors.IsInvalid(err) || !strings.Contains(err.Error(), "set at most one of integration.git and integration.github") {
		t.Fatalf("creating a PlatformAgent with both spellings = %v, want the schema's Invalid refusal", err)
	}

	for name, integration := range map[string]agentv1alpha1.IntegrationSpec{
		"git":   {Git: &agentv1alpha1.GitSpec{Repository: "gke-labs/kube-agents"}},
		"alias": {GitHub: &agentv1alpha1.GitHubSpec{GitRepo: "gke-labs/kube-agents"}},
	} {
		if err := cl.Create(ctx, newAgent(name, integration)); err != nil {
			t.Errorf("creating a PlatformAgent with only %s = %v, want it admitted", name, err)
		}
	}
}
