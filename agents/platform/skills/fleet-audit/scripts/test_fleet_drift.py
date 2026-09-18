#!/usr/bin/env python3
"""Tests for fleet_drift.py, the fleet-consistency-drift collector."""

import copy
import inspect
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
import fleet_drift as fd  # noqa: E402

NOW = datetime(2026, 8, 1, tzinfo=timezone.utc)


def run_of(rc: int, stdout: str = "", stderr: str = "") -> fd.Run:
    return fd.Run(["x"], rc, stdout, stderr, 0.01)


def cluster(name, project="acme", location="us-central1", autopilot=False, status="RUNNING", created="2020-01-01T00:00:00Z", labels=None, **overrides):
    doc = {
        "name": name, "_project": project, "location": location, "status": status, "createTime": created,
        "autopilot": {"enabled": autopilot},
        "resourceLabels": labels if labels is not None else {"environment": "prod"},
        "releaseChannel": {"channel": "REGULAR"},
        "shieldedNodes": {"enabled": True},
        "nodePools": [
            {
                "name": "default-pool",
                "config": {"shieldedInstanceConfig": {"enableSecureBoot": True, "enableIntegrityMonitoring": True}, "imageType": "COS_CONTAINERD"},
                "autoscaling": {"enabled": True},
            }
        ],
        "networkConfig": {"datapathProvider": "ADVANCED_DATAPATH", "enableIntraNodeVisibility": True},
        "networkPolicy": {"enabled": False},
        "privateClusterConfig": {"enablePrivateNodes": True, "enablePrivateEndpoint": True},
        "masterAuthorizedNetworksConfig": {"enabled": True, "cidrBlocks": ["10.0.0.0/8"]},
        "loggingConfig": {"componentConfig": {"enableComponents": ["SYSTEM_COMPONENTS", "WORKLOADS"]}},
        "monitoringConfig": {"componentConfig": {"enableComponents": ["SYSTEM_COMPONENTS"]}, "managedPrometheusConfig": {"enabled": True}},
        "binaryAuthorization": {"evaluationMode": "PROJECT_SINGLETON_POLICY_ENFORCE"},
        "autoscaling": {"enableNodeAutoprovisioning": True},
        "databaseEncryption": {"state": "ENCRYPTED"},
    }
    for path, value in overrides.items():
        target = doc
        keys = path.split(".")
        for key in keys[:-1]:
            target = target.setdefault(key, {})
        target[keys[-1]] = value
    return doc


def pool(name, *, secure_boot=True, integrity=True, autoscaling=True, image="COS_CONTAINERD", taints=None):
    """A node pool for the cohorts below, varying one `_pool_fraction` input
    at a time so a fleet differs on exactly the facet a test is about."""
    config = {"shieldedInstanceConfig": {"enableSecureBoot": secure_boot, "enableIntegrityMonitoring": integrity}, "imageType": image}
    if taints:
        config["taints"] = taints
    return {"name": name, "config": config, "autoscaling": {"enabled": autoscaling}}


def K(name, project="acme", location="us-central1"):
    """`fd.ckey` for a cluster built by `cluster()` above.

    The collector keys its per-cluster dicts by `(project, location, name)`
    rather than by name, because a GKE cluster name is only unique inside its
    project and this is the collector that sweeps every project.
    """
    return (project, location, name)


class DiscoverProjectsTest(unittest.TestCase):
    @staticmethod
    def _discovery_run(projects_stdout, clusters_by_project=None):
        """A `run` that answers the three argv shapes discovery issues."""
        clusters_by_project = clusters_by_project or {}

        def run(argv, **_):
            if argv[:4] == ["gcloud", "config", "get-value", "project"]:
                return run_of(0, "acme\n")
            if argv[:3] == ["gcloud", "projects", "list"]:
                return run_of(0, projects_stdout)
            if argv[:4] == ["gcloud", "container", "clusters", "list"]:
                project = argv[argv.index("--project") + 1]
                return run_of(0, json.dumps(clusters_by_project.get(project, [])))
            raise AssertionError(f"unexpected argv {argv}")

        return run

    def test_the_given_project_is_the_whole_scope(self):
        """`--project` scopes the run. It used to be a *seed*: the inventory
        scrape ran unconditionally after it, so a scoped run still fanned out
        across everything the scrape produced."""
        calls = []

        def run(argv, **_):
            calls.append(argv)
            return run_of(0)

        self.assertEqual(fd.discover_projects("acme", run=run), ["acme"])
        self.assertEqual(calls, [])

    def test_falls_back_to_active_gcloud_project(self):
        result = fd.discover_projects(None, run=self._discovery_run(""))
        self.assertEqual(result, ["acme"])

    def test_every_project_projects_list_returns_is_in_scope(self):
        result = fd.discover_projects(
            None,
            run=self._discovery_run("acme\nacme-staging\nempty-proj\n", {"acme-staging": [{"name": "c1"}]}),
        )
        self.assertEqual(result, ["acme", "acme-staging", "empty-proj"])

    def test_discovery_names_projects_and_lists_none_of_them(self):
        """Scope used to be decided by listing every candidate here and
        keeping the ones that answered with a cluster, which made the sweep's
        thread pool a cache lookup over work already done serially. A project
        holding nothing contributes no manifest entry either way."""
        calls = []

        def run(argv, **_):
            calls.append(argv)
            if argv[:4] == ["gcloud", "config", "get-value", "project"]:
                return run_of(0, "acme\n")
            if argv[:3] == ["gcloud", "projects", "list"]:
                return run_of(0, "acme\nacme-staging\n")
            raise AssertionError(f"discovery listed clusters: {argv}")

        self.assertEqual(fd.discover_projects(None, run=run), ["acme", "acme-staging"])
        self.assertEqual([a for a in calls if "clusters" in a], [])

    def test_keeps_a_project_whose_clusters_list_could_not_be_read(self):
        """`[]` and `None` are different answers: an unreadable project stays
        in scope so the manifest records the loss."""

        def run(argv, **_):
            if argv[:4] == ["gcloud", "config", "get-value", "project"]:
                return run_of(0, "acme\n")
            if argv[:3] == ["gcloud", "projects", "list"]:
                return run_of(0, "acme\ndenied-proj\n")
            return run_of(1, "", "PERMISSION_DENIED")

        self.assertEqual(fd.discover_projects(None, run=run), ["acme", "denied-proj"])

    def test_an_unlistable_fleet_falls_back_to_the_base_project(self):
        def run(argv, **_):
            if argv[:4] == ["gcloud", "config", "get-value", "project"]:
                return run_of(0, "acme\n")
            return run_of(1, "", "PERMISSION_DENIED")

        self.assertEqual(fd.discover_projects(None, run=run), ["acme"])

    def test_english_prose_is_no_longer_a_source_of_project_ids(self):
        """The scrape read `/opt/data/INVENTORY.raw.md` -- model-written prose
        with no project-ID marker in its contract -- with a regex matching any
        lowercase word of six to thirty characters, so `cluster`, `namespace`,
        `production` and `monitoring` each became a target the run issued a
        `clusters list` against."""
        self.assertFalse(hasattr(fd, "PROJECT_ID_RE"))
        self.assertFalse(hasattr(fd, "INVENTORY_PATH"))
        self.assertNotIn("read_text", inspect.signature(fd.discover_projects).parameters)


class EnumerateProjectClustersTest(unittest.TestCase):
    def test_tags_each_cluster_with_its_project(self):
        clusters_json = json.dumps([{"name": "c1"}])
        clusters, record, error = fd.enumerate_project_clusters("acme", run=lambda a: run_of(0, clusters_json))
        self.assertEqual(clusters[0]["_project"], "acme")
        self.assertIsNotNone(record)
        self.assertIsNone(error)

    def test_failed_list_returns_empty_with_no_record_and_says_why(self):
        clusters, record, error = fd.enumerate_project_clusters("acme", run=lambda a: run_of(1, "", "denied"))
        self.assertEqual(clusters, [])
        self.assertIsNone(record)
        # The error travels back so `collect_fleet` can put it in the manifest.
        # A log line alone leaves the failure nowhere a validator can read it.
        self.assertIn("denied", error)
        self.assertIn("rc=1", error)


class ClusterEligibilityTest(unittest.TestCase):
    def test_running_and_old_is_eligible(self):
        self.assertIsNone(fd.cluster_eligibility(cluster("c1"), now=NOW))

    def test_reconciling_still_votes(self):
        """A reconcile is work in progress on a cluster that is otherwise up,
        and this module compares the configuration `clusters list` returns
        rather than reading inside the cluster. Excluding it dropped a cluster
        out of every cohort for the duration of any routine change -- and a
        cohort is a majority vote, so a missing member can flip the majority it
        was meant to define."""
        self.assertIsNone(fd.cluster_eligibility(cluster("c1", status="RECONCILING"), now=NOW))

    def test_provisioning_is_ineligible(self):
        reason = fd.cluster_eligibility(cluster("c1", status="PROVISIONING"), now=NOW)
        self.assertIn("PROVISIONING", reason)

    def test_brand_new_is_ineligible(self):
        reason = fd.cluster_eligibility(cluster("c1", created="2026-07-31T23:00:00Z"), now=NOW)
        self.assertIn("under 24h", reason)


class EnvironmentOfTest(unittest.TestCase):
    def test_reads_resource_label(self):
        self.assertEqual(fd.environment_of(cluster("c1", labels={"environment": "staging"})), ("staging", "label"))

    def test_normalizes_synonyms(self):
        self.assertEqual(fd.environment_of(cluster("c1", labels={"environment": "prd"})), ("prod", "label"))

    def test_infers_from_name_when_no_label(self):
        self.assertEqual(fd.environment_of(cluster("prod-usc1", labels={})), ("prod", "inferred"))

    def test_unknown_when_neither(self):
        self.assertEqual(fd.environment_of(cluster("cluster-one", labels={}))[0], "unknown")

    def test_prefers_label_over_name(self):
        self.assertEqual(fd.environment_of(cluster("dev-box", labels={"environment": "prod"})), ("prod", "label"))


class CohortStrategyTest(unittest.TestCase):
    def test_environment_strategy_when_any_cluster_has_one(self):
        clusters = [cluster("a", labels={"environment": "prod"}), cluster("b", labels={})]
        self.assertEqual(fd.decide_cohort_strategy(clusters), "environment")

    def test_project_strategy_when_multi_project_and_no_environment(self):
        clusters = [cluster("a", project="p1", labels={}), cluster("b", project="p2", labels={})]
        self.assertEqual(fd.decide_cohort_strategy(clusters), "project")

    def test_mode_only_as_last_resort(self):
        clusters = [cluster("a", project="p1", labels={}), cluster("b", project="p1", labels={})]
        self.assertEqual(fd.decide_cohort_strategy(clusters), "mode-only")

    def test_a_sparse_name_guess_does_not_select_environment(self):
        """One `test` token in sixteen names is our inference, not the fleet's
        convention, and acting on it costs coverage rather than buying
        precision: the guessed cluster lands alone in a cohort of one and is
        compared against nothing. Cohorting by mode compares all sixteen."""
        clusters = [cluster("deploy-test", labels={})] + [cluster(f"c{i}", labels={}) for i in range(15)]
        self.assertEqual(fd.decide_cohort_strategy(clusters), "mode-only")

    def test_a_fleetwide_naming_convention_does_select_environment(self):
        """Inference earns the strategy once it is the fleet's actual naming
        convention rather than a guess about a couple of stragglers."""
        clusters = [cluster(f"prod-{i}", labels={}) for i in range(3)]
        clusters += [cluster(f"dev-{i}", labels={}) for i in range(3)]
        self.assertEqual(fd.decide_cohort_strategy(clusters), "environment")

    def test_one_real_label_settles_it_without_a_majority(self):
        """A label is the customer declaring how they organize their fleet, so
        it does not need numbers behind it the way a guess does."""
        clusters = [cluster("a", labels={"environment": "prod"})]
        clusters += [cluster(f"c{i}", labels={}) for i in range(15)]
        self.assertEqual(fd.decide_cohort_strategy(clusters), "environment")


class ComputeBaselineTest(unittest.TestCase):
    def test_no_baseline_under_the_floor(self):
        self.assertIsNone(fd.compute_baseline({"a": "X", "b": "X"}))

    def test_no_baseline_below_two_thirds(self):
        self.assertIsNone(fd.compute_baseline({"a": "X", "b": "X", "c": "Y", "d": "Y"}))

    def test_baseline_at_exactly_two_thirds(self):
        result = fd.compute_baseline({"a": "X", "b": "X", "c": "Y"})
        self.assertEqual(result[0], "X")
        self.assertAlmostEqual(result[3], 2 / 3)

    def test_unanimous_baseline(self):
        result = fd.compute_baseline({"a": "X", "b": "X", "c": "X"})
        self.assertEqual(result, ("X", 3, 3, 1.0))


class SeverityLadderTest(unittest.TestCase):
    def test_high_confidence_keeps_base_severity(self):
        sev, downgrades = fd.apply_severity_ladder("critical", 1.0, 1, False)
        self.assertEqual(sev, "critical")
        self.assertEqual(downgrades, [])

    def test_r_under_90_drops_one_level(self):
        sev, _ = fd.apply_severity_ladder("critical", 0.85, 1, False)
        self.assertEqual(sev, "major")

    def test_r_under_80_drops_two_levels_cumulative(self):
        sev, _ = fd.apply_severity_ladder("critical", 0.75, 1, False)
        self.assertEqual(sev, "minor")

    def test_three_or_more_outliers_drops_one_level(self):
        sev, _ = fd.apply_severity_ladder("critical", 1.0, 3, False)
        self.assertEqual(sev, "major")

    def test_inferred_environment_drops_one_level(self):
        sev, _ = fd.apply_severity_ladder("critical", 1.0, 1, True)
        self.assertEqual(sev, "major")

    def test_a_major_facet_at_weak_confidence_is_dropped_entirely(self):
        sev, _ = fd.apply_severity_ladder("major", 0.75, 1, False)
        self.assertIsNone(sev)

    def test_a_critical_facet_at_weak_confidence_survives_as_minor(self):
        sev, _ = fd.apply_severity_ladder("critical", 0.71, 1, False)
        self.assertEqual(sev, "minor")

    def test_downgrades_stack(self):
        sev, downgrades = fd.apply_severity_ladder("critical", 0.75, 3, True)
        self.assertIsNone(sev)  # 2 (r<0.80) + 1 (k>=3) + 1 (inferred) = 4 steps from critical
        self.assertEqual(len(downgrades), 4)


class FacetNormalizeTest(unittest.TestCase):
    """One flag / no-flag pair per facet -- the roster this stream's SOP
    validator checks against, and the reason a slug exists here at all."""

    def hit(self, slug, base_cluster, outlier_overrides):
        facet = fd.FACETS_BY_SLUG[slug]
        baseline_token = facet.normalize(base_cluster)
        outlier = copy.deepcopy(base_cluster)
        for path, value in outlier_overrides.items():
            target = outlier
            keys = path.split(".")
            for key in keys[:-1]:
                target = target.setdefault(key, {})
            target[keys[-1]] = value
        outlier_token = facet.normalize(outlier)
        return baseline_token, outlier_token, facet.should_flag(outlier_token, baseline_token)

    def test_release_channel(self):
        base, out, flagged = self.hit("release-channel", cluster("c"), {"releaseChannel.channel": "STABLE"})
        self.assertTrue(flagged)

    def test_release_channel_unenrolled_is_excluded_not_flagged(self):
        c = cluster("c", **{"releaseChannel.channel": ""})
        self.assertIsNone(fd.norm_release_channel(c))

    def test_shielded_nodes(self):
        base, out, flagged = self.hit("shielded-nodes", cluster("c"), {"shieldedNodes.enabled": False})
        self.assertTrue(flagged)

    def test_secure_boot_all_vs_none(self):
        c = cluster("c", node_pools=None)
        base, out, flagged = self.hit("secure-boot", cluster("c"), {"nodePools": [{"config": {"shieldedInstanceConfig": {"enableSecureBoot": False}}}]})
        self.assertTrue(flagged)

    def test_secure_boot_excludes_windows_pools(self):
        c = cluster(
            "c",
            nodePools=[
                {"name": "linux", "config": {"shieldedInstanceConfig": {"enableSecureBoot": True}, "imageType": "COS_CONTAINERD"}},
                {"name": "win", "config": {"shieldedInstanceConfig": {"enableSecureBoot": False}, "imageType": "WINDOWS_LTSC_CONTAINERD"}},
            ],
        )
        self.assertEqual(fd.norm_secure_boot(c), "ALL")

    def test_integrity_monitoring(self):
        base, out, flagged = self.hit("integrity-monitoring", cluster("c"), {"nodePools": [{"config": {"shieldedInstanceConfig": {"enableIntegrityMonitoring": False}}}]})
        self.assertTrue(flagged)

    # SOP 4.3 and 4.8 both state a one-directional impact ("nodes boot
    # unverified", "cannot absorb load the way its peers do") and a remediation
    # that turns the feature on. A cluster covering *more* pools than its cohort
    # is the reverse, so flagging it renders an inverted impact and a fix that
    # never converges: enabling the feature everywhere lands on ALL, which still
    # is not a NONE baseline, and the finding recurs on every subsequent run.

    def test_less_only_ranks_none_below_some_below_all(self):
        self.assertTrue(fd._flag_less_only("NONE", "SOME"))
        self.assertTrue(fd._flag_less_only("NONE", "ALL"))
        self.assertTrue(fd._flag_less_only("SOME", "ALL"))
        self.assertFalse(fd._flag_less_only("ALL", "ALL"))
        self.assertFalse(fd._flag_less_only("SOME", "SOME"))

    def test_pool_autoscaling_some_against_none_baseline_is_not_flagged(self):
        # The live case: nine peers autoscale no pool, spot-capacity-test
        # autoscales its spot pool. It absorbs load better, not worse.
        base, out, flagged = self.hit(
            "pool-autoscaling",
            cluster("c", nodePools=[{"name": "default-pool"}, {"name": "spot-pool"}]),
            {"nodePools": [{"name": "default-pool"}, {"name": "spot-pool", "autoscaling": {"enabled": True}}]},
        )
        self.assertEqual(base, "NONE")
        self.assertEqual(out, "SOME")
        self.assertFalse(flagged)

    def test_pool_autoscaling_none_against_all_baseline_is_flagged(self):
        base, out, flagged = self.hit(
            "pool-autoscaling",
            cluster("c", nodePools=[{"name": "default-pool", "autoscaling": {"enabled": True}}]),
            {"nodePools": [{"name": "default-pool"}]},
        )
        self.assertEqual((base, out), ("ALL", "NONE"))
        self.assertTrue(flagged)

    def test_secure_boot_all_against_none_baseline_is_not_flagged(self):
        base, out, flagged = self.hit(
            "secure-boot",
            cluster("c", nodePools=[{"name": "p", "config": {"imageType": "COS_CONTAINERD"}}]),
            {"nodePools": [{"name": "p", "config": {"imageType": "COS_CONTAINERD", "shieldedInstanceConfig": {"enableSecureBoot": True}}}]},
        )
        self.assertEqual((base, out), ("NONE", "ALL"))
        self.assertFalse(flagged)

    def test_node_autoprovisioning_on_against_off_baseline_is_not_flagged(self):
        base, out, flagged = self.hit(
            "node-autoprovisioning",
            cluster("c", autoscaling={"enableNodeAutoprovisioning": False}),
            {"autoscaling": {"enableNodeAutoprovisioning": True}},
        )
        self.assertEqual((base, out), ("OFF", "ON"))
        self.assertFalse(flagged)

    def test_node_autoprovisioning_off_against_on_baseline_is_flagged(self):
        base, out, flagged = self.hit(
            "node-autoprovisioning",
            cluster("c", autoscaling={"enableNodeAutoprovisioning": True}),
            {"autoscaling": {"enableNodeAutoprovisioning": False}},
        )
        self.assertEqual((base, out), ("ON", "OFF"))
        self.assertTrue(flagged)

    def test_shielded_nodes_on_against_off_baseline_is_not_flagged(self):
        base, out, flagged = self.hit("shielded-nodes", cluster("c", shieldedNodes={"enabled": False}), {"shieldedNodes.enabled": True})
        self.assertEqual((base, out), ("OFF", "ON"))
        self.assertFalse(flagged)

    # The same asymmetry on the facets whose SOP impact accuses the outlier of
    # exposure (4.5), missing telemetry (4.6), or unwrapped etcd (4.13). Each
    # offers only an enable-the-control remediation, so flagging the hardened
    # side would report the one cluster that got it right and leave "weaken it
    # to match your peers" as the only recommendation that closes the finding.

    def test_hardened_outlier_is_never_flagged_against_a_lax_majority(self):
        for slug, lax, hardened in (
            ("private-nodes", "OFF", "ON"),
            ("private-endpoint", "OFF", "ON"),
            ("authorized-networks", "OFF", "ON"),
            ("managed-prometheus", "OFF", "ON"),
            ("database-encryption", "DECRYPTED", "ENCRYPTED"),
        ):
            with self.subTest(slug=slug):
                should_flag = fd.FACETS_BY_SLUG[slug].should_flag
                self.assertFalse(should_flag(hardened, lax), "%s flagged the hardened side" % slug)
                self.assertTrue(should_flag(lax, hardened), "%s missed the degraded side" % slug)

    def test_database_encryption_decrypted_against_encrypted_majority_is_flagged(self):
        base, out, flagged = self.hit(
            "database-encryption",
            cluster("c", databaseEncryption={"state": "ENCRYPTED"}),
            {"databaseEncryption": {"state": "DECRYPTED"}},
        )
        self.assertEqual((base, out), ("ENCRYPTED", "DECRYPTED"))
        self.assertTrue(flagged)

    def test_only_neutral_facets_still_compare_both_directions(self):
        # release-channel, intra-node-visibility and datapath-provider describe
        # a difference rather than a loss, so _flag_ne is right for them and
        # wrong everywhere else. Pin the membership so a new facet has to choose.
        bidirectional = {f.slug for f in fd.FACETS if f.should_flag is fd._flag_ne}
        self.assertEqual(bidirectional, {"release-channel", "intra-node-visibility", "datapath-provider"})

    def test_network_policy_reads_both_engines_as_one_token(self):
        # Two tokens, not three. A Calico cluster and a Dataplane V2 cluster
        # agree here, so neither is ever the other's outlier and both count
        # toward the baseline an `OFF` cluster is measured against.
        dpv2 = cluster("c")
        calico = cluster("c", **{"networkConfig.datapathProvider": "LEGACY_DATAPATH", "networkPolicy.enabled": True})
        self.assertEqual(fd.norm_network_policy(dpv2), "ENFORCED")
        self.assertEqual(fd.norm_network_policy(calico), "ENFORCED")

    def test_network_policy_off_against_enforcing_majority_is_flagged(self):
        base, out, flagged = self.hit("network-policy", cluster("c"), {"networkConfig.datapathProvider": "LEGACY_DATAPATH", "networkPolicy.enabled": False})
        self.assertEqual(out, "OFF")
        self.assertTrue(flagged)

    def test_private_nodes(self):
        base, out, flagged = self.hit("private-nodes", cluster("c"), {"privateClusterConfig.enablePrivateNodes": False})
        self.assertTrue(flagged)

    def test_private_endpoint_legacy_field(self):
        base, out, flagged = self.hit("private-endpoint", cluster("c"), {"privateClusterConfig.enablePrivateEndpoint": False})
        self.assertTrue(flagged)

    def test_private_endpoint_falls_back_to_newer_field(self):
        c = cluster("c", privateClusterConfig={"enablePrivateNodes": True}, controlPlaneEndpointsConfig={"ipEndpointsConfig": {"enablePublicEndpoint": False}})
        self.assertEqual(fd.norm_private_endpoint(c), "ON")

    def test_authorized_networks_requires_nonempty_cidrs(self):
        c = cluster("c", masterAuthorizedNetworksConfig={"enabled": True, "cidrBlocks": []})
        self.assertEqual(fd.norm_authorized_networks(c), "OFF")

    def test_authorized_networks_falls_back_to_newer_field(self):
        """The two surfaces are mutually exclusive, so one read is not enough.

        `norm_private_endpoint` above already falls back this way. Authorized
        networks did not, so a cluster configured through
        `ipEndpointsConfig.authorizedNetworksConfig` normalised to OFF and drifted
        as a critical against peers holding the identical setting.
        """
        c = cluster(
            "c",
            masterAuthorizedNetworksConfig={},
            controlPlaneEndpointsConfig={
                "ipEndpointsConfig": {
                    "authorizedNetworksConfig": {
                        "enabled": True,
                        "cidrBlocks": [{"displayName": "corp", "cidrBlock": "10.0.0.0/8"}],
                    }
                }
            },
        )
        self.assertEqual(fd.norm_authorized_networks(c), "ON")

    def test_logging_components_superset_not_flagged(self):
        baseline = fd.norm_logging_components(cluster("c"))
        outlier = fd.norm_logging_components(cluster("c", loggingConfig={"componentConfig": {"enableComponents": ["SYSTEM_COMPONENTS", "WORKLOADS", "APISERVER"]}}))
        self.assertFalse(fd._flag_not_superset(outlier, baseline))

    def test_logging_components_subset_is_flagged(self):
        baseline = fd.norm_logging_components(cluster("c"))
        outlier = fd.norm_logging_components(cluster("c", loggingConfig={"componentConfig": {"enableComponents": ["WORKLOADS"]}}))
        self.assertTrue(fd._flag_not_superset(outlier, baseline))

    def test_logging_severity_major_when_missing_system_components(self):
        self.assertEqual(fd._logging_severity("WORKLOADS"), "major")

    def test_logging_severity_minor_when_system_components_present(self):
        self.assertEqual(fd._logging_severity("SYSTEM_COMPONENTS,WORKLOADS"), "minor")

    def test_monitoring_components_disjoint_is_flagged(self):
        baseline = "SYSTEM_COMPONENTS"
        outlier = "WORKLOADS"
        self.assertTrue(fd._flag_not_superset(outlier, baseline))

    def test_managed_prometheus(self):
        base, out, flagged = self.hit("managed-prometheus", cluster("c"), {"monitoringConfig.managedPrometheusConfig": {"enabled": False}})
        self.assertTrue(flagged)

    def test_binary_authorization_mode_difference_not_flagged(self):
        self.assertFalse(fd._flag_off_only("SOME_OTHER_ENABLED_MODE", "PROJECT_SINGLETON_POLICY_ENFORCE"))

    def test_binary_authorization_off_is_flagged(self):
        c = cluster("c", binaryAuthorization={"evaluationMode": "DISABLED"})
        self.assertEqual(fd.norm_binary_authorization(c), "OFF")
        self.assertTrue(fd._flag_off_only("OFF", "ON"))

    def test_binary_authorization_legacy_enabled_field(self):
        c = cluster("c", binaryAuthorization={"enabled": True})
        self.assertEqual(fd.norm_binary_authorization(c), "ON")

    def test_node_autoprovisioning(self):
        base, out, flagged = self.hit("node-autoprovisioning", cluster("c"), {"autoscaling.enableNodeAutoprovisioning": False})
        self.assertTrue(flagged)

    def test_pool_autoscaling_excludes_tainted_pools(self):
        c = cluster("c", nodePools=[{"name": "pinned", "autoscaling": {"enabled": False}, "config": {"taints": [{"key": "dedicated"}]}}])
        self.assertIsNone(fd.norm_pool_autoscaling(c))

    def test_intra_node_visibility(self):
        base, out, flagged = self.hit("intra-node-visibility", cluster("c"), {"networkConfig.enableIntraNodeVisibility": False})
        self.assertTrue(flagged)

    def test_datapath_provider(self):
        base, out, flagged = self.hit("datapath-provider", cluster("c"), {"networkConfig.datapathProvider": "LEGACY_DATAPATH"})
        self.assertTrue(flagged)

    def test_label_keys_extra_keys_not_flagged(self):
        baseline = fd.norm_label_keys(cluster("c", labels={"environment": "prod", "team": "x"}))
        outlier = fd.norm_label_keys(cluster("c", labels={"environment": "prod", "team": "x", "extra": "y"}))
        self.assertFalse(fd._flag_not_superset(outlier, baseline))

    def test_label_keys_drops_goog_prefixed(self):
        c = cluster("c", labels={"environment": "prod", "goog-gke-node-pool-provisioning-model": "x"})
        self.assertEqual(fd.norm_label_keys(c), "environment")

    def test_label_keys_missing_key_is_flagged(self):
        baseline = fd.norm_label_keys(cluster("c", labels={"environment": "prod", "team": "x"}))
        outlier = fd.norm_label_keys(cluster("c", labels={"environment": "prod"}))
        self.assertTrue(fd._flag_not_superset(outlier, baseline))

    def test_image_type_windows_pool_excluded(self):
        c = cluster("c", nodePools=[
            {"config": {"imageType": "COS_CONTAINERD"}},
            {"config": {"imageType": "WINDOWS_LTSC_CONTAINERD"}},
        ])
        self.assertEqual(fd.norm_image_type(c), "COS")

    def test_image_type_containerd_rename_is_not_a_divergence(self):
        cos = fd.norm_image_type(cluster("c", nodePools=[{"config": {"imageType": "COS"}}]))
        cos_containerd = fd.norm_image_type(cluster("c", nodePools=[{"config": {"imageType": "COS_CONTAINERD"}}]))
        self.assertEqual(cos, cos_containerd)

    def test_image_type_real_divergence_is_flagged(self):
        baseline = fd.norm_image_type(cluster("c", nodePools=[{"config": {"imageType": "COS_CONTAINERD"}}]))
        outlier = fd.norm_image_type(cluster("c", nodePools=[{"config": {"imageType": "UBUNTU_CONTAINERD"}}]))
        self.assertTrue(fd._flag_not_superset(outlier, baseline))

    def test_database_encryption(self):
        base, out, flagged = self.hit("database-encryption", cluster("c"), {"databaseEncryption.state": "DECRYPTED"})
        self.assertTrue(flagged)

    def test_database_encryption_absent_block_is_decrypted(self):
        c = cluster("c", databaseEncryption={})
        self.assertEqual(fd.norm_database_encryption(c), "DECRYPTED")


class ComputeDriftTest(unittest.TestCase):
    def cohort(self, n=4, outlier_overrides=None, **base_overrides):
        clusters = [cluster(f"c{i}", labels={"environment": "prod"}, **base_overrides) for i in range(n)]
        if outlier_overrides:
            for path, value in outlier_overrides.items():
                target = clusters[-1]
                keys = path.split(".")
                for key in keys[:-1]:
                    target = target.setdefault(key, {})
                target[keys[-1]] = value
        return clusters

    def test_a_clean_cohort_produces_no_findings(self):
        checks_run, candidates = fd.compute_drift(self.cohort(), now=NOW)
        self.assertTrue(all(v == [] for v in candidates.values()))
        self.assertIn("shielded-nodes", checks_run[K("c0")])

    def test_a_single_outlier_is_flagged(self):
        # n=20 keeps r=0.95, well clear of the confidence ladder's r<0.90
        # step, so this exercises the plain outlier path without also
        # exercising the downgrade -- that is SeverityLadderTest's job.
        clusters = self.cohort(n=20, outlier_overrides={"shieldedNodes.enabled": False})
        _, candidates = fd.compute_drift(clusters, now=NOW)
        outlier_name = clusters[-1]["name"]
        self.assertEqual(len(candidates[K(outlier_name)]), 1)
        self.assertEqual(candidates[K(outlier_name)][0]["check"], "shielded-nodes")
        self.assertEqual(candidates[K(outlier_name)][0]["severity"], "major")
        self.assertEqual(candidates[K("c0")], [])

    def test_cohort_under_the_floor_produces_nothing(self):
        clusters = [cluster("a"), cluster("b")]
        _, candidates = fd.compute_drift(clusters, now=NOW)
        self.assertEqual(candidates[K("a")], [])
        self.assertEqual(fd.compute_drift(clusters, now=NOW)[0][K("a")], [])

    def test_autopilot_and_standard_are_never_compared_together(self):
        clusters = self.cohort(n=3, outlier_overrides={"shieldedNodes.enabled": False})
        clusters.append(cluster("c-auto", autopilot=True, labels={"environment": "prod"}, **{"shieldedNodes.enabled": False}))
        _, candidates = fd.compute_drift(clusters, now=NOW)
        # the autopilot cluster is alone in its mode's cohort -- under the
        # floor, so it gets no findings regardless of its shielded-nodes value
        self.assertEqual(candidates[K("c-auto")], [])

    def test_standard_only_facets_are_never_computed_for_autopilot(self):
        clusters = [cluster(f"a{i}", autopilot=True, labels={"environment": "prod"}) for i in range(4)]
        checks_run, _ = fd.compute_drift(clusters, now=NOW)
        self.assertNotIn("secure-boot", checks_run[K("a0")])
        self.assertNotIn("image-type", checks_run[K("a0")])

    def test_datapath_provider_is_computed_but_never_flagged_on_autopilot(self):
        clusters = [cluster(f"a{i}", autopilot=True, labels={"environment": "prod"}) for i in range(3)]
        clusters.append(cluster("a-outlier", autopilot=True, labels={"environment": "prod"}, **{"networkConfig.datapathProvider": "LEGACY_DATAPATH"}))
        checks_run, candidates = fd.compute_drift(clusters, now=NOW)
        self.assertIn("datapath-provider", checks_run[K("a-outlier")])
        self.assertEqual(candidates[K("a-outlier")], [])

    def test_ineligible_cluster_gets_no_facets_compared(self):
        clusters = self.cohort(n=3)
        clusters.append(cluster("provisioning", status="PROVISIONING", labels={"environment": "prod"}))
        checks_run, candidates = fd.compute_drift(clusters, now=NOW)
        self.assertEqual(checks_run[K("provisioning")], [])
        self.assertEqual(candidates[K("provisioning")], [])

    def test_a_reconciling_cluster_is_still_compared(self):
        clusters = self.cohort(n=3)
        clusters.append(cluster("reconciling", status="RECONCILING", labels={"environment": "prod"}))
        checks_run, _ = fd.compute_drift(clusters, now=NOW)
        self.assertIn("release-channel", checks_run[K("reconciling")])

    def test_split_cluster_guard_replaces_many_findings_with_one(self):
        # n=20 keeps r=0.95 for every facet -- comfortably clear of the
        # confidence ladder, so all six overrides below survive as
        # findings and the split-cluster guard is what collapses them,
        # not a severity downgrade dropping two of the six first.
        clusters = self.cohort(n=20)
        outlier = clusters[-1]
        for facet_slug, path, value in [
            ("shielded-nodes", "shieldedNodes.enabled", False),
            ("private-nodes", "privateClusterConfig.enablePrivateNodes", False),
            ("private-endpoint", "privateClusterConfig.enablePrivateEndpoint", False),
            ("intra-node-visibility", "networkConfig.enableIntraNodeVisibility", False),
            ("managed-prometheus", "monitoringConfig.managedPrometheusConfig", {"enabled": False}),
            ("database-encryption", "databaseEncryption.state", "DECRYPTED"),
        ]:
            target = outlier
            keys = path.split(".")
            for key in keys[:-1]:
                target = target.setdefault(key, {})
            target[keys[-1]] = value
        _, candidates = fd.compute_drift(clusters, now=NOW)
        self.assertEqual(len(candidates[K(outlier["name"])]), 1)
        self.assertEqual(candidates[K(outlier["name"])][0]["check"], "uncohorted")

    def test_environment_strategy_separates_cohorts(self):
        prod = self.cohort(n=4)
        staging = [cluster(f"s{i}", labels={"environment": "staging"}, **{"shieldedNodes.enabled": False}) for i in range(4)]
        _, candidates = fd.compute_drift(prod + staging, now=NOW)
        # staging's own majority is shieldedNodes=False, so none of them are outliers there
        self.assertEqual(candidates[K("s0")], [])

    def test_baseline_at_exactly_two_thirds_still_fires_for_a_critical_facet(self):
        # r = 2/3 = 0.667 triggers both the r<0.90 and r<0.80 downgrade
        # steps -- two steps from critical (index 0) lands on minor (index
        # 2), which is exactly the SOP's own worked example: "a base-
        # critical facet at r=0.71 survives as minor."
        clusters = [
            cluster("c0", labels={"environment": "prod"}),
            cluster("c1", labels={"environment": "prod"}),
            cluster("c2", labels={"environment": "prod"}, **{"privateClusterConfig.enablePrivateNodes": False}),
        ]
        _, candidates = fd.compute_drift(clusters, now=NOW)
        self.assertEqual(len(candidates[K("c2")]), 1)
        self.assertEqual(candidates[K("c2")][0]["severity"], "minor")

    def test_baseline_at_exactly_two_thirds_drops_a_major_facet_entirely(self):
        clusters = [
            cluster("c0", labels={"environment": "prod"}),
            cluster("c1", labels={"environment": "prod"}),
            cluster("c2", labels={"environment": "prod"}, **{"shieldedNodes.enabled": False}),
        ]
        _, candidates = fd.compute_drift(clusters, now=NOW)
        self.assertEqual(candidates[K("c2")], [])


class ClusterIdentityTest(unittest.TestCase):
    """A GKE cluster name is unique inside its project, not across the fleet,
    and this is the collector that sweeps every project."""

    @staticmethod
    def _two_projects():
        fleet = []
        for proj in ("p1", "p2"):
            fleet.append(cluster("web", project=proj, labels={"team": "x"}))
            fleet += [cluster(f"{proj}-{i}", project=proj, labels={"team": "x"}) for i in range(9)]
        fleet[0]["shieldedNodes"] = {"enabled": False}  # p1/web only
        return fleet

    def test_the_same_name_in_two_projects_stays_two_clusters(self):
        fleet = self._two_projects()
        self.assertEqual(fd.decide_cohort_strategy(fleet), "project")
        checks_run, candidates = fd.compute_drift(fleet, now=NOW)
        p1, p2 = K("web", project="p1"), K("web", project="p2")
        self.assertEqual([c["check"] for c in candidates[p1]], ["shielded-nodes"])
        # Keyed by name, p2/web was handed p1/web's finding as well as its own
        # empty list, and published it under an indistinguishable Cluster/web.
        self.assertEqual(candidates[p2], [])
        # §6 rejects a duplicated `checks_run` entry, and the merge produced one
        # by concatenating both clusters' facet lists into a single value.
        self.assertEqual(len(checks_run[p1]), len(set(checks_run[p1])))
        self.assertEqual(sorted(checks_run[p1]), sorted(checks_run[p2]))


class SplitCountTest(unittest.TestCase):
    """§3.2 defines `k` as `n - m` -- how split the cohort is -- not the number
    of clusters that ended up flagged."""

    @staticmethod
    def _fleet():
        # 27 SOME, 2 ALL, 1 NONE on secure-boot. r = 27/30 = 0.90 clears both
        # consensus steps, and k = 30 - 27 = 3 lands exactly on §3.5's `k >= 3`
        # step. Only `none0` is flagged -- `_flag_less_only` stays quiet for the
        # two clusters covering more pools than the cohort does -- so a `k` read
        # off the flagged count is 1 and skips the step.
        fleet = [cluster(f"s{i}", labels={"team": "x"}, nodePools=[pool("a"), pool("b", secure_boot=False)]) for i in range(27)]
        fleet += [cluster(f"all{i}", labels={"team": "x"}, nodePools=[pool("a"), pool("b")]) for i in range(2)]
        fleet.append(cluster("none0", labels={"team": "x"}, nodePools=[pool("a", secure_boot=False), pool("b", secure_boot=False)]))
        return fleet

    def test_off_baseline_clusters_count_toward_k_even_when_unflagged(self):
        _, candidates = fd.compute_drift(self._fleet(), now=NOW)
        found = [c for c in candidates[K("none0")] if c["check"] == "secure-boot"]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["severity"], "minor")  # major, one step for k
        self.assertIn("k=3>=3", found[0]["excerpt"])

    def test_a_cluster_that_diverges_upward_is_still_not_flagged(self):
        _, candidates = fd.compute_drift(self._fleet(), now=NOW)
        self.assertEqual(candidates[K("all0")], [])


class InferredEnvironmentTest(unittest.TestCase):
    """§3.5 downgrades a finding whose cohort membership rests on an inferred
    environment -- which is only ever true under the `environment` strategy."""

    def test_a_name_token_does_not_downgrade_when_cohorts_ignore_environment(self):
        fleet = [cluster(n, labels={"team": "x"}) for n in ("alpha", "beta", "gamma", "delta", "prod-eps")]
        fleet[0]["shieldedNodes"] = {"enabled": False}
        # One name token out of five does not earn the environment strategy, so
        # no cohort key holds an environment and no membership rests on one.
        self.assertEqual(fd.decide_cohort_strategy(fleet), "mode-only")
        _, candidates = fd.compute_drift(fleet, now=NOW)
        found = candidates[K("alpha")]
        self.assertEqual([f["check"] for f in found], ["shielded-nodes"])
        self.assertEqual(found[0]["severity"], "minor")  # r=0.80<0.90 only
        self.assertNotIn("inferred environment", found[0]["excerpt"])

    def test_an_inferred_environment_still_downgrades_when_it_drew_the_cohort(self):
        fleet = [cluster(f"prod-{i}") for i in range(10)]
        for c in fleet:
            c["resourceLabels"] = {"team": "x"}
        fleet[0]["shieldedNodes"] = {"enabled": False}
        self.assertEqual(fd.decide_cohort_strategy(fleet), "environment")
        _, candidates = fd.compute_drift(fleet, now=NOW)
        found = candidates[K("prod-0")]
        self.assertEqual(found[0]["severity"], "minor")  # major, one step
        self.assertIn("inferred environment", found[0]["excerpt"])


class MissingTokensTest(unittest.TestCase):
    """§3.8's `missing:` line, so a title does not have to re-derive the set
    difference the gate already took.

    Live case, `drift-peer-std-4` on 2026-09-01: logging off entirely against a
    `SYSTEM_COMPONENTS,WORKLOADS` cohort, published as "logging component set
    missing WORKLOADS relative to its cohort" -- one of the two, reading as
    though system logging still worked.
    """

    @staticmethod
    def _cohort(outlier_logging):
        fleet = [cluster(f"peer{i}", labels={"team": "x"}) for i in range(9)]
        fleet.append(cluster("odd", labels={"team": "x"}, **{"loggingConfig.componentConfig": outlier_logging}))
        return fleet

    def _logging_finding(self, outlier_logging):
        _, candidates = fd.compute_drift(self._cohort(outlier_logging), now=NOW)
        found = [c for c in candidates[K("odd")] if c["check"] == "logging-components"]
        self.assertEqual(len(found), 1)
        return found[0]

    def test_a_cluster_with_no_logging_at_all_is_missing_the_whole_baseline(self):
        found = self._logging_finding({})
        self.assertIn("observed: NONE", found["excerpt"])
        self.assertIn("missing: SYSTEM_COMPONENTS, WORKLOADS", found["excerpt"])
        # Missing SYSTEM_COMPONENTS is the `major` leg of `_logging_severity`,
        # and r = 9/10 = 0.90 clears the `r < 0.90` step, so nothing downgrades.
        self.assertEqual(found["severity"], "major")

    def test_a_partial_set_names_only_what_it_actually_lacks(self):
        found = self._logging_finding({"enableComponents": ["SYSTEM_COMPONENTS"]})
        self.assertIn("missing: WORKLOADS", found["excerpt"])
        self.assertNotIn("SYSTEM_COMPONENTS,", found["excerpt"].split("missing: ")[1])
        self.assertEqual(found["severity"], "minor")

    def test_a_facet_that_is_not_set_valued_gets_no_missing_line(self):
        fleet = [cluster(f"peer{i}", labels={"team": "x"}) for i in range(9)]
        fleet.append(cluster("odd", labels={"team": "x"}, **{"shieldedNodes.enabled": False}))
        _, candidates = fd.compute_drift(fleet, now=NOW)
        found = [c for c in candidates[K("odd")] if c["check"] == "shielded-nodes"]
        self.assertEqual(len(found), 1)
        # `ON`/`OFF` is not a set, so "missing: ON" would assert a shape the
        # facet does not have.
        self.assertNotIn("missing:", found[0]["excerpt"])


class PeerListTest(unittest.TestCase):
    """`peers:` names the clusters holding the baseline, not every cluster that
    voted.

    Live case, `drift-peer-std-4` on 2026-09-01: the excerpt said the baseline
    held "in 9/10 clusters", then listed 10 names -- one of them
    `drift-peer-std-4` itself, whose `observed:` line directly underneath said
    it did not hold the baseline. Sorting put the outlier first as often as not,
    so the first name a reader saw in the comparison set was the cluster being
    compared.
    """

    @staticmethod
    def _peers_line(excerpt):
        return next(line for line in excerpt.splitlines() if line.startswith("peers:"))

    def _logging_excerpt(self):
        fleet = [cluster(f"peer{i}", labels={"team": "x"}) for i in range(9)]
        fleet.append(cluster("odd", labels={"team": "x"}, **{"loggingConfig.componentConfig": {}}))
        _, candidates = fd.compute_drift(fleet, now=NOW)
        found = [c for c in candidates[K("odd")] if c["check"] == "logging-components"]
        self.assertEqual(len(found), 1)
        return found[0]["excerpt"]

    def test_the_outlier_is_not_listed_among_the_peers_it_differs_from(self):
        excerpt = self._logging_excerpt()
        self.assertIn("in 9/10 clusters", excerpt)
        self.assertNotIn("odd", self._peers_line(excerpt))

    def test_the_peer_count_agrees_with_the_baseline_count_above_it(self):
        # 9 hold the baseline, 6 are printed, so the overflow is 3. Counting all
        # 10 voters printed "+4 more" beside a line claiming 9.
        self.assertIn("+3 more", self._peers_line(self._logging_excerpt()))


class PoolShapeTest(unittest.TestCase):
    """§4.8: do not flag single-pool clusters against multi-pool peers."""

    @staticmethod
    def _fleet(solo_pools):
        fleet = [cluster(f"m{i}", labels={"team": "x"}, nodePools=[pool("a"), pool("b", autoscaling=False)]) for i in range(9)]
        fleet.append(cluster("solo", labels={"team": "x"}, nodePools=solo_pools))
        return fleet

    def test_a_single_pool_cluster_is_not_flagged_against_a_some_baseline(self):
        # A one-pool cluster can only normalize to ALL or NONE, so against a
        # SOME baseline it is an outlier no change can close: turning
        # autoscaling on moves it to ALL, still not SOME.
        _, candidates = fd.compute_drift(self._fleet([pool("a", autoscaling=False)]), now=NOW)
        self.assertEqual([c for c in candidates[K("solo")] if c["check"] == "pool-autoscaling"], [])

    def test_a_multi_pool_cluster_is_still_flagged_against_the_same_baseline(self):
        fleet = self._fleet([pool("a", autoscaling=False), pool("b", autoscaling=False)])
        _, candidates = fd.compute_drift(fleet, now=NOW)
        found = [c for c in candidates[K("solo")] if c["check"] == "pool-autoscaling"]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["severity"], "minor")

    def test_the_suppression_does_not_reach_the_other_pool_fraction_facets(self):
        # §4.3's secure-boot shares the ALL/SOME/NONE scale but lists a
        # different set of suppressions, and not this one.
        fleet = [cluster(f"m{i}", labels={"team": "x"}, nodePools=[pool("a"), pool("b", secure_boot=False)]) for i in range(9)]
        fleet.append(cluster("solo", labels={"team": "x"}, nodePools=[pool("a", secure_boot=False)]))
        _, candidates = fd.compute_drift(fleet, now=NOW)
        self.assertEqual([c["check"] for c in candidates[K("solo")]], ["secure-boot"])


class EligibilityCreateTimeTest(unittest.TestCase):
    def test_a_null_create_time_is_treated_as_settled(self):
        self.assertIsNone(fd.cluster_eligibility(cluster("c", created=None), now=NOW))

    def test_an_unparseable_create_time_is_treated_as_settled(self):
        self.assertIsNone(fd.cluster_eligibility(cluster("c", created="not-a-date"), now=NOW))

    def test_a_genuinely_fresh_cluster_is_still_excluded(self):
        why = fd.cluster_eligibility(cluster("c", created="2026-07-31T18:00:00Z"), now=NOW)
        self.assertIn("under 24h", why or "")

    def test_a_null_create_time_does_not_truncate_the_manifest(self):
        """`None.replace` is an AttributeError no caller catches, and the SOP
        runs this module as `fleet_drift.py > manifest.json` -- so the shell had
        already truncated the manifest by the time the traceback printed, and
        one cluster with an odd createTime lost the whole fleet."""
        docs = [cluster(f"c{i}", labels={"team": "x"}) for i in range(3)]
        docs[1]["createTime"] = None

        def run(argv, **kwargs):
            if "list" in argv and "clusters" in argv:
                return run_of(0, json.dumps(docs))
            return run_of(0)

        manifest = fd.collect_fleet("acme", run=run, now=NOW)
        self.assertEqual(len(manifest["clusters"]), 3)
        self.assertTrue(all(c["outcome"] == "collected" for c in manifest["clusters"]))


class CollectFleetTest(unittest.TestCase):
    def test_manifest_shape(self):
        clusters_json = json.dumps([cluster(f"c{i}", labels={"environment": "prod"}) for i in range(4)])

        def run(argv, **kwargs):
            if "list" in argv and "clusters" in argv:
                return run_of(0, clusters_json)
            return run_of(0)

        manifest = fd.collect_fleet("acme", run=run, now=NOW)
        self.assertEqual(manifest["audit"], "fleet-consistency-drift")
        self.assertEqual(len(manifest["clusters"]), 4)
        self.assertTrue(all(c["outcome"] == "collected" for c in manifest["clusters"]))

    def test_a_project_that_fails_to_list_is_recorded_not_dropped(self):
        """Returning nothing made a project whose `clusters list` failed
        indistinguishable from one holding no clusters, so the manifest read
        complete and the document was held to nothing. It matters more here
        than in a per-cluster stream: drift ranks each cluster against its
        cohort, and clusters missing from the comparison silently change what
        counts as an outlier."""

        def run(argv, **kwargs):
            return run_of(1, "", "denied")

        manifest = fd.collect_fleet("acme", run=run, now=NOW)
        self.assertEqual([c["name"] for c in manifest["clusters"]], ["project/acme"])
        entry = manifest["clusters"][0]
        self.assertEqual(entry["outcome"], "gate-failed")
        self.assertIn("denied", entry["error"])
        self.assertIn("all 1 project(s) in scope failed", manifest["error"])

    def test_one_project_crashing_costs_that_project_and_no_other(self):
        """`future.result()` re-raises, and the SOP redirects this collector's
        stdout into the manifest — so an unmodelled exception on one project
        used to leave a zero-byte file and lose the whole fleet. Only a failed
        `clusters list` was modelled; a `TypeError` off an unexpected API shape
        was not."""
        clusters_json = json.dumps([cluster(f"c{i}", labels={"environment": "prod"}) for i in range(4)])

        def run(argv, **kwargs):
            if argv[:2] == ["gcloud", "config"] and "get-value" in argv:
                return run_of(0, "acme\n")
            if argv[:3] == ["gcloud", "projects", "list"]:
                return run_of(0, "acme\nboom\n")
            if "list" in argv and "clusters" in argv:
                if "boom" in argv:
                    raise TypeError("unsupported operand type(s) for /: 'str' and 'str'")
                return run_of(0, clusters_json)
            return run_of(0)

        manifest = fd.collect_fleet(run=run, now=NOW)
        by_name = {c["name"]: c for c in manifest["clusters"]}
        self.assertEqual({f"c{i}" for i in range(4)} - set(by_name), set())
        self.assertEqual(by_name["project/boom"]["outcome"], "gate-failed")
        self.assertIn("TypeError", by_name["project/boom"]["error"])

    def test_a_name_two_projects_share_is_qualified_by_project(self):
        """A GKE cluster name is unique inside its project, and the manifest is
        fleet-wide: `audit_report._vouching_clusters` keys a dict by it, so two
        `seeded-a`s used to leave one entry and one cluster nobody reported on.
        Every project in the evaluation pool carries the same three names."""
        fleets = {
            "acme": [cluster(f"seeded-{x}") for x in "abc"],
            "other": [cluster(f"seeded-{x}", project="other") for x in "abc"]
            + [cluster("only-here", project="other")],
        }
        fleets["other"][2]["shieldedNodes"] = {"enabled": False}

        def run(argv, **kwargs):
            if argv[:2] == ["gcloud", "config"] and "get-value" in argv:
                return run_of(0, "acme\n")
            if argv[:3] == ["gcloud", "projects", "list"]:
                return run_of(0, "acme\nother\n")
            if "list" in argv and "clusters" in argv:
                return run_of(0, json.dumps(fleets[argv[argv.index("--project") + 1]]))
            return run_of(0)

        manifest = fd.collect_fleet(run=run, now=NOW)
        by_name = {c["name"]: c for c in manifest["clusters"]}
        self.assertEqual(
            sorted(by_name),
            ["acme/seeded-a", "acme/seeded-b", "acme/seeded-c", "only-here",
             "other/seeded-a", "other/seeded-b", "other/seeded-c"],
        )
        # A name only one project holds stays bare: the qualified form is the
        # harder one to paste into `gcloud`, so it is paid only where it buys
        # something.
        self.assertEqual(by_name["only-here"]["project"], "other")
        found = by_name["other/seeded-c"]["candidates"]
        self.assertEqual([c["object"] for c in found], ["Cluster/other/seeded-c"])
        # The `peers:` line names the clusters holding the baseline, and an
        # unqualified `seeded-a` there points at two different clusters.
        self.assertIn("acme/seeded-a", found[0]["excerpt"])
        self.assertNotIn("peers: seeded-a", found[0]["excerpt"])

    def test_every_project_failing_to_list_is_an_error_on_the_manifest(self):
        """Each loss is already a `gate-failed` row, but a run that read no
        cluster at all is a failed run rather than a clean small fleet, and §4's
        stop rule reads the top-level key. Discovery itself succeeded here, so
        `discovery.error` is not the one that fires."""

        def run(argv, **kwargs):
            if argv[:2] == ["gcloud", "config"] and "get-value" in argv:
                return run_of(0, "acme\n")
            if argv[:3] == ["gcloud", "projects", "list"]:
                return run_of(0, "acme\nother\n")
            return run_of(1, "", "PERMISSION_DENIED")

        manifest = fd.collect_fleet(run=run, now=NOW)
        self.assertEqual(
            [c["outcome"] for c in manifest["clusters"]], ["gate-failed", "gate-failed"]
        )
        self.assertIn("all 2 project(s) in scope failed", manifest["error"])
        self.assertIn("PERMISSION_DENIED", manifest["error"])

    def test_a_project_holding_nothing_is_not_an_error(self):
        """An empty project is an answer, not a loss: discovery no longer
        filters those out, and reading two of them is a healthy run."""

        def run(argv, **kwargs):
            if argv[:2] == ["gcloud", "config"] and "get-value" in argv:
                return run_of(0, "acme\n")
            if argv[:3] == ["gcloud", "projects", "list"]:
                return run_of(0, "acme\nother\n")
            if "list" in argv and "clusters" in argv:
                return run_of(0, "[]")
            return run_of(0)

        manifest = fd.collect_fleet(run=run, now=NOW)
        self.assertEqual(manifest["clusters"], [])
        self.assertNotIn("error", manifest)

    def test_each_project_is_listed_exactly_once_by_the_sweep(self):
        """Discovery names projects and the sweep lists them, so every project
        costs one `clusters list`. Discovery used to list each candidate too,
        which billed a project whose list had failed for two."""
        clusters_json = json.dumps([cluster(f"c{i}", labels={"environment": "prod"}) for i in range(4)])
        lists = []

        def run(argv, **kwargs):
            if argv[:2] == ["gcloud", "config"] and "get-value" in argv:
                return run_of(0, "acme\n")
            if argv[:3] == ["gcloud", "projects", "list"]:
                return run_of(0, "acme\nother\n")
            if "list" in argv and "clusters" in argv:
                lists.append(argv[argv.index("--project") + 1])
                return run_of(0, clusters_json)
            return run_of(0)

        manifest = fd.collect_fleet(run=run, now=NOW)
        self.assertEqual(sorted(lists), ["acme", "other"])
        self.assertEqual(len(manifest["clusters"]), 8)
        self.assertNotIn("error", manifest)

    def test_a_failed_discovery_is_an_error_on_the_manifest_not_an_empty_fleet(self):
        """Neither the active project nor `projects list` answered: nothing was
        looked at, which is not the same manifest as a fleet with no clusters."""

        def run(argv, **kwargs):
            return run_of(1, "", "no credentials")

        manifest = fd.collect_fleet(run=run, now=NOW)
        self.assertEqual(manifest["clusters"], [])
        self.assertIn("project discovery failed", manifest["error"])
        self.assertIn("no credentials", manifest["error"])

    def test_a_stub_gcloud_that_answers_nothing_exits_nonzero_with_a_warning(self):
        """End to end through `main`, with a `gcloud` on PATH that fails every
        call: the manifest is still printed (the SOP redirects stdout into the
        file), the discovery failure is on it, the exit code is non-zero and
        stderr carries the WARNING."""
        stub = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, stub, True)
        gcloud = os.path.join(stub, "gcloud")
        with open(gcloud, "w") as fh:
            fh.write("#!/bin/sh\necho 'ERROR: (gcloud) no credentials' >&2\nexit 1\n")
        os.chmod(gcloud, 0o755)
        env = dict(os.environ, PATH=f"{stub}{os.pathsep}{os.environ.get('PATH', '')}")
        done = subprocess.run(
            [sys.executable, fd.__file__], capture_output=True, text=True, env=env, timeout=120
        )
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("WARNING", done.stderr)
        manifest = json.loads(done.stdout)
        self.assertEqual(manifest["clusters"], [])
        self.assertIn("no credentials", manifest["error"])

    def test_a_project_that_lists_cleanly_adds_no_project_entry(self):
        clusters_json = json.dumps([cluster(f"c{i}", labels={"environment": "prod"}) for i in range(4)])

        def run(argv, **kwargs):
            if "list" in argv and "clusters" in argv:
                return run_of(0, clusters_json)
            return run_of(0)

        manifest = fd.collect_fleet("acme", run=run, now=NOW)
        self.assertEqual([c for c in manifest["clusters"] if c["name"].startswith("project/")], [])

    def test_every_cluster_publishes_the_mode(self):
        """`cluster_mode` is part of the cohort key here and it silences five
        facets, so this collector knows the mode before it writes a line — and
        withheld it, leaving each stream to re-derive a fact already resolved."""
        clusters = [cluster(f"a{i}", autopilot=True, labels={"environment": "prod"}) for i in range(2)]
        clusters += [cluster(f"s{i}", labels={"environment": "prod"}) for i in range(2)]

        def run(argv, **kwargs):
            if "list" in argv and "clusters" in argv:
                return run_of(0, json.dumps(clusters))
            return run_of(0)

        manifest = fd.collect_fleet("acme", run=run, now=NOW)
        self.assertEqual(
            {c["name"]: c["autopilot"] for c in manifest["clusters"]},
            {"a0": True, "a1": True, "s0": False, "s1": False},
        )

    def test_the_project_level_entry_claims_no_mode(self):
        """A project is not a cluster. The gate-failed entry stands for a whole
        `clusters list` that never answered, so there is no mode to publish and
        a `false` there would read as a fleet of Standard clusters."""

        def run(argv, **kwargs):
            return run_of(1, "", "denied")

        entry = fd.collect_fleet("acme", run=run, now=NOW)["clusters"][0]
        self.assertEqual(entry["name"], "project/acme")
        self.assertNotIn("autopilot", entry)


class AutopilotNotApplicableTest(unittest.TestCase):
    """The five `standard_only` facets have to be declared, not just dropped.

    `compute_drift` skips them for an Autopilot cohort, which is right — each
    reads a `.nodePools[]` field or a node-management setting Google owns. But
    dropping a slug leaves it missing from `commands`, which is also how a check
    nobody ran looks, so §6 counts it as a coverage gap unless the model excuses
    it by hand.
    """

    NA = ("secure-boot", "integrity-monitoring", "node-autoprovisioning", "pool-autoscaling", "image-type")

    def manifest(self, clusters):
        clusters_json = json.dumps(clusters)

        def run(argv, **kwargs):
            if "list" in argv and "clusters" in argv:
                return run_of(0, clusters_json)
            return run_of(0)

        return fd.collect_fleet("acme", run=run, now=NOW)

    def autopilot_cohort(self, n=4):
        return [cluster(f"a{i}", autopilot=True, labels={"environment": "prod"}) for i in range(n)]

    def test_the_five_are_declared_with_a_reason(self):
        entry = self.manifest(self.autopilot_cohort())["clusters"][0]
        declared = {n["check"]: n["reason"] for n in entry["checks_not_applicable"]}
        self.assertEqual(sorted(declared), sorted(self.NA))
        for reason in declared.values():
            self.assertIn("Autopilot", reason)

    def test_none_of_the_five_is_also_claimed_as_a_check_that_ran(self):
        entry = self.manifest(self.autopilot_cohort())["clusters"][0]
        ran = {c["check"] for c in entry["commands"]}
        self.assertEqual(ran & set(self.NA), set())

    def test_a_standard_cluster_declares_nothing_and_runs_them(self):
        clusters = [cluster(f"c{i}", labels={"environment": "prod"}) for i in range(4)]
        entry = self.manifest(clusters)["clusters"][0]
        self.assertNotIn("checks_not_applicable", entry)
        self.assertLessEqual(set(self.NA), {c["check"] for c in entry["commands"]})

    def test_an_undersized_autopilot_cohort_still_declares_them(self):
        """The live fleet's shape: two Autopilot clusters in one cohort against
        a floor of three, so no facet compared and every facet slug is missing
        from `commands`. The `limitations` sentence covers the ones that could
        have run; these five could not have, cohort or no cohort, and belong
        out of the denominator rather than inside the sentence.

        `no-environment-label` is the one slug still in `commands`, because it
        is the one check that does not need a cohort -- these clusters are
        labelled, so it ran and passed."""
        entry = self.manifest(self.autopilot_cohort(n=2))["clusters"][0]
        self.assertEqual([c["check"] for c in entry["commands"]], [fd.UNLABELLED_SLUG])
        self.assertIn("no facet compared", entry["limitations"])
        self.assertEqual(sorted(n["check"] for n in entry["checks_not_applicable"]), sorted(self.NA))

    def test_datapath_provider_is_not_declared(self):
        """It carries `autopilot_excluded`, not `standard_only`: the facet is
        computed and recorded in `checks_run`, and only the flagging is
        suppressed. Declaring it too would have the manifest assert both."""
        entry = self.manifest(self.autopilot_cohort())["clusters"][0]
        self.assertNotIn("datapath-provider", [n["check"] for n in entry["checks_not_applicable"]])
        self.assertIn("datapath-provider", {c["check"] for c in entry["commands"]})

    def test_the_two_together_account_for_the_whole_roster(self):
        entry = self.manifest(self.autopilot_cohort())["clusters"][0]
        ran = {c["check"] for c in entry["commands"]}
        declared = {n["check"] for n in entry["checks_not_applicable"]}
        roster = {f.slug for f in fd.FACETS} | {fd.UNLABELLED_SLUG}
        self.assertEqual(roster - ran - declared, set())
        self.assertEqual(ran & declared, set())


class UnvotedFacetsTest(unittest.TestCase):
    """A facet a compared cluster did not vote on has to be accounted for.

    §6 reads `commands` as the roster of checks that ran and divides it by the
    full roster, so a slug in none of `commands`, `checks_not_applicable` or a
    `limitations` sentence reports the cluster `partial` on every run with
    nothing saying what is missing. `cohort_limitations` speaks only for
    clusters nothing compared at all, and a cluster compared on fifteen facets
    out of nineteen fell between the two.
    """

    POOL_FACETS = ["image-type", "integrity-monitoring", "pool-autoscaling", "secure-boot"]

    def manifest(self, clusters):
        clusters_json = json.dumps(clusters)

        def run(argv, **kwargs):
            if "list" in argv and "clusters" in argv:
                return run_of(0, clusters_json)
            return run_of(0)

        return {c["name"]: c for c in fd.collect_fleet("acme", run=run, now=NOW)["clusters"]}

    def test_a_cluster_with_no_node_pools_declares_the_pool_facets(self):
        """The Autopilot position on a Standard cluster: nothing to read, so
        `autopilot_not_applicable`'s call applies for the same slugs."""
        fleet = [cluster(f"c{i}") for i in range(4)]
        fleet[0]["nodePools"] = []
        entry = self.manifest(fleet)["c0"]
        declared = {n["check"]: n["reason"] for n in entry["checks_not_applicable"]}
        self.assertEqual(sorted(declared), self.POOL_FACETS)
        for reason in declared.values():
            self.assertIn("runs no node pools", reason)
        # Not a gap as well as a declaration, and the facet that reads a
        # cluster-level field still voted.
        self.assertNotIn("limitations", entry)
        self.assertIn("node-autoprovisioning", {c["check"] for c in entry["commands"]})

    def test_a_windows_only_cluster_is_not_an_outlier_on_image_type(self):
        """`_flag_not_superset` reads the empty set as a subset of every
        baseline, so a cluster with no Linux pool was an outlier missing every
        image type its cohort runs -- a finding whose remediation is to set an
        image type on a node pool that does not exist."""
        # Eleven, so `r` clears §3.5's 0.90 rung and the finding is not
        # dropped by the severity ladder instead of by the fix.
        fleet = [cluster(f"c{i}") for i in range(11)]
        fleet[0]["nodePools"] = [pool("win", image="WINDOWS_LTSC_CONTAINERD")]
        entry = self.manifest(fleet)["c0"]
        self.assertEqual(entry["candidates"], [])
        declared = {n["check"]: n["reason"] for n in entry["checks_not_applicable"]}
        self.assertIn("image-type", declared)
        self.assertIn("every node pool runs Windows", declared["image-type"])

    def test_a_release_channel_nobody_set_is_a_gap_not_a_declaration(self):
        """§3.1 is narrow: `checks_not_applicable` leaves the coverage
        denominator, so it is for what the cluster's *shape* rules out. A
        cluster pinned to a static version is a configuration this audit did
        not compare, which is a gap and has to count as one."""
        fleet = [cluster(f"c{i}") for i in range(4)]
        fleet[0]["releaseChannel"] = {}
        entry = self.manifest(fleet)["c0"]
        self.assertNotIn("release-channel", {c["check"] for c in entry["commands"]})
        self.assertNotIn("release-channel", {n["check"] for n in entry.get("checks_not_applicable", [])})
        self.assertIn("held no readable value on this cluster", entry["limitations"])
        self.assertIn("`release-channel`", entry["limitations"])

    def test_a_cohort_that_reached_no_baseline_says_so_for_every_member(self):
        """§3.3 wants one token on two thirds of the readable values. An evenly
        split cohort has no majority to be an outlier from, so the facet is
        uncompared for all four members and not one of them said so."""
        fleet = [cluster(f"c{i}", **{"networkConfig.enableIntraNodeVisibility": i < 2}) for i in range(4)]
        entries = self.manifest(fleet)
        for name in ("c0", "c3"):
            entry = entries[name]
            self.assertNotIn("intra-node-visibility", {c["check"] for c in entry["commands"]})
            self.assertIn("reached no baseline in cohort standard/prod", entry["limitations"])
            self.assertIn("`intra-node-visibility` (4 readable value(s), commonest on 2)", entry["limitations"])

    def test_the_three_together_account_for_the_whole_roster(self):
        fleet = [cluster(f"c{i}", **{"networkConfig.enableIntraNodeVisibility": i < 2}) for i in range(4)]
        fleet[0]["nodePools"] = []
        fleet[1]["releaseChannel"] = {}
        roster = {f.slug for f in fd.FACETS} | {fd.UNLABELLED_SLUG}
        for entry in self.manifest(fleet).values():
            ran = {c["check"] for c in entry["commands"]}
            declared = {n["check"] for n in entry.get("checks_not_applicable", [])}
            named = {slug for slug in roster if f"`{slug}`" in entry.get("limitations", "")}
            self.assertEqual(roster - ran - declared - named, set(), entry["name"])
            self.assertEqual(ran & declared, set(), entry["name"])
            self.assertEqual(ran & named, set(), entry["name"])


class CohortLimitationsTest(unittest.TestCase):
    """A cluster no facet compared has to say so.

    The live four-cluster fleet: two autopilot clusters with no environment
    label, one autopilot labelled `test`, one standard. Cohorts of 2, 1 and 1
    against a floor of 3, so every cohort abstained and not one facet was
    compared — and the manifest called all four `collected` with an empty
    `commands` list, four seconds after it started. `collected` is what tells
    the model the target needs no manual fallback, so nothing downstream had
    any way to know the comparison never happened.
    """

    def _floored_fleet(self):
        return [
            cluster("auto-a", autopilot=True),
            cluster("auto-b", autopilot=True),
            cluster("auto-test", autopilot=True, labels={"environment": "test"}),
            cluster("std-a"),
        ]

    def test_every_member_of_an_undersized_cohort_is_explained(self):
        lim = fd.cohort_limitations(self._floored_fleet(), now=NOW)
        self.assertEqual(len(lim), 4)
        self.assertIn("only 2 comparable clusters", lim[K("auto-a")])
        self.assertIn("only 2 comparable clusters", lim[K("auto-b")])
        # Singular for a one-member cohort: the sentence a lone cluster like
        # kube-agents-host gets on every run.
        self.assertIn("only 1 comparable cluster ", lim[K("auto-test")])
        self.assertIn("only 1 comparable cluster ", lim[K("std-a")])
        for text in lim.values():
            self.assertIn(f"minimum {fd.COHORT_FLOOR}", text)
            self.assertIn("no facet compared", text)

    def test_the_sentence_names_the_cohort_it_floored_out_of(self):
        lim = fd.cohort_limitations(self._floored_fleet(), now=NOW)
        self.assertIn("cohort autopilot/prod", lim[K("auto-a")])
        self.assertIn("cohort autopilot/test", lim[K("auto-test")])
        self.assertIn("cohort standard/prod", lim[K("std-a")])

    def test_the_lone_unlabelled_cluster_is_told_a_label_is_the_difference(self):
        # The live fleet's shape: fifteen of sixteen carry `environment=test`,
        # kube-agents-host carries none, so it cohorts alone under 2.3 and is
        # the one cluster drift can never compare -- on this run or any later
        # one. The floor sentence alone reads as a fleet-size quirk and gets
        # waited out; the cause is what makes it fixable.
        fleet = [cluster(f"c{i}", labels={"environment": "test"}) for i in range(3)]
        fleet.append(cluster("host", labels={}))
        lim = fd.cohort_limitations(fleet, now=NOW)
        self.assertEqual(list(lim), [K("host")])
        self.assertIn("cohort standard/unknown has only 1 comparable cluster",
                      lim[K("host")])
        self.assertIn("no environment label while 3 of 4 do", lim[K("host")])

    def _live_shape(self, host_labels):
        """adamparco-kage: 5 Autopilot and 10 Standard all `test`, plus the
        install's own Standard host cluster carrying whatever is passed."""
        fleet = [cluster(f"ap-{i}", autopilot=True, labels={"environment": "test"})
                 for i in range(5)]
        fleet += [cluster(f"std-{i}", labels={"environment": "test"}) for i in range(10)]
        fleet.append(cluster("host", labels=host_labels))
        return fleet

    def test_the_advice_names_the_only_label_that_would_reach_the_floor(self):
        # A cohort key is (mode, environment), so a label compares this cluster
        # only if COHORT_FLOOR - 1 clusters *at its own mode* already carry it.
        # On the live fleet that is `test` and nothing else.
        lim = fd.cohort_limitations(self._live_shape({}), now=NOW)
        text = lim[K("host")]
        self.assertIn("Only `environment=test` would reach the floor here", text)
        self.assertIn("10 other standard clusters carry `test`", text)
        self.assertIn("Any other value opens a new cohort of one", text)

    def test_a_label_that_describes_the_cluster_is_the_no_op_the_advice_warns_of(self):
        """The reason the sentence has to name values: three of the four labels
        an operator would reach for on an install's host cluster leave the gap
        exactly where it was, and say less about it than before."""
        for env in ("prod", "platform", "hub"):
            with self.subTest(environment=env):
                lim = fd.cohort_limitations(
                    self._live_shape({"environment": env}), now=NOW)
                text = lim[K("host")]
                self.assertIn(f"cohort standard/{env} has only 1 comparable cluster",
                              text)
                # Not `unknown` any more, so the whole cause clause drops: the
                # operator who did what the sentence asked is told strictly
                # less than they were before they did it.
                self.assertNotIn("environment label", text)
        # ... and the one value the sentence does name compares the cluster,
        # which is what makes naming it worth doing.
        self.assertEqual(
            fd.cohort_limitations(self._live_shape({"environment": "test"}), now=NOW),
            {})

    def test_a_fleet_where_no_label_would_help_says_so_instead(self):
        """Naming values must not become naming none of them silently. Two
        Standard clusters share `test`, so the third would reach the floor --
        but the lone unlabelled cluster here is Autopilot, and a cohort key is
        mode-first, so no environment value on the fleet has same-mode peers."""
        fleet = [cluster(f"std-{i}", labels={"environment": "test"}) for i in range(3)]
        fleet.append(cluster("host", autopilot=True, labels={}))
        text = fd.cohort_limitations(fleet, now=NOW)[K("host")]
        self.assertIn("No environment value on this fleet has the 2 other"
                      " autopilot clusters", text)
        self.assertNotIn("would reach the floor here", text)

    def test_two_joinable_values_are_both_named(self):
        fleet = [cluster(f"p{i}", labels={"environment": "prod"}) for i in range(2)]
        fleet += [cluster(f"t{i}", labels={"environment": "test"}) for i in range(4)]
        fleet.append(cluster("host", labels={}))
        text = fd.cohort_limitations(fleet, now=NOW)[K("host")]
        # Descending peer count, so the value that needs the least explaining
        # comes first; `prod` is floored itself at 2 but reaches 3 with this one.
        self.assertIn("Only `environment=test` or `environment=prod` would reach"
                      " the floor here", text)

    def test_a_fleet_nobody_labelled_is_not_told_to_add_a_label(self):
        # Every cluster unknown together cohorts by mode alone, so there is no
        # named cohort being kept out of and nothing to point at. Counting the
        # label source rather than the resolved environment is what keeps the
        # inferred strategy out too: those clusters carry no label either, and
        # a count of them would make the sentence false.
        fleet = [cluster(f"c{i}", labels={}) for i in range(2)]
        lim = fd.cohort_limitations(fleet, now=NOW)
        self.assertEqual(len(lim), 2)
        for text in lim.values():
            self.assertIn("no facet compared", text)
            self.assertNotIn("environment label", text)

    def test_a_cohort_that_reaches_the_floor_explains_nothing(self):
        """A compared cluster must not carry a limitation — it would read as a
        coverage gap on a cluster that was in fact fully voted on."""
        fleet = [cluster(f"c{i}", labels={"environment": "prod"}) for i in range(3)]
        self.assertEqual(fd.cohort_limitations(fleet, now=NOW), {})

    def test_an_ineligible_cluster_carries_its_eligibility_reason(self):
        fleet = [cluster(f"c{i}", labels={"environment": "prod"}) for i in range(3)]
        fleet.append(cluster("broken", labels={"environment": "prod"}, status="DEGRADED"))
        lim = fd.cohort_limitations(fleet, now=NOW)
        self.assertEqual(set(lim), {K("broken")})
        self.assertIn("status DEGRADED", lim[K("broken")])

    def test_the_manifest_carries_the_sentence(self):
        clusters_json = json.dumps(self._floored_fleet())

        def run(argv, **kwargs):
            if "list" in argv and "clusters" in argv:
                return run_of(0, clusters_json)
            return run_of(0)

        manifest = fd.collect_fleet("acme", run=run, now=NOW)
        self.assertEqual(len(manifest["clusters"]), 4)
        for entry in manifest["clusters"]:
            # `no-environment-label` is the one check that runs without a
            # cohort, so it is in `commands` even here; every facet abstained.
            self.assertEqual([c["check"] for c in entry["commands"]], [fd.UNLABELLED_SLUG])
            self.assertIn("no facet compared", entry["limitations"])

    def test_a_compared_fleet_gets_no_limitations_key_at_all(self):
        clusters_json = json.dumps(
            [cluster(f"c{i}", labels={"environment": "prod"}) for i in range(3)]
        )

        def run(argv, **kwargs):
            if "list" in argv and "clusters" in argv:
                return run_of(0, clusters_json)
            return run_of(0)

        manifest = fd.collect_fleet("acme", run=run, now=NOW)
        for entry in manifest["clusters"]:
            self.assertNotIn("limitations", entry)
            self.assertTrue(entry["commands"])

    def test_the_limitation_reaches_the_coverage_arithmetic(self):
        """End to end: the sentence has to become a coverage gap, or the run
        still reports a fleet nobody compared as a clean one."""
        import audit_report

        lim = fd.cohort_limitations(self._floored_fleet(), now=NOW)
        doc = {
            "audit": "fleet-consistency-drift",
            "scope": {
                "clusters": [
                    {
                        "name": name,
                        "location": "us-central1",
                        "project": "acme",
                        "checks_run": [],
                        "limitations": text,
                    }
                    for name, text in sorted(lim.items())
                ],
                "skipped": [],
            },
            "findings": [],
        }
        gaps = audit_report.coverage_gaps(doc)
        self.assertEqual(len(gaps), 4)
        self.assertTrue(all("no facet compared" in g for g in gaps))


class NoEnvironmentLabelTest(unittest.TestCase):
    """§4.14 — the one check that fires outside a cohort.

    Every facet is a majority vote, so §2.4's floor silences all nineteen for a
    cluster with too few peers, and §2.3 gives an unlabelled cluster no peers at
    all. The cluster whose labelling diverges most from the fleet was therefore
    the one cluster `label-keys` could never see: `cohort_limitations` said so
    in a sentence, and a sentence in `limitations` opens no pull request.
    """

    def _live_shape(self, host_labels, *, host_autopilot=False):
        """adamparco-kage: 5 Autopilot and 10 Standard all `test`, plus the
        install's own host cluster carrying whatever is passed."""
        fleet = [cluster(f"ap-{i}", autopilot=True, labels={"environment": "test"})
                 for i in range(5)]
        fleet += [cluster(f"std-{i}", labels={"environment": "test"}) for i in range(10)]
        fleet.append(cluster("host", autopilot=host_autopilot, labels=host_labels))
        return fleet

    def check(self, fleet):
        return fd.unlabelled_environment_candidates(fleet, now=NOW)

    def test_the_lone_unlabelled_cluster_gets_a_finding(self):
        _run, candidates, _na = self.check(self._live_shape({}))
        self.assertEqual(list(candidates), [K("host")])
        found = candidates[K("host")][0]
        self.assertEqual(found["check"], fd.UNLABELLED_SLUG)
        self.assertEqual(found["object"], "Cluster/host")
        self.assertEqual(found["severity"], "minor")
        self.assertEqual(found["namespace"], "")

    def test_the_finding_names_the_label_to_set_and_the_peers_that_hold_it(self):
        _run, candidates, _na = self.check(self._live_shape({}))
        excerpt = candidates[K("host")][0]["excerpt"]
        self.assertIn("Set `resourceLabels.environment` to `test`", excerpt)
        self.assertIn("10 other standard clusters carry it", excerpt)

    def test_a_fleet_that_cohorts_on_inferred_names_claims_no_labels(self):
        """§2.3 takes the `environment` strategy from a naming convention too,
        so the finding can fire on a fleet where nothing is labelled at all.
        "0 of 4 eligible clusters on this fleet do" beside a finding about the
        one cluster that does not is a sentence arguing against itself, and
        `_unlabelled_cause` already drops its own version of that clause on the
        same count."""
        fleet = [cluster(f"prod-{i}", labels={}) for i in range(3)] + [cluster("host", labels={})]
        _run, candidates, _na = self.check(fleet)
        excerpt = candidates[K("host")][0]["excerpt"]
        self.assertNotIn("0 of 4", excerpt)
        self.assertIn("nor does any of the other 3 eligible clusters", excerpt)
        # And the peers do not *carry* the label the finding asks for, either.
        self.assertIn("3 other standard clusters resolve to it from their names", excerpt)

    def test_the_finding_and_the_coverage_gap_prescribe_the_same_value(self):
        """The reason `joinable_environments` is a function. The gap sentence
        and the finding are read by the same person minutes apart, and an audit
        that prescribes a label its own gap sentence says will not work has
        told them to do nothing twice."""
        fleet = self._live_shape({})
        _run, candidates, _na = self.check(fleet)
        gap = fd.cohort_limitations(fleet, now=NOW)[K("host")]
        self.assertIn("Only `environment=test` would reach the floor here", gap)
        self.assertIn("to `test`", candidates[K("host")][0]["excerpt"])

    def test_a_labelled_cluster_runs_the_check_and_passes_it(self):
        run, candidates, na = self.check(self._live_shape({"environment": "test"}))
        self.assertEqual(candidates, {})
        self.assertEqual(na, {})
        # Every eligible cluster, not just the one that could have failed:
        # a slug missing from `checks_run` is how a check nobody ran looks.
        self.assertEqual(len(run), 16)
        self.assertEqual(run[K("host")], [fd.UNLABELLED_SLUG])

    def test_an_inferred_environment_is_not_this_defect(self):
        """A name token cohorts the cluster, on a guess §3.5 already charges a
        severity step for. `joins no cohort` would be false of it."""
        fleet = self._live_shape({})
        fleet[-1] = cluster("host-test", labels={})
        _run, candidates, _na = self.check(fleet)
        self.assertEqual(candidates, {})

    def test_unlabelled_clusters_that_reach_the_floor_together_are_compared(self):
        """Three unlabelled Standard clusters cohort as `standard/unknown` and
        compare each other, which is the coverage this finding claims is
        missing. Publishing it anyway would be false about its own impact."""
        fleet = [cluster(f"std-{i}", labels={"environment": "test"}) for i in range(4)]
        fleet += [cluster(f"bare-{i}", labels={}) for i in range(3)]
        run, candidates, _na = self.check(fleet)
        self.assertEqual(candidates, {})
        self.assertEqual(len(run), 7)

    def test_no_finding_where_no_label_would_close_the_gap(self):
        """The §3.7 rule: withhold a finding whose own remediation is a no-op.
        Here the unlabelled cluster is Autopilot and every labelled peer is
        Standard, so mode-first cohorting means no value has same-mode peers.
        The check still ran -- the gap is real, a label just is not the fix."""
        fleet = [cluster(f"std-{i}", labels={"environment": "test"}) for i in range(3)]
        fleet.append(cluster("host", autopilot=True, labels={}))
        run, candidates, na = self.check(fleet)
        self.assertEqual(candidates, {})
        self.assertEqual(na, {})
        self.assertEqual(run[K("host")], [fd.UNLABELLED_SLUG])

    def test_a_second_joinable_value_is_offered_but_not_prescribed(self):
        fleet = [cluster(f"p{i}", labels={"environment": "prod"}) for i in range(2)]
        fleet += [cluster(f"t{i}", labels={"environment": "test"}) for i in range(4)]
        fleet.append(cluster("host", labels={}))
        _run, candidates, _na = self.check(fleet)
        excerpt = candidates[K("host")][0]["excerpt"]
        self.assertIn("Set `resourceLabels.environment` to `test`", excerpt)
        self.assertIn("(`prod` would also reach it, on 2)", excerpt)

    def test_the_abstaining_count_is_the_clusters_own_roster(self):
        """An Autopilot cluster owes fourteen facets, not nineteen — the five
        `standard_only` ones are declared inapplicable elsewhere and counting
        them here would overstate what the label buys."""
        fleet = [cluster(f"ap-{i}", autopilot=True, labels={"environment": "test"})
                 for i in range(3)]
        fleet.append(cluster("host", autopilot=True, labels={}))
        _run, candidates, _na = self.check(fleet)
        standard_only = sum(1 for f in fd.FACETS if f.standard_only)
        self.assertIn(f"all {len(fd.FACETS) - standard_only} comparative checks",
                      candidates[K("host")][0]["excerpt"])
        _run, standard, _na = self.check(self._live_shape({}))
        self.assertIn(f"all {len(fd.FACETS)} comparative checks",
                      standard[K("host")][0]["excerpt"])

    def test_a_fleet_that_does_not_cohort_by_environment_declares_it_na(self):
        """Under `project` or `mode-only` no cohort key holds an environment,
        so a label changes nothing about who this cluster is compared against.
        Silence would read as a check nobody ran, on every cluster, forever."""
        fleet = [cluster(f"c{i}", project=p, labels={})
                 for p in ("acme", "other") for i in range(2)]
        run, candidates, na = self.check(fleet)
        self.assertEqual(run, {})
        self.assertEqual(candidates, {})
        self.assertEqual(len(na), 4)
        reason = next(iter(na.values()))[0]["reason"]
        self.assertEqual(next(iter(na.values()))[0]["check"], fd.UNLABELLED_SLUG)
        self.assertIn("cohorts this fleet by `project`", reason)

    def test_an_ineligible_cluster_neither_runs_it_nor_declares_it(self):
        """§1 already excluded it for a reason a label cannot fix, and its
        `limitations` string covers the whole roster."""
        fleet = self._live_shape({})
        fleet[-1] = cluster("host", labels={}, status="PROVISIONING")
        run, candidates, na = self.check(fleet)
        self.assertNotIn(K("host"), run)
        self.assertEqual(candidates, {})
        self.assertEqual(na, {})

    def test_the_finding_reaches_the_manifest(self):
        clusters_json = json.dumps(self._live_shape({}))

        def run(argv, **kwargs):
            if "list" in argv and "clusters" in argv:
                return run_of(0, clusters_json)
            return run_of(0)

        manifest = fd.collect_fleet("acme", run=run, now=NOW)
        host = next(e for e in manifest["clusters"] if e["name"] == "host")
        self.assertEqual([c["check"] for c in host["candidates"]], [fd.UNLABELLED_SLUG])
        self.assertIn(fd.UNLABELLED_SLUG, [c["check"] for c in host["commands"]])
        # The compared clusters ran it too, and none of them failed it.
        peer = next(e for e in manifest["clusters"] if e["name"] == "std-0")
        self.assertIn(fd.UNLABELLED_SLUG, [c["check"] for c in peer["commands"]])
        self.assertEqual(peer["candidates"], [])

    def test_the_slug_is_on_the_audit_report_roster(self):
        """A finding whose slug the roster does not carry is rejected at
        `finish`, so the collector would publish nothing."""
        import audit_report

        spec = audit_report.AUDITS["fleet-consistency-drift"]
        self.assertIn(fd.UNLABELLED_SLUG, spec.checks)
        self.assertEqual(len(spec.checks), len(fd.FACETS) + 1)


class ManifestComposesWithAuditReportTest(unittest.TestCase):
    def test_checks_run_copied_from_a_collected_cluster_survives_cross_check(self):
        import audit_report

        clusters = [cluster(f"c{i}", labels={"environment": "prod"}) for i in range(4)]
        clusters_json = json.dumps(clusters)

        def run(argv, **kwargs):
            if "list" in argv and "clusters" in argv:
                return run_of(0, clusters_json)
            return run_of(0)

        manifest = fd.collect_fleet("acme", run=run, now=NOW)
        data = {
            "audit": "fleet-consistency-drift",
            "scope": {
                "clusters": [
                    {"name": e["name"], "checks_run": [{"check": c["check"], "command": c["command"]} for c in e["commands"]]}
                    for e in manifest["clusters"]
                ],
                "skipped": [],
            },
        }
        audit_report.cross_check_manifest(data, manifest)  # must not raise

    def test_a_check_absent_from_the_manifest_is_rejected(self):
        import audit_report

        clusters = [cluster(f"c{i}", labels={"environment": "prod"}) for i in range(2)]  # under the floor
        clusters_json = json.dumps(clusters)

        def run(argv, **kwargs):
            if "list" in argv and "clusters" in argv:
                return run_of(0, clusters_json)
            return run_of(0)

        manifest = fd.collect_fleet("acme", run=run, now=NOW)
        data = {
            "audit": "fleet-consistency-drift",
            "scope": {"clusters": [{"name": "c0", "checks_run": [{"check": "shielded-nodes", "command": "x"}]}]},
        }
        with self.assertRaises(audit_report.ValidationError):
            audit_report.cross_check_manifest(data, manifest)


if __name__ == "__main__":
    unittest.main()
