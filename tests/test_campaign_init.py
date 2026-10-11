"""Offline tests with a synthetic world and a fake Gemini client (no network)."""

import copy
import gzip
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import campaign_init as ci
import world_parser


def make_world():
    return {"schema_version": 1, "metadata": {"mapName": "Test"},
            "pack": {
                "burgs": [0, {"i": 1, "name": "Karthal", "x": 10, "y": 20, "state": 1, "culture": 1, "population": 12, "capital": 1, "port": 0},
                          {"i": 2, "name": "Dunmere", "x": 30, "y": 40, "state": 2, "culture": 2, "population": 5, "port": 1}][1:],
                "states": [{"i": 0, "name": "Neutrals"},
                           {"i": 1, "name": "Aldor", "fullName": "Kingdom of Aldor", "form": "Monarchy", "capital": 1, "culture": 1, "neighbors": [2], "diplomacy": ["x", "x", "Enemy"]},
                           {"i": 2, "name": "Brevik", "fullName": "Duchy of Brevik", "form": "Duchy", "capital": 2, "culture": 2, "neighbors": [1], "diplomacy": ["x", "Enemy", "x"]}],
                "provinces": [], "cultures": [{"i": 1, "name": "Aldic", "type": "Generic"}, {"i": 2, "name": "Brevic", "type": "Naval"}],
                "religions": [{"i": 1, "name": "Sun Faith", "type": "Folk"}], "markers": [], "features": [{"i": 1, "name": "Aldor Isle", "type": "island", "land": True}],
                "routes": [], "rivers": [], "zones": []}}


def good_response():
    return {
        "message": "Welcome to the Kingdom of Aldor, where Karthal's bells toll uneasily. Who are you? Tell me your name, background and abilities.",
        "world_updates": [
            {"entity_type": "burg", "entity_id": 1, "changes": {"criminal_organization": "The Black Hand"}, "reason": "Operates in secret."},
            {"entity_type": "faction", "entity_id": "black_hand", "changes": {"name": "The Black Hand", "description": "Smugglers.", "home_burg_id": 1}, "reason": "New faction."}],
        "gamestate_updates": {
            "phase": "character_creation",
            "campaign": {"title": "The Ashen Crown", "starting_location": {"burg_id": 1}, "current_turn": 0},
            "npcs": {"vera": {"name": "Vera", "role": "Spy", "description": "Sharp.", "motivation": "Revenge.", "location_burg_id": 1, "faction": "black_hand",
                              "relationships": [{"npc_id": "orrin", "relation": "rival"}]},
                     "orrin": {"name": "Orrin", "role": "Captain", "description": "Stern.", "motivation": "Order.", "location_burg_id": 2}},
            "quests": {"missing_ledger": {"title": "Missing Ledger", "description": "Find it.", "giver_npc_id": "vera", "location_burg_id": 1,
                                          "objectives": ["Search the docks"], "related_thread_id": "succession"}}},
        "notes_updates": {
            "campaign": {"title": "The Ashen Crown", "premise": "A crown lost.", "main_conflict": "Succession crisis.", "tone": "Grim", "current_chapter": 1},
            "established_lore": [{"text": "Aldor and Brevik are at war.", "refs": {"state_ids": [1, 2]}}, {"text": "The old king died."}, {"text": "Karthal is the capital."}],
            "plot_threads": [{"id": "succession", "title": "Succession", "description": "Heirs fight.", "status": "established", "related_npc_ids": ["vera"]},
                             {"id": "betrayal", "title": "Betrayal", "description": "Orrin sells out.", "status": "planned", "planned_beats": ["Orrin defects"]}],
            "secrets": [{"text": "Vera poisoned the king.", "status": "established", "known_by": ["vera"]}],
            "event_log": []}}


class FakeClient:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def generate(self, system, turns):
        self.calls.append(list(turns))
        value = self.responses.pop(0)
        return value if isinstance(value, str) else json.dumps(value)


class InitializerTests(unittest.TestCase):
    def run_init(self, *responses, directory=None):
        world, gs, notes = make_world(), ci.initial_gamestate(), ci.initial_notes()
        client = FakeClient(*responses)
        message = ci.initialize_campaign(world, gs, notes, directory=directory, client=client)
        return world, gs, notes, message, client

    def test_success_updates_all_three_files_consistently(self):
        world, gs, notes, message, _ = self.run_init(good_response())
        self.assertIn("Aldor", message)
        self.assertEqual((gs["phase"], gs["player"], gs["campaign"]["current_turn"]), ("character_creation", None, 0))
        self.assertEqual(gs["campaign"]["starting_location"]["burg_id"], 1)
        self.assertEqual(world["pack"]["burgs"][0]["criminal_organization"], "The Black Hand")
        self.assertIn("black_hand", world["factions"])
        self.assertEqual(notes["campaign"]["title"], gs["campaign"]["title"])
        self.assertTrue(all(s["revealed"] is False for s in notes["secrets"]))
        self.assertEqual({t["status"] for t in notes["plot_threads"]}, {"established", "planned"})
        self.assertEqual(notes["event_log"][-1]["id"], f"evt_{len(notes['event_log'])}")

    def test_invalid_burg_triggers_correction_then_succeeds(self):
        bad = good_response()
        bad["gamestate_updates"]["campaign"]["starting_location"]["burg_id"] = 999
        _, gs, _, _, client = self.run_init(bad, good_response())
        self.assertEqual(len(client.calls), 2)
        self.assertIn("999", client.calls[1][-1][1])
        self.assertEqual(gs["phase"], "character_creation")

    def test_protected_and_conflicting_world_updates_rejected(self):
        world = make_world()
        idx = ci.WorldIndex(world)
        errors = []
        ci._validate_world_updates([
            {"entity_type": "burg", "entity_id": 1, "changes": {"x": 5}, "reason": "no"},
            {"entity_type": "burg", "entity_id": 1, "changes": {"name": "Other"}, "reason": "rename"},
            {"entity_type": "burg", "entity_id": 77, "changes": {"a": 1}, "reason": "missing"},
            {"entity_type": "state", "entity_id": 1, "changes": {"form": "Republic"}, "reason": "ok", "overrides_existing": True}],
            idx, world, errors)
        self.assertEqual(len(errors), 3)
        self.assertTrue(any("protected" in e for e in errors))
        self.assertTrue(any("does not exist" in e for e in errors))

    def test_player_and_resolved_threads_rejected(self):
        bad = good_response()
        bad["gamestate_updates"]["player"] = {"name": "Hero"}
        bad["notes_updates"]["plot_threads"][0]["status"] = "resolved"
        bad["notes_updates"]["secrets"][0]["revealed"] = True
        errors, built = ci.validate_and_build(bad, make_world(), ci.initial_gamestate(), ci.initial_notes())
        self.assertIsNone(built)
        self.assertEqual(len(errors), 3)

    def test_failure_leaves_inputs_untouched(self):
        world, gs, notes = make_world(), ci.initial_gamestate(), ci.initial_notes()
        before = copy.deepcopy((world, gs, notes))
        with self.assertRaises(ci.CampaignInitError):
            ci.initialize_campaign(world, gs, notes, client=FakeClient("not json", "{}", "[]"))
        self.assertEqual((world, gs, notes), before)

    def test_directory_flow_and_rollback(self):
        with TemporaryDirectory() as d:
            d = Path(d)
            ci._dump(d / "world.json", make_world(), True)
            message = ci.initialize_campaign_from_directory(d, client=FakeClient(good_response()))
            self.assertTrue(message)
            self.assertIsNone(ci.read_json(d / "gamestate.json")["player"])
            self.assertEqual(ci.read_json(d / "notes.json")["campaign"]["title"], "The Ashen Crown")
            self.assertNotIn("criminal_organization", ci.read_json(d / "world.base.json")["pack"]["burgs"][0])
            with self.assertRaises(ci.CampaignInitError):  # already initialized
                ci.initialize_campaign_from_directory(d, client=FakeClient(good_response()))
            before = {n: (d / n).read_bytes() for n in ("world.json", "gamestate.json", "notes.json")}
            with self.assertRaises(TypeError):  # unserializable data aborts before any replace
                ci.save_all(d, {"world.json": ({"a": 1}, True), "gamestate.json": ({"bad": object()}, False)})
            self.assertEqual(before, {n: (d / n).read_bytes() for n in before})
            self.assertEqual([p.name for p in d.glob(".*")], [])

    def test_stage_diagnostics_report_timings_without_campaign_contents(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)
            ci._dump(path / "world.json", make_world(), True)
            messages = []
            introduction = ci.initialize_campaign_from_directory(
                path, client=FakeClient(good_response()), progress=messages.append,
                cancel_event=Event())
            self.assertIn("Aldor", introduction)
            for stage in ("input files loaded", "prompt prepared", "API stage ended",
                          "response parsed", "validation stage ended", "persistence stage ended"):
                self.assertTrue(any(stage in message for message in messages), stage)
            self.assertNotIn(introduction, " ".join(messages))
            self.assertTrue((path / "gamestate.json").exists())
            self.assertTrue((path / "notes.json").exists())

    def test_cancelled_campaign_does_not_write_files(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)
            ci._dump(path / "world.json", make_world(), True)
            cancelled = Event()
            cancelled.set()
            with self.assertRaisesRegex(ci.CampaignInitError, "cancelled"):
                ci.initialize_campaign_from_directory(
                    path, client=FakeClient(good_response()), cancel_event=cancelled)
            self.assertFalse((path / "gamestate.json").exists())

    def test_gemini_request_timeout_and_retries_are_bounded(self):
        client = ci.GeminiClient(api_key="test-only-key", timeout_seconds=1,
                                 max_request_attempts=2)
        original_client = client._client
        try:
            options = original_client._api_client._http_options
            self.assertEqual(options.timeout, 1000)
            self.assertEqual(options.retry_options.attempts, 1)
            calls = []

            def fail_request(**kwargs):
                calls.append(1)
                raise TimeoutError("simulated timeout")

            client._client = SimpleNamespace(models=SimpleNamespace(generate_content=fail_request))
            with patch("campaign_init.time.sleep") as sleep:
                with self.assertRaisesRegex(ci.CampaignInitError, "request limit"):
                    client.generate("test system", [("user", "test prompt")])
            self.assertEqual(len(calls), 2)
            sleep.assert_called_once_with(2)
        finally:
            original_client.close()

    def test_http_400_reports_sanitized_reason_without_retry(self):
        from google.genai import errors

        key = "test-only-secret-key"
        private = "WORLD BRIEF: private campaign data"
        provider_message = ("Invalid response mime type application/json; key=" + key
                            + "; " + private)
        client = ci.GeminiClient(api_key=key, max_request_attempts=2)
        original_client = client._client
        calls, progress = [], []

        def reject_request(**kwargs):
            calls.append(1)
            raise errors.ClientError(400, {"error": {"message": provider_message,
                                                      "status": "INVALID_ARGUMENT"}})

        client._progress = progress.append
        client._client = SimpleNamespace(models=SimpleNamespace(generate_content=reject_request))
        try:
            with patch("campaign_init.time.sleep") as sleep:
                with self.assertRaises(ci.CampaignInitError) as caught:
                    client.generate("system", [("user", "prompt")])
            self.assertEqual(len(calls), 1)
            sleep.assert_not_called()
            reported = str(caught.exception) + " ".join(progress)
            self.assertIn("Invalid response mime type", reported)
            self.assertNotIn(key, reported)
            self.assertNotIn("private campaign data", reported)
        finally:
            original_client.close()

    def test_gemini_3_omits_deprecated_temperature_but_requests_json(self):
        client = ci.GeminiClient(api_key="test-only-key", model="gemini-3.8-flash")
        original_client = client._client
        captured = []

        def accept_request(**kwargs):
            captured.append(kwargs)
            return SimpleNamespace(text='{"status":"ok"}')

        client._client = SimpleNamespace(models=SimpleNamespace(generate_content=accept_request))
        try:
            self.assertEqual(client.generate("system", [("user", "prompt")]), '{"status":"ok"}')
            self.assertEqual(len(captured), 1)
            self.assertEqual(captured[0]["config"].response_mime_type, "application/json")
            self.assertIsNone(captured[0]["config"].temperature)
        finally:
            original_client.close()

    def test_exhausted_quota_is_not_retried(self):
        from google.genai import errors

        client = ci.GeminiClient(api_key="test-only-key", max_request_attempts=2)
        original_client = client._client
        calls = []

        def reject_request(**kwargs):
            calls.append(1)
            raise errors.ClientError(429, {"error": {"message": "Quota exhausted for this project"}})

        client._client = SimpleNamespace(models=SimpleNamespace(generate_content=reject_request))
        try:
            with patch("campaign_init.time.sleep") as sleep:
                with self.assertRaises(ci.CampaignInitError):
                    client.generate("system", [("user", "prompt")])
            self.assertEqual(len(calls), 1)
            sleep.assert_not_called()
        finally:
            original_client.close()

    def test_real_azgaar_world_completes_local_campaign_stages(self):
        fixture_path = Path(__file__).parent / "fixtures" / "frepoyland.json.gz"
        with gzip.open(fixture_path, "rt", encoding="utf-8") as stream:
            world = world_parser.parse_world(json.load(stream)["minimal"])
        proposal = good_response()
        proposal["message"] = f"Welcome to {world['pack']['burgs'][0]['name']}. Who are you?"
        with TemporaryDirectory() as directory:
            path = Path(directory)
            ci._dump(path / "world.json", world, True)
            messages = []
            introduction = ci.initialize_campaign_from_directory(
                path, client=FakeClient(proposal), progress=messages.append)
            self.assertEqual(introduction, proposal["message"])
            self.assertTrue((path / "gamestate.json").exists())
            self.assertTrue((path / "notes.json").exists())
            self.assertTrue(any("persistence stage ended" in m for m in messages))

    def test_world_brief_contains_ids_and_relations(self):
        brief = ci.build_world_brief(make_world())
        self.assertEqual({b["id"] for b in brief["settlements"]}, {1, 2})
        self.assertEqual(brief["states"][1]["diplomacy"], {"Brevik": "Enemy"})


if __name__ == "__main__":
    unittest.main()
