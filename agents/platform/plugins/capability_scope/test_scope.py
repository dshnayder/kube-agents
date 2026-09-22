"""Tests for the capability_scope prototype: the ranker, the sticky working set, the control
file, and the inert-by-default contract. No model in the loop."""

import importlib
import json
import os
import pathlib
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[3]
SKILLS_DIR = REPO / "agents" / "platform" / "skills"
SCENARIOS = REPO / "bench" / "experiments" / "capability-scope-ab" / "scenarios.json"
FROZEN_CATALOGUE = HERE / "test_catalogue.json"
TOP_K = 6
# The probes whose gold skill the v1 ranker puts in its top six on the frozen catalogue. A
# regression removes one of these; an improvement adds to the list and the test is updated.
GOLDEN_TOP_K = {
    "crashloop", "hpa", "nodepool-no-scaleup", "namespace-cost", "pdb-probes", "https-gateway",
    "pod-ip-exhaustion", "upgrade-plan", "fleet-behind-channel", "daily-backups", "manifest-go-api",
    "harden-cluster", "prometheus-alerts", "tpu-vbar", "vllm-l4", "gitops-pr",
}

sys.path.insert(0, str(HERE))


def _fresh_scope(**env):
    for key in list(os.environ):
        if key.startswith("KA_"):
            del os.environ[key]
    os.environ.update(env)
    os.environ.setdefault("KA_SKILLS_DIR", str(SKILLS_DIR))
    os.environ.setdefault("KA_SCOPE_RECORD", os.path.join(tempfile.mkdtemp(), "record.jsonl"))
    if "scope" in sys.modules:
        return importlib.reload(sys.modules["scope"])
    return importlib.import_module("scope")


class Agent:
    def __init__(self, session_id="s1"):
        self.session_id = session_id


def _tool(name, desc="", params=None):
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": params or {}}}}


class InertByDefault(unittest.TestCase):
    def test_no_mode_means_no_effect_and_no_record(self):
        scope = _fresh_scope()
        record = pathlib.Path(os.environ["KA_SCOPE_RECORD"])
        self.assertIsNone(scope.mode())
        self.assertIsNone(scope.handle_pre_llm_call(session_id="s", user_message="pvc pending", turn_id="t"))
        tools = [_tool("terminal"), _tool("web_extract")]
        self.assertIs(scope.filter_tools(Agent(), tools), tools)
        scope.handle_pre_tool_call(tool_name="skill_view", args={"name": "gke-storage"}, session_id="s")
        scope.handle_post_api_request(session_id="s", usage={"prompt_tokens": 1})
        self.assertFalse(record.exists())

    def test_unknown_mode_is_inert(self):
        scope = _fresh_scope(KA_SCOPE_MODE="banana")
        self.assertIsNone(scope.mode())


class ShadowMode(unittest.TestCase):
    def test_records_but_changes_nothing(self):
        scope = _fresh_scope(KA_SCOPE_MODE="off", KA_SCOPE_N_TOOLS="0")
        self.assertIsNone(scope.handle_pre_llm_call(session_id="s", user_message="a PVC is stuck Pending", turn_id="t"))
        tools = [_tool("terminal"), _tool("web_extract", "extract a web page"), _tool("tool_search", "search tools")]
        out = scope.filter_tools(Agent("s"), tools)
        self.assertIs(out, tools)
        events = [json.loads(l) for l in pathlib.Path(os.environ["KA_SCOPE_RECORD"]).read_text().splitlines()]
        kinds = [e["event"] for e in events]
        self.assertEqual(kinds, ["turn", "tools"])
        self.assertFalse(events[1]["enforced"])
        self.assertIn("web_extract", events[1]["tools_hidden"])


class SkillsMode(unittest.TestCase):
    def test_injects_descriptions_for_ranked_skills(self):
        scope = _fresh_scope(KA_SCOPE_MODE="skills", KA_SCOPE_K_SKILLS="3")
        out = scope.handle_pre_llm_call(session_id="s", user_message="Set up daily backups of the orders namespace", turn_id="t")
        self.assertIn(scope.SKILLS_HEADER, out["context"])
        self.assertIn("gke-backup-dr", out["context"])
        self.assertLessEqual(out["context"].count("\n- "), 3)

    def test_working_set_is_sticky_across_turns(self):
        scope = _fresh_scope(KA_SCOPE_MODE="skills", KA_SCOPE_K_SKILLS="2")
        first = scope.handle_pre_llm_call(session_id="s", user_message="daily backups with thirty days retention", turn_id="1")
        second = scope.handle_pre_llm_call(session_id="s", user_message="now expose the web service over https with the gateway api", turn_id="2")
        self.assertIn("gke-backup-dr", first["context"])
        self.assertIn("gke-backup-dr", second["context"], "a skill shown last turn stays for STICKY_TURNS turns")
        self.assertIn("gke-service-networking", second["context"])


class ToolsMode(unittest.TestCase):
    def test_filters_and_carries_a_shelf(self):
        scope = _fresh_scope(KA_SCOPE_MODE="skills+tools", KA_SCOPE_N_TOOLS="1")
        scope.handle_pre_llm_call(session_id="s", user_message="extract the text of this web page", turn_id="1")
        tools = [_tool("terminal", "run a command"), _tool("tool_search", "search tools"),
                 _tool("web_extract", "extract text from a web page"), _tool("image_generate", "make an image"),
                 _tool("browser_navigate", "open a url in the browser")]
        out = scope.filter_tools(Agent("s"), tools)
        names = [scope._tool_name(t) for t in out]
        self.assertIn("terminal", names)
        self.assertIn("web_extract", names)
        self.assertNotIn("image_generate", names)
        carrier = next(t for t in out if scope._tool_name(t) == "tool_search")
        self.assertIn("image_generate", carrier["function"]["description"])
        self.assertNotIn("image_generate", tools[1]["function"]["description"], "the original array is not mutated")

    def test_a_tool_named_from_the_shelf_loads_next_turn(self):
        scope = _fresh_scope(KA_SCOPE_MODE="skills+tools", KA_SCOPE_N_TOOLS="1")
        tools = [_tool("terminal"), _tool("tool_search"), _tool("web_extract", "web page text"), _tool("image_generate", "make an image")]
        scope.handle_pre_llm_call(session_id="s", user_message="extract this web page", turn_id="1")
        scope.filter_tools(Agent("s"), tools)
        scope.handle_pre_llm_call(session_id="s", user_message="please use image_generate for a diagram", turn_id="2")
        names = [scope._tool_name(t) for t in scope.filter_tools(Agent("s"), tools)]
        self.assertIn("image_generate", names)


class Ranker(unittest.TestCase):
    def test_golden_probes(self):
        """Ranks against the frozen catalogue in test_catalogue.json, not the live skills tree, so
        a skill edit or an upstream sync cannot move this set; only a ranker change can."""
        scope = _fresh_scope(KA_SCOPE_MODE="off")
        frozen = json.loads(FROZEN_CATALOGUE.read_text())["skills"]
        scope.load_skill_catalogue = lambda: [dict(s, path="") for s in frozen]
        scenarios = json.loads(SCENARIOS.read_text())["scenarios"]
        hits = set()
        for s in scenarios:
            if not s["gold"]:
                continue
            top = [n for n, _ in scope.rank_skills(s["prompt"], TOP_K)]
            if any(g in top for g in s["gold"]):
                hits.add(s["id"])
        self.assertEqual(hits, GOLDEN_TOP_K)

    def test_stem_and_tokens(self):
        scope = _fresh_scope()
        # Pins the v1 stemmer the recorded run used, weaknesses included (see STEM_SUFFIXES).
        self.assertEqual(scope._stem("autoscaling"), scope._stem("autoscaler"))
        self.assertEqual(scope._stem("clusters"), scope._stem("cluster"))
        self.assertNotEqual(scope._stem("upgrades"), scope._stem("upgrade"), "known v1 weakness; changing it re-runs the A/B")
        self.assertEqual(scope._stem("pvcs"), "pvcs", "short tokens are not stemmed")
        self.assertNotIn("the", scope._tokens("the PVC is Pending"))

    def test_sticky_update_keeps_fresh_carries_recent_and_evicts_by_disuse(self):
        scope = _fresh_scope()
        active = {}
        self.assertEqual(scope._sticky_update(active, ["a", "b", "c"], 1, 2), ["a", "b"])
        self.assertEqual(scope._sticky_update(active, ["c"], 2, 2), ["c", "a", "b"], "fresh first, then carried")
        self.assertEqual(scope._sticky_update(active, ["d", "e", "f"], 3, 2), ["d", "e", "c", "a"], "capped at twice the budget")
        for turn in range(4, 4 + scope.STICKY_TURNS + 1):
            scope._sticky_update(active, [], turn, 2)
        self.assertEqual(active, {})


class ControlFile(unittest.TestCase):
    def test_only_ka_keys_and_environment_wins(self):
        sys.path.insert(0, str(HERE.parent))
        module = importlib.import_module("capability_scope")
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "control.env"
            path.write_text("KA_SCOPE_MODE=skills\nHERMES_MAX_ITERATIONS=8\nKA_SCOPE_K_SKILLS=4\n# comment\n")
            env = {"KA_SCOPE_MODE": "off"}
            applied = module.load_control_file(path, env)
        self.assertEqual(applied, ["KA_SCOPE_K_SKILLS"])
        self.assertEqual(env["KA_SCOPE_MODE"], "off")
        self.assertNotIn("HERMES_MAX_ITERATIONS", env)


if __name__ == "__main__":
    unittest.main()
