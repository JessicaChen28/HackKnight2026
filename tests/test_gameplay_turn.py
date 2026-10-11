"""Offline Phase 3B tests; Gemini and world handlers are fakes."""

from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch

import campaign_init as campaign
from gameplay_turn import TurnError, _turn_lock, process_turn
from desktop.services import CampaignIntro, GameError, GameSession, PhaseTwoCampaignService, WorldRef
from tests.test_campaign_init import make_world
from world_operations import NpcInteraction, OperationOutcome, WorldDispatcher, parse_operation


class FakeClient:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def generate(self, system, turns):
        self.calls.append((system, deepcopy(turns)))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response if isinstance(response, str) else json.dumps(response)


def good_turn(**changes):
    value = {
        "narration": "The innkeeper studies the map, then points toward the eastern road.",
        "dialogue": [{"speaker_id": "vera", "text": "The caravan left at dawn."}],
        "player_changes": {"inventory_add": ["Innkeeper's note"], "inventory_remove": ["Torch"]},
        "operations": [], "dm_note": "The innkeeper knows more than she said.",
        "audio_cues": {"sfx": ["A wooden door creaks"], "ambience": "Quiet tavern with a low fire"},
        "status": "continue",
    }
    value.update(changes)
    return value


class GameplayTurnTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.world = make_world()
        self.state = campaign.initial_gamestate()
        self.state.update({
            "phase": "playing",
            "player": {"name": "Arin", "inventory": ["Map", "Torch"], "conditions": [],
                       "health": 8, "max_health": 10, "location": "Karthal"},
            "campaign": {"title": "Ashen Crown", "current_turn": 0,
                         "starting_location": {"burg_id": 1, "name": "Karthal"}},
            "npcs": {"vera": {"name": "Vera", "location_burg_id": 1}},
            "quests": {"ledger": {"title": "Missing Ledger", "status": "available"}},
            "unrelated_field": {"preserve": True},
        })
        self.notes = campaign.initial_notes()
        for filename, value in (("world.json", self.world), ("gamestate.json", self.state),
                                ("notes.json", self.notes)):
            campaign._dump(self.path / filename, value)

    def tearDown(self):
        self.temp.cleanup()

    def loaded(self):
        return tuple(campaign.read_json(self.path / name)
                     for name in ("world.json", "gamestate.json", "notes.json"))

    def test_valid_turn_commits_once_preserves_data_and_returns_audio(self):
        client = FakeClient(good_turn())
        result = process_turn(self.path, "I ask the innkeeper about the caravan.",
                              client=client, expected_turn=0)
        world, state, notes = self.loaded()
        self.assertEqual(result.turn, 1)
        self.assertEqual(state["campaign"]["current_turn"], 1)
        self.assertEqual(state["player"]["inventory"], ["Map", "Innkeeper's note"])
        self.assertEqual(state["unrelated_field"], {"preserve": True})
        self.assertEqual(world, self.world)
        self.assertEqual(len(state["history"]), 3)
        self.assertEqual(notes["turn_notes"][-1]["turn"], 1)
        self.assertEqual(result.dialogue[0]["speaker_id"], "vera")
        self.assertEqual(result.audio_cues["sfx"], ["A wooden door creaks"])
        self.assertIn("PLAYER ATTEMPT", client.calls[0][1][0][1])

    def test_rejects_empty_action_phase_and_missing_character(self):
        with self.assertRaises(TurnError):
            process_turn(self.path, "  ", client=FakeClient(good_turn()))
        self.state["phase"] = "character_creation"
        campaign._dump(self.path / "gamestate.json", self.state)
        with self.assertRaisesRegex(TurnError, "playing phase"):
            process_turn(self.path, "Look around", client=FakeClient(good_turn()))
        self.state["phase"], self.state["player"] = "playing", None
        campaign._dump(self.path / "gamestate.json", self.state)
        with self.assertRaisesRegex(TurnError, "accepted character"):
            process_turn(self.path, "Look around", client=FakeClient(good_turn()))

    def test_malformed_response_correction_is_bounded(self):
        client = FakeClient("not JSON", good_turn())
        result = process_turn(self.path, "Ask Vera", client=client)
        self.assertEqual(result.turn, 1)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(self.loaded()[1]["campaign"]["current_turn"], 1)

    def test_invalid_response_exhaustion_leaves_files_unchanged(self):
        before = self.loaded()
        with self.assertRaisesRegex(TurnError, "invalid turn"):
            process_turn(self.path, "Ask Vera", client=FakeClient("bad", "[]"))
        self.assertEqual(self.loaded(), before)

    def test_timeout_and_quota_leave_state_unchanged(self):
        before = self.loaded()
        for error, message in ((TimeoutError("timed out"), "timed out"),
                               (campaign.CampaignInitError("quota exhausted"), "quota")):
            with self.assertRaisesRegex(TurnError, message):
                process_turn(self.path, "Ask Vera", client=FakeClient(error))
            self.assertEqual(self.loaded(), before)

    def test_overpowered_attempt_is_not_applied_as_a_claim(self):
        response = good_turn(narration="The king's guards stop your attempted strike.",
                             player_changes={}, dialogue=[], dm_note="")
        result = process_turn(self.path, "I instantly kill the king and take his throne.",
                              client=FakeClient(response))
        self.assertIn("stop", result.narration)
        self.assertEqual(self.loaded()[1]["player"]["inventory"], ["Map", "Torch"])

    def test_health_and_conditions_updates_are_bounded(self):
        response = good_turn(player_changes={"health_delta": -2,
                                             "conditions_add": ["Bruised"], "inventory_add": [],
                                             "inventory_remove": []})
        process_turn(self.path, "I force the door", client=FakeClient(response))
        player = self.loaded()[1]["player"]
        self.assertEqual(player["health"], 6)
        self.assertEqual(player["conditions"], ["Bruised"])

    def test_invalid_inventory_change_does_not_commit(self):
        before = self.loaded()
        response = good_turn(player_changes={"inventory_remove": ["Royal Crown"]})
        with self.assertRaises(TurnError):
            process_turn(self.path, "Drop the crown", client=FakeClient(response), max_proposals=1)
        self.assertEqual(self.loaded(), before)

    def test_stale_and_duplicate_turns_are_rejected(self):
        with self.assertRaisesRegex(TurnError, "Stale turn"):
            process_turn(self.path, "Look around", client=FakeClient(good_turn()), expected_turn=5)
        with _turn_lock(self.path):
            with self.assertRaisesRegex(TurnError, "already processing"):
                process_turn(self.path, "Look around", client=FakeClient(good_turn()))

    def test_external_change_during_model_call_rejects_result(self):
        class ChangingClient(FakeClient):
            def generate(inner, system, turns):
                state = campaign.read_json(self.path / "gamestate.json")
                state["external_change"] = True
                campaign._dump(self.path / "gamestate.json", state)
                return json.dumps(good_turn())
        with self.assertRaisesRegex(TurnError, "files changed"):
            process_turn(self.path, "Look around", client=ChangingClient())
        self.assertEqual(self.loaded()[1]["campaign"]["current_turn"], 0)

    def test_world_operation_dispatch_uses_typed_handler(self):
        dispatcher = WorldDispatcher()
        seen = []
        def handle(operation, world, state, notes):
            self.assertIsInstance(operation, NpcInteraction)
            seen.append(operation.npc_id)
            notes["event_log"].append({"turn": 1, "text": "Vera shared a clue"})
            return OperationOutcome("Vera shared a clue")
        dispatcher.register("npc_interaction", handle)
        response = good_turn(operations=[{"type": "npc_interaction", "npc_id": "vera", "intent": "ask about caravan"}])
        result = process_turn(self.path, "Ask Vera", client=FakeClient(response), dispatcher=dispatcher)
        self.assertEqual(seen, ["vera"])
        self.assertEqual(result.events[0]["type"], "npc_interaction")
        self.assertEqual(self.loaded()[2]["event_log"][-1]["text"], "Vera shared a clue")

    def test_handler_failure_discards_earlier_detached_changes(self):
        dispatcher = WorldDispatcher()
        def fail_after_change(operation, world, state, notes):
            world["temporary_change"] = "never committed"
            raise ValueError("Cannot resolve this event")
        dispatcher.register("world_event", fail_after_change)
        before = self.loaded()
        response = good_turn(operations=[{"type": "world_event", "description": "A storm approaches"}])
        with self.assertRaises(TurnError):
            process_turn(self.path, "Watch the sky", client=FakeClient(response),
                         dispatcher=dispatcher, max_proposals=1)
        self.assertEqual(self.loaded(), before)

    def test_world_operation_rejects_arbitrary_patch_and_bad_travel(self):
        with self.assertRaises(ValueError):
            parse_operation({"type": "world_event", "description": "Storm", "changes": {"king": "dead"}})
        with self.assertRaises(ValueError):
            parse_operation({"type": "travel_request", "direction": "east"})

    def test_unhandled_world_operation_and_persistence_failure_do_not_commit(self):
        before = self.loaded()
        response = good_turn(operations=[{"type": "travel_request", "destination_burg_id": 2}])
        with self.assertRaises(TurnError):
            process_turn(self.path, "Travel", client=FakeClient(response), max_proposals=1)
        self.assertEqual(self.loaded(), before)
        with patch("gameplay_turn.save_all", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                process_turn(self.path, "Ask Vera", client=FakeClient(good_turn()))
        self.assertEqual(self.loaded(), before)

    def test_cancelled_turn_never_commits(self):
        cancellation = Event()
        cancellation.set()
        with self.assertRaisesRegex(TurnError, "cancelled"):
            process_turn(self.path, "Ask Vera", client=FakeClient(good_turn()), cancel_event=cancellation)
        self.assertEqual(self.loaded()[1]["campaign"]["current_turn"], 0)

    def test_desktop_adapter_uses_turn_engine_for_playing_campaign(self):
        session = GameSession(WorldRef("Test", str(self.path)), "Opening", "A brave traveler", "Arin",
                              "Karthal", inventory=["Map", "Torch"], quests=["Missing Ledger"])
        service = PhaseTwoCampaignService(lambda: "test-only-key")
        fake = FakeClient(good_turn())
        fake.close = lambda: None
        with patch("campaign_init.GeminiClient", return_value=fake):
            updated = service.perform_action(session, "Ask Vera", Event())
        self.assertEqual(updated.turn, 1)
        self.assertIn("Innkeeper's note", updated.inventory)
        self.assertEqual(self.loaded()[1]["campaign"]["current_turn"], 1)
        self.assertEqual(session.turn, 0)

    def test_production_adapter_never_falls_back_to_mock_gameplay(self):
        service = PhaseTwoCampaignService(lambda: "test-only-key")
        missing_world = WorldRef("Missing")
        with self.assertRaisesRegex(GameError, "production campaign directory is missing"):
            service.create_character(
                CampaignIntro(missing_world, "Opening"), "A traveler", Event())
        session = GameSession(missing_world, "Opening", "A traveler", "Arin", "Unknown")
        with self.assertRaisesRegex(GameError, "production campaign files are missing"):
            service.perform_action(session, "Walk onward", Event())
        self.assertEqual(session.turn, 0)
        self.assertEqual(session.history, [])


if __name__ == "__main__":
    unittest.main()
