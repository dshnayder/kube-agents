| metric | stock-shipped | fulldesc-shipped | scoped-all-shipped | stock-grown | fulldesc-grown | scoped-all-grown |
| --- | --- | --- | --- | --- | --- | --- |
| runs | 66 | 66 | 66 | 66 | 66 | 66 |
| errors | 0 | 0 | 0 | 0 | 0 | 0 |
| incomplete_runs | 0 | 0 | 0 | 0 | 0 | 0 |
| probes | 60 | 60 | 60 | 60 | 60 | 60 |
| first_skill_gold_pct | 55.0 | 60.0 | 68.3 | 53.3 | 58.3 | 78.3 |
| first_skill_acceptable_pct | 1.7 | 0.0 | 1.7 | 0.0 | 8.3 | 5.0 |
| first_skill_wrong_pct | 1.7 | 0.0 | 3.3 | 1.7 | 3.3 | 0.0 |
| no_skill_loaded_pct | 41.7 | 40.0 | 26.7 | 45.0 | 30.0 | 16.7 |
| any_gold_loaded_pct | 56.7 | 60.0 | 71.7 | 53.3 | 61.7 | 78.3 |
| control_spurious_loads | 0/6 | 0/6 | 0/6 | 0/6 | 0/6 | 0/6 |
| skill_views_per_run | 0.7 | 0.6 | 0.8 | 0.6 | 0.9 | 0.9 |
| tool_searches_per_run | 0.1 | 0.1 | 0.1 | 0.1 | 0.2 | 0.1 |
| api_calls_per_run | 8.6 | 8.7 | 8.5 | 8.3 | 8.4 | 8.9 |
| input_tokens_per_run_median | 308948.0 | 328393.5 | 305665.5 | 318116.5 | 399373.0 | 321476.5 |
| output_tokens_per_run_median | 1222.0 | 1404.0 | 1264.0 | 1268.5 | 1652.5 | 1321.0 |
| cache_read_share_pct_mean | 91.6 | 93.6 | 87.5 | 93.3 | 93.0 | 88.0 |
| first_call_prompt_tokens_median | 28369.0 | 31322.0 | 28324.5 | 30436.0 | 38582.0 | 29661.5 |
| wall_seconds_median | 70.3 | 120.0 | 74.3 | 77.2 | 128.6 | 92.4 |
| time_to_first_tool_call_median | 8.7 | 15.5 | 7.3 | 8.4 | 17.9 | 7.5 |
| skill_loads_outside_working_set | 16/50 | 10/48 | 25/78 | 28/58 | 16/62 | 20/82 |

### stock-shipped: first skill loaded per scenario
- control-capabilities: -, -, -
- control-list-nodes: -, -, -
- crashloop: -, -, -
- cut-the-bill: gke-cost-analysis, gke-cost-optimization, gke-cost-optimization
- daily-backups: gke-backup-dr, gke-backup-dr, gke-backup-dr
- fleet-behind-channel: fleet-upgrade-verification, fleet-upgrade-verification, -
- gitops-pr: submit-suggestion, submit-suggestion, -
- harden-cluster: gke-platform-security, gke-platform-security, gke-platform-security
- hpa: -, -, submit-suggestion
- https-gateway: -, gke-service-networking, -
- inspect-repo: inspect-repository, inspect-repository, inspect-repository
- manifest-go-api: gke-manifest-generation, gke-manifest-generation, gke-manifest-generation
- namespace-cost: gke-cost-analysis, gke-cost-analysis, gke-cost-analysis
- nodepool-no-scaleup: -, -, -
- pdb-probes: -, -, gke-reliability
- pod-403-gcs: -, -, -
- pod-ip-exhaustion: -, gke-networking, -
- prometheus-alerts: -, -, -
- pvc-pending: -, -, -
- tpu-vbar: gke-ai-troubleshooting-tpu-vbar-oom, gke-ai-troubleshooting-tpu-vbar-oom, gke-ai-troubleshooting-tpu-vbar-oom
- upgrade-plan: gke-upgrades, gke-upgrades, gke-upgrades
- vllm-l4: gke-inference, gke-inference, gke-inference
### fulldesc-shipped: first skill loaded per scenario
- control-capabilities: -, -, -
- control-list-nodes: -, -, -
- crashloop: -, -, -
- cut-the-bill: -, -, gke-cost-optimization
- daily-backups: gke-backup-dr, gke-backup-dr, gke-backup-dr
- fleet-behind-channel: fleet-upgrade-verification, fleet-upgrade-verification, fleet-upgrade-verification
- gitops-pr: -, submit-suggestion, submit-suggestion
- harden-cluster: gke-platform-security, gke-platform-security, gke-platform-security
- hpa: -, gke-workload-scaling, gke-workload-scaling
- https-gateway: gke-service-networking, gke-service-networking, -
- inspect-repo: inspect-repository, inspect-repository, inspect-repository
- manifest-go-api: gke-manifest-generation, gke-manifest-generation, gke-manifest-generation
- namespace-cost: gke-cost-analysis, gke-cost-analysis, gke-cost-analysis
- nodepool-no-scaleup: -, -, -
- pdb-probes: gke-reliability, gke-reliability, -
- pod-403-gcs: -, -, -
- pod-ip-exhaustion: -, gke-networking, gke-networking
- prometheus-alerts: -, -, -
- pvc-pending: -, -, -
- tpu-vbar: gke-ai-troubleshooting-tpu-vbar-oom, gke-ai-troubleshooting-tpu-vbar-oom, gke-ai-troubleshooting-tpu-vbar-oom
- upgrade-plan: gke-upgrades, gke-upgrades, gke-upgrades
- vllm-l4: -, gke-inference, -
### scoped-all-shipped: first skill loaded per scenario
- control-capabilities: -, -, -
- control-list-nodes: -, -, -
- crashloop: -, -, gke-workload-troubleshooting
- cut-the-bill: gke-cost-analysis, gke-cost-optimization, gke-cost-optimization
- daily-backups: gke-backup-dr, gke-backup-dr, gke-backup-dr
- fleet-behind-channel: fleet-upgrade-verification, -, -
- gitops-pr: submit-suggestion, -, submit-suggestion
- harden-cluster: gke-platform-security, gke-platform-security, gke-platform-security
- hpa: gke-workload-scaling, gke-workload-scaling, submit-suggestion
- https-gateway: gke-service-networking, gke-service-networking, gke-service-networking
- inspect-repo: submit-suggestion, -, -
- manifest-go-api: gke-manifest-generation, gke-manifest-generation, gke-manifest-generation
- namespace-cost: gke-cost-analysis, gke-cost-analysis, gke-cost-analysis
- nodepool-no-scaleup: -, -, -
- pdb-probes: gke-reliability, gke-reliability, gke-reliability
- pod-403-gcs: -, -, -
- pod-ip-exhaustion: gke-networking, gke-networking, gke-networking
- prometheus-alerts: gke-observability, gke-observability, gke-observability
- pvc-pending: -, -, -
- tpu-vbar: gke-ai-troubleshooting-tpu-vbar-oom, gke-ai-troubleshooting-tpu-vbar-oom, gke-ai-troubleshooting-tpu-vbar-oom
- upgrade-plan: gke-upgrades, gke-upgrades, gke-upgrades
- vllm-l4: gke-inference, gke-inference, gke-inference
### stock-grown: first skill loaded per scenario
- control-capabilities: -, -, -
- control-list-nodes: -, -, -
- crashloop: -, -, -
- cut-the-bill: gke-cost-optimization, gke-cost-optimization, gke-cost-optimization
- daily-backups: gke-backup-dr, gke-backup-dr, gke-backup-dr
- fleet-behind-channel: -, -, -
- gitops-pr: -, submit-suggestion, submit-suggestion
- harden-cluster: gke-platform-security, gke-platform-security, gke-platform-security
- hpa: -, -, -
- https-gateway: gke-service-networking, gke-service-networking, -
- inspect-repo: inspect-repository, inspect-repository, inspect-repository
- manifest-go-api: gke-manifest-generation, gke-manifest-generation, gke-manifest-generation
- namespace-cost: gke-cost-analysis, gke-cost-analysis, gke-cost-analysis
- nodepool-no-scaleup: -, gke-autoscaler-diagnostics, -
- pdb-probes: -, -, -
- pod-403-gcs: -, -, -
- pod-ip-exhaustion: -, -, -
- prometheus-alerts: -, gke-observability, -
- pvc-pending: -, -, -
- tpu-vbar: gke-ai-troubleshooting-tpu-vbar-oom, gke-ai-troubleshooting-tpu-vbar-oom, gke-ai-troubleshooting-tpu-vbar-oom
- upgrade-plan: gke-upgrades, gke-upgrades, gke-upgrades
- vllm-l4: gke-inference, gke-inference, gke-inference
### fulldesc-grown: first skill loaded per scenario
- control-capabilities: -, -, -
- control-list-nodes: -, -, -
- crashloop: -, -, -
- cut-the-bill: gke-cost-analysis, gke-cost-optimization, -
- daily-backups: gke-backup-dr, gke-backup-dr, -
- fleet-behind-channel: fleet-upgrade-verification, fleet-upgrade-verification, fleet-upgrade-verification
- gitops-pr: submit-suggestion, submit-suggestion, -
- harden-cluster: gke-platform-security, gke-platform-security, gke-platform-security
- hpa: -, submit-suggestion, -
- https-gateway: gke-service-networking, gke-service-networking, -
- inspect-repo: inspect-repository, inspect-repository, inspect-repository
- manifest-go-api: gke-manifest-generation, gke-manifest-generation, gke-manifest-generation
- namespace-cost: gke-cost-analysis, gke-cost-analysis, gke-cost-analysis
- nodepool-no-scaleup: -, -, -
- pdb-probes: gke-reliability, gke-reliability, gke-reliability
- pod-403-gcs: cluster-agent-lifecycle, gke-workload-identity, gke-workload-identity
- pod-ip-exhaustion: -, -, gke-networking
- prometheus-alerts: gke-observability, gke-alert-configuration, gke-alert-configuration
- pvc-pending: -, -, -
- tpu-vbar: gke-ai-troubleshooting-tpu-vbar-oom, gke-ai-troubleshooting-tpu-vbar-oom, gke-ai-troubleshooting-tpu-vbar-oom
- upgrade-plan: gke-upgrades, gke-upgrades, gke-upgrades
- vllm-l4: -, gke-inference, gke-inference
### scoped-all-grown: first skill loaded per scenario
- control-capabilities: -, -, -
- control-list-nodes: -, -, -
- crashloop: gke-workload-troubleshooting, gke-workload-troubleshooting, gke-workload-troubleshooting
- cut-the-bill: gke-cost-optimization, gke-cost-optimization, gke-cost-optimization
- daily-backups: gke-backup-dr, gke-backup-dr, gke-backup-dr
- fleet-behind-channel: -, fleet-upgrade-verification, -
- gitops-pr: submit-suggestion, submit-suggestion, submit-suggestion
- harden-cluster: gke-platform-security, gke-platform-security, gke-platform-security
- hpa: gke-workload-scaling, gke-workload-scaling, gke-workload-scaling
- https-gateway: gke-service-networking, gke-service-networking, gke-service-networking
- inspect-repo: -, -, inspect-repository
- manifest-go-api: gke-manifest-generation, gke-manifest-generation, gke-manifest-generation
- namespace-cost: gke-cost-analysis, gke-cost-analysis, gke-cost-analysis
- nodepool-no-scaleup: -, -, -
- pdb-probes: gke-reliability, gke-reliability, gke-reliability
- pod-403-gcs: gke-workload-identity, gke-workload-identity, gke-workload-identity
- pod-ip-exhaustion: gke-networking, gke-networking, gke-networking
- prometheus-alerts: gke-observability, gke-observability, gke-observability
- pvc-pending: -, -, -
- tpu-vbar: gke-ai-troubleshooting-tpu-vbar-oom, gke-ai-troubleshooting-tpu-vbar-oom, gke-ai-troubleshooting-tpu-vbar-oom
- upgrade-plan: gke-upgrades, gke-upgrades, gke-upgrades
- vllm-l4: gke-inference, gke-inference, gke-inference

## Comparisons

### stock-shipped vs fulldesc-shipped

- first_skill_gold_pct: stock-shipped 55.0% (42.5, 66.9) vs fulldesc-shipped 60.0% (47.4, 71.4); p=0.5796
- any_gold_loaded_pct: stock-shipped 56.7% (44.1, 68.4) vs fulldesc-shipped 60.0% (47.4, 71.4); p=0.7111
- no_skill_loaded_pct: stock-shipped 41.7% (30.1, 54.3) vs fulldesc-shipped 40.0% (28.6, 52.6); p=0.8527

### stock-shipped vs scoped-all-shipped

- first_skill_gold_pct: stock-shipped 55.0% (42.5, 66.9) vs scoped-all-shipped 68.3% (55.8, 78.7); p=0.1331
- any_gold_loaded_pct: stock-shipped 56.7% (44.1, 68.4) vs scoped-all-shipped 71.7% (59.2, 81.5); p=0.0866
- no_skill_loaded_pct: stock-shipped 41.7% (30.1, 54.3) vs scoped-all-shipped 26.7% (17.1, 39.0); p=0.0832

### fulldesc-shipped vs scoped-all-shipped

- first_skill_gold_pct: fulldesc-shipped 60.0% (47.4, 71.4) vs scoped-all-shipped 68.3% (55.8, 78.7); p=0.3412
- any_gold_loaded_pct: fulldesc-shipped 60.0% (47.4, 71.4) vs scoped-all-shipped 71.7% (59.2, 81.5); p=0.1779
- no_skill_loaded_pct: fulldesc-shipped 40.0% (28.6, 52.6) vs scoped-all-shipped 26.7% (17.1, 39.0); p=0.1213

### stock-grown vs fulldesc-grown

- first_skill_gold_pct: stock-grown 53.3% (40.9, 65.4) vs fulldesc-grown 58.3% (45.7, 69.9); p=0.5813
- any_gold_loaded_pct: stock-grown 53.3% (40.9, 65.4) vs fulldesc-grown 61.7% (49.0, 72.9); p=0.3558
- no_skill_loaded_pct: stock-grown 45.0% (33.1, 57.5) vs fulldesc-grown 30.0% (19.9, 42.5); p=0.0897

### stock-grown vs scoped-all-grown

- first_skill_gold_pct: stock-grown 53.3% (40.9, 65.4) vs scoped-all-grown 78.3% (66.4, 86.9); p=0.0039
- any_gold_loaded_pct: stock-grown 53.3% (40.9, 65.4) vs scoped-all-grown 78.3% (66.4, 86.9); p=0.0039
- no_skill_loaded_pct: stock-grown 45.0% (33.1, 57.5) vs scoped-all-grown 16.7% (9.3, 28.0); p=0.0008

### fulldesc-grown vs scoped-all-grown

- first_skill_gold_pct: fulldesc-grown 58.3% (45.7, 69.9) vs scoped-all-grown 78.3% (66.4, 86.9); p=0.0185
- any_gold_loaded_pct: fulldesc-grown 61.7% (49.0, 72.9) vs scoped-all-grown 78.3% (66.4, 86.9); p=0.0464
- no_skill_loaded_pct: fulldesc-grown 30.0% (19.9, 42.5) vs scoped-all-grown 16.7% (9.3, 28.0); p=0.0842

