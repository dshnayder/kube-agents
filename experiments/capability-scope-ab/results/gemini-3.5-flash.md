| metric | stock-shipped | fulldesc-shipped | scoped-all-shipped | stock-grown | fulldesc-grown | scoped-all-grown |
| --- | --- | --- | --- | --- | --- | --- |
| runs | 66 | 66 | 66 | 66 | 66 | 66 |
| errors | 0 | 0 | 0 | 0 | 0 | 0 |
| incomplete_runs | 0 | 0 | 0 | 0 | 0 | 0 |
| probes | 60 | 60 | 60 | 60 | 60 | 60 |
| first_skill_gold_pct | 70.0 | 76.7 | 76.7 | 65.0 | 63.3 | 73.3 |
| first_skill_acceptable_pct | 1.7 | 3.3 | 8.3 | 10.0 | 13.3 | 8.3 |
| first_skill_wrong_pct | 0.0 | 1.7 | 0.0 | 1.7 | 1.7 | 0.0 |
| no_skill_loaded_pct | 28.3 | 18.3 | 15.0 | 23.3 | 21.7 | 18.3 |
| any_gold_loaded_pct | 71.7 | 78.3 | 81.7 | 68.3 | 63.3 | 75.0 |
| control_spurious_loads | 0/6 | 0/6 | 1/6 | 0/6 | 0/6 | 0/6 |
| skill_views_per_run | 0.8 | 0.9 | 1.0 | 0.8 | 0.8 | 0.9 |
| tool_searches_per_run | 0.2 | 0.2 | 0.3 | 0.0 | 0.1 | 0.2 |
| api_calls_per_run | 7.4 | 6.6 | 7.1 | 7.0 | 5.5 | 7 |
| input_tokens_per_run_median | 258906 | 251721 | 309989.0 | 273438.5 | 249100 | 279440 |
| output_tokens_per_run_median | 1509 | 1277 | 1634.0 | 1361.5 | 1288 | 1390 |
| cache_read_share_pct_mean | 91.2 | 92.0 | 89.2 | 91.9 | 94.2 | 89.6 |
| first_call_prompt_tokens_median | 29874 | 32852 | 29815.0 | 34887.5 | 40109 | 31168 |
| wall_seconds_median | 51.9 | 56.1 | 59.9 | 58.1 | 54.8 | 64.6 |
| time_to_first_tool_call_median | 6.1 | 12.1 | 4.7 | 7.2 | 12.2 | 6.5 |
| skill_loads_outside_working_set | 9/52 | 8/59 | 12/74 | 11/54 | 11/56 | 13/74 |

### stock-shipped: first skill loaded per scenario
- control-capabilities: -, -, -
- control-list-nodes: -, -, -
- crashloop: gke-workload-troubleshooting, gke-workload-troubleshooting, gke-workload-troubleshooting
- cut-the-bill: gke-cost-optimization, -, gke-cost-analysis
- daily-backups: gke-backup-dr, -, gke-backup-dr
- fleet-behind-channel: fleet-upgrade-verification, -, -
- gitops-pr: submit-suggestion, submit-suggestion, submit-suggestion
- harden-cluster: -, gke-platform-security, gke-platform-security
- hpa: gke-workload-scaling, gke-workload-scaling, gke-workload-scaling
- https-gateway: gke-service-networking, gke-service-networking, gke-service-networking
- inspect-repo: -, -, -
- manifest-go-api: gke-manifest-generation, gke-manifest-generation, gke-manifest-generation
- namespace-cost: gke-cost-analysis, -, -
- nodepool-no-scaleup: gke-cluster-autoscaler, gke-cluster-autoscaler, gke-cluster-autoscaler
- pdb-probes: gke-reliability, -, gke-reliability
- pod-403-gcs: -, -, -
- pod-ip-exhaustion: gke-networking, gke-networking, -
- prometheus-alerts: gke-observability, gke-observability, gke-observability
- pvc-pending: gke-storage, gke-storage, gke-storage
- tpu-vbar: -, gke-ai-troubleshooting-tpu-vbar-oom, gke-ai-troubleshooting-tpu-vbar-oom
- upgrade-plan: gke-upgrades, gke-upgrades, -
- vllm-l4: gke-inference, gke-inference, gke-inference
### fulldesc-shipped: first skill loaded per scenario
- control-capabilities: -, -, -
- control-list-nodes: -, -, -
- crashloop: -, gke-workload-troubleshooting, gke-workload-troubleshooting
- cut-the-bill: gke-cost-optimization, gke-cost-analysis, gke-cost-optimization
- daily-backups: gke-backup-dr, gke-backup-dr, gke-backup-dr
- fleet-behind-channel: fleet-upgrade-verification, fleet-upgrade-verification, fleet-upgrade-verification
- gitops-pr: submit-suggestion, submit-suggestion, submit-suggestion
- harden-cluster: gke-platform-security, gke-platform-security, gke-platform-security
- hpa: gke-workload-scaling, gke-workload-scaling, -
- https-gateway: gke-service-networking, -, gke-service-networking
- inspect-repo: inspect-repository, inspect-repository, -
- manifest-go-api: gke-manifest-generation, gke-manifest-generation, gke-manifest-generation
- namespace-cost: gke-cost-analysis, gke-cost-analysis, gke-cost-analysis
- nodepool-no-scaleup: gke-cluster-autoscaler, gke-cluster-autoscaler, -
- pdb-probes: -, gke-reliability, gke-reliability
- pod-403-gcs: -, -, -
- pod-ip-exhaustion: gke-networking, gke-networking, gke-networking
- prometheus-alerts: gke-observability, gke-observability, gke-observability
- pvc-pending: -, -, gke-workload-troubleshooting
- tpu-vbar: gke-ai-troubleshooting-tpu-vbar-oom, gke-ai-troubleshooting-tpu-vbar-oom, gke-ai-troubleshooting-tpu-vbar-oom
- upgrade-plan: gke-upgrades, gke-upgrades, gke-upgrades
- vllm-l4: gke-manifest-generation, gke-inference, gke-inference
### scoped-all-shipped: first skill loaded per scenario
- control-capabilities: -, -, -
- control-list-nodes: gke-basics, -, -
- crashloop: gke-workload-troubleshooting, gke-workload-troubleshooting, gke-workload-troubleshooting
- cut-the-bill: gke-cost-analysis, gke-cost-analysis, gke-cost-analysis
- daily-backups: gke-backup-dr, gke-backup-dr, gke-backup-dr
- fleet-behind-channel: fleet-upgrade-verification, fleet-upgrade-verification, fleet-upgrade-verification
- gitops-pr: -, submit-suggestion, submit-suggestion
- harden-cluster: gke-platform-security, gke-platform-security, gke-platform-security
- hpa: gke-workload-scaling, gke-workload-scaling, gke-workload-scaling
- https-gateway: gke-service-networking, gke-service-networking, gke-service-networking
- inspect-repo: -, -, -
- manifest-go-api: gke-manifest-generation, gke-manifest-generation, gke-manifest-generation
- namespace-cost: gke-cost-analysis, gke-cost-analysis, gke-cost-analysis
- nodepool-no-scaleup: -, gke-cluster-autoscaler, gke-cluster-autoscaler
- pdb-probes: gke-reliability, gke-reliability, gke-reliability
- pod-403-gcs: -, -, -
- pod-ip-exhaustion: gke-networking, gke-networking, gke-networking
- prometheus-alerts: gke-observability, gke-observability, gke-observability
- pvc-pending: gke-workload-troubleshooting, gke-workload-troubleshooting, -
- tpu-vbar: gke-ai-troubleshooting-tpu-vbar-oom, gke-ai-troubleshooting-tpu-vbar-oom, gke-ai-troubleshooting-tpu-vbar-oom
- upgrade-plan: gke-upgrades, gke-upgrades, gke-upgrades
- vllm-l4: gke-inference, gke-inference, gke-inference
### stock-grown: first skill loaded per scenario
- control-capabilities: -, -, -
- control-list-nodes: -, -, -
- crashloop: gke-workload-troubleshooting, gke-workload-troubleshooting, gke-workload-troubleshooting
- cut-the-bill: gke-cost-analysis, gke-cost-analysis, gke-cost-optimization
- daily-backups: gke-backup-dr, gke-backup-dr, gke-backup-dr
- fleet-behind-channel: fleet-upgrade-verification, fleet-upgrade-verification, fleet-upgrade-verification
- gitops-pr: -, submit-suggestion, submit-suggestion
- harden-cluster: gke-platform-security, gke-platform-security, gke-platform-security
- hpa: -, submit-suggestion, -
- https-gateway: gke-service-networking, gke-service-networking, -
- inspect-repo: inspect-repository, -, inspect-repository
- manifest-go-api: -, -, gke-manifest-generation
- namespace-cost: gke-cost-analysis, gke-cost-analysis, gke-cost-analysis
- nodepool-no-scaleup: gke-cluster-autoscaler, -, -
- pdb-probes: -, gke-reliability, -
- pod-403-gcs: -, gke-workload-identity, gke-workload-identity
- pod-ip-exhaustion: gke-networking, gke-networking, -
- prometheus-alerts: gke-observability, gke-observability, gke-observability
- pvc-pending: gke-storage-troubleshooting, -, gke-storage-troubleshooting
- tpu-vbar: gke-ai-troubleshooting-tpu-vbar-oom, gke-ai-troubleshooting-tpu-vbar-oom, gke-ai-troubleshooting-tpu-vbar-oom
- upgrade-plan: gke-upgrades, gke-upgrades, gke-upgrades
- vllm-l4: gke-inference, gke-inference, gke-inference
### fulldesc-grown: first skill loaded per scenario
- control-capabilities: -, -, -
- control-list-nodes: -, -, -
- crashloop: -, gke-workload-troubleshooting, gke-workload-troubleshooting
- cut-the-bill: gke-cost-analysis, gke-cost-analysis, -
- daily-backups: gke-backup-dr, gke-backup-dr, gke-backup-dr
- fleet-behind-channel: fleet-upgrade-verification, -, fleet-upgrade-verification
- gitops-pr: submit-suggestion, submit-suggestion, submit-suggestion
- harden-cluster: gke-platform-security, gke-platform-security, gke-platform-security
- hpa: gke-workload-scaling, -, -
- https-gateway: gke-service-networking, gke-service-networking, gke-service-networking
- inspect-repo: inspect-repository, inspect-repository, inspect-repository
- manifest-go-api: gke-manifest-generation, gke-manifest-generation, gke-manifest-generation
- namespace-cost: gke-cost-analysis, gke-cost-analysis, gke-cost-analysis
- nodepool-no-scaleup: -, gke-cluster-autoscaler, -
- pdb-probes: gke-reliability, -, gke-reliability
- pod-403-gcs: gke-workload-identity, gke-workload-identity, gke-workload-identity
- pod-ip-exhaustion: gke-networking, -, -
- prometheus-alerts: -, -, -
- pvc-pending: gke-storage-troubleshooting, gke-storage-troubleshooting, gke-storage-troubleshooting
- tpu-vbar: gke-ai-troubleshooting-tpu-vbar-oom, gke-ai-troubleshooting-tpu-vbar-oom, gke-ai-troubleshooting-tpu-vbar-oom
- upgrade-plan: gke-upgrades, gke-upgrades, gke-upgrades
- vllm-l4: gke-inference, gke-inference, gke-manifest-generation
### scoped-all-grown: first skill loaded per scenario
- control-capabilities: -, -, -
- control-list-nodes: -, -, -
- crashloop: gke-workload-troubleshooting, gke-workload-troubleshooting, gke-workload-troubleshooting
- cut-the-bill: gke-cost-analysis, -, gke-cost-analysis
- daily-backups: gke-backup-dr, gke-backup-dr, gke-backup-dr
- fleet-behind-channel: fleet-upgrade-verification, fleet-upgrade-verification, fleet-upgrade-verification
- gitops-pr: submit-suggestion, submit-suggestion, submit-suggestion
- harden-cluster: gke-platform-security, gke-platform-security, gke-platform-security
- hpa: gke-workload-scaling, gke-workload-scaling, gke-workload-scaling
- https-gateway: -, gke-service-networking, gke-service-networking
- inspect-repo: -, -, -
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

- first_skill_gold_pct: stock-shipped 70.0% (57.5, 80.1) vs fulldesc-shipped 76.7% (64.6, 85.6); p=0.409
- any_gold_loaded_pct: stock-shipped 71.7% (59.2, 81.5) vs fulldesc-shipped 78.3% (66.4, 86.9); p=0.3991
- no_skill_loaded_pct: stock-shipped 28.3% (18.5, 40.8) vs fulldesc-shipped 18.3% (10.6, 29.9); p=0.1953

### stock-shipped vs scoped-all-shipped

- first_skill_gold_pct: stock-shipped 70.0% (57.5, 80.1) vs scoped-all-shipped 76.7% (64.6, 85.6); p=0.409
- any_gold_loaded_pct: stock-shipped 71.7% (59.2, 81.5) vs scoped-all-shipped 81.7% (70.1, 89.4); p=0.1953
- no_skill_loaded_pct: stock-shipped 28.3% (18.5, 40.8) vs scoped-all-shipped 15.0% (8.1, 26.1); p=0.0763

### fulldesc-shipped vs scoped-all-shipped

- first_skill_gold_pct: fulldesc-shipped 76.7% (64.6, 85.6) vs scoped-all-shipped 76.7% (64.6, 85.6); p=1.0
- any_gold_loaded_pct: fulldesc-shipped 78.3% (66.4, 86.9) vs scoped-all-shipped 81.7% (70.1, 89.4); p=0.6481
- no_skill_loaded_pct: fulldesc-shipped 18.3% (10.6, 29.9) vs scoped-all-shipped 15.0% (8.1, 26.1); p=0.6242

### stock-grown vs fulldesc-grown

- first_skill_gold_pct: stock-grown 65.0% (52.4, 75.8) vs fulldesc-grown 63.3% (50.7, 74.4); p=0.849
- any_gold_loaded_pct: stock-grown 68.3% (55.8, 78.7) vs fulldesc-grown 63.3% (50.7, 74.4); p=0.5636
- no_skill_loaded_pct: stock-grown 23.3% (14.4, 35.4) vs fulldesc-grown 21.7% (13.1, 33.6); p=0.827

### stock-grown vs scoped-all-grown

- first_skill_gold_pct: stock-grown 65.0% (52.4, 75.8) vs scoped-all-grown 73.3% (61.0, 82.9); p=0.323
- any_gold_loaded_pct: stock-grown 68.3% (55.8, 78.7) vs scoped-all-grown 75.0% (62.8, 84.2); p=0.4178
- no_skill_loaded_pct: stock-grown 23.3% (14.4, 35.4) vs scoped-all-grown 18.3% (10.6, 29.9); p=0.5001

### fulldesc-grown vs scoped-all-grown

- first_skill_gold_pct: fulldesc-grown 63.3% (50.7, 74.4) vs scoped-all-grown 73.3% (61.0, 82.9); p=0.239
- any_gold_loaded_pct: fulldesc-grown 63.3% (50.7, 74.4) vs scoped-all-grown 75.0% (62.8, 84.2); p=0.1664
- no_skill_loaded_pct: fulldesc-grown 21.7% (13.1, 33.6) vs scoped-all-grown 18.3% (10.6, 29.9); p=0.6481

