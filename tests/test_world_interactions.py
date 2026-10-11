"""Pure Phase 3C behavior; no model, browser, audio service, or disk commits."""

from copy import deepcopy
import gzip
import json
from pathlib import Path
import unittest

from world_interactions import WorldOperationError, apply_world_operations


def polygon(coords):
    return {"type": "Polygon", "coordinates": [coords]}


def campaign():
    world = {"schema_version": 1, "metadata": {"mapName": "Test", "seed": "123", "width": 100,
            "height": 100, "distanceScale": 1, "distanceUnit": "mi"}, "pack": {
                "burgs": [{"i": 1, "name": "Harbor", "x": 10, "y": 10, "state": 1, "feature": 1, "port": 3},
                          {"i": 2, "name": "Hill", "x": 20, "y": 10, "state": 1, "feature": 1},
                          {"i": 3, "name": "Far Isle", "x": 80, "y": 10, "state": 2, "feature": 2, "port": 3},
                          {"i": 4, "name": "Long Road", "x": 45, "y": 10, "state": 1, "feature": 1}],
                "states": [{"i": 1, "name": "West"}, {"i": 2, "name": "East"}],
                "features": [{"i": 1, "land": True}, {"i": 2, "land": True}, {"i": 3, "land": False}],
                "routes": [], "rivers": [], "provinces": [], "cultures": [], "religions": [],
                "markers": [], "zones": []},
             "factions": {"guild": {"id": "guild", "name": "Guild"}}, "unrelated": {"keep": True}}
    coasts = {"seed": "123", "map_width": 100, "map_height": 100,
              "landmasses": [{"feature_id": 1, "geometry": polygon([[0, 0], [50, 0], [50, 50], [0, 50], [0, 0]])},
                             {"feature_id": 2, "geometry": polygon([[70, 0], [100, 0], [100, 50], [70, 50], [70, 0]])}],
              "water_bodies": [{"feature_id": 3,
                                "geometry": polygon([[50, 0], [70, 0], [70, 50], [50, 50], [50, 0]])}]}
    gs = {"schema_version": 1, "phase": "playing", "player": {"abilities": []},
          "campaign": {"title": "Test", "current_turn": 4, "starting_location": {"burg_id": 1}},
          "npcs": {"vera": {"id": "vera", "name": "Vera", "location_burg_id": 1,
                            "relationships": []}},
          "quests": {"ledger": {"id": "ledger", "title": "Ledger", "giver_npc_id": "vera",
                                "location_burg_id": 1, "objectives": ["Find it", "Return it"],
                                "status": "available"}}, "history": []}
    notes = {"schema_version": 1, "campaign": {"title": "Test"}, "established_lore": [],
             "plot_threads": [], "secrets": [{"text": "Hidden murderer", "revealed": False}],
             "event_log": []}
    return world, notes, gs, coasts


def op(kind, identifier="action_one", **fields):
    return {"type": kind, "id": identifier, **fields}


class WorldInteractionTests(unittest.TestCase):
    def setUp(self):
        self.world, self.notes, self.gs, self.coasts = campaign()

    def apply(self, *ops):
        return apply_world_operations(self.world, self.notes, self.gs, list(ops), coastlines=self.coasts)

    def assert_rejected(self, operation, phrase):
        before = deepcopy((self.world, self.notes, self.gs))
        with self.assertRaisesRegex(WorldOperationError, phrase):
            self.apply(operation)
        self.assertEqual(before, (self.world, self.notes, self.gs))

    def test_land_travel_real_ids_and_no_input_mutation(self):
        result = self.apply(op("travel", destination_burg_id=2, mode="walk", duration_days=0.5))
        self.assertEqual(result.gamestate["campaign"]["current_location"]["burg_id"], 2)
        self.assertIn("road not verified", result.events[0]["route_status"])
        self.assertEqual(self.gs["campaign"]["starting_location"]["burg_id"], 1)
        self.assertEqual(result.world["unrelated"], {"keep": True})

    def test_unknown_destination_and_obstacle(self):
        self.assert_rejected(op("travel", destination_burg_id=999, mode="walk", duration_days=10), "unknown burgs")
        self.assert_rejected(op("travel", destination_burg_id=3, mode="walk", duration_days=10), "crosses water")

    def test_long_distance_and_border_ship(self):
        self.assert_rejected(op("travel", destination_burg_id=4, mode="ride", duration_days=0), "duration too short")
        self.assert_rejected(op("travel", destination_burg_id=3, mode="ship", duration_days=2,
                                route_justification="A captain offers passage."), "border_strategy")
        self.world["pack"]["burgs"][2]["port"] = 4
        self.assert_rejected(op("travel", destination_burg_id=3, mode="ship", duration_days=2,
                                route_justification="A captain offers passage.", border_strategy="permit"),
                             "different water bodies")
        self.world["pack"]["burgs"][2]["port"] = 3
        result = self.apply(op("travel", destination_burg_id=3, mode="ship", duration_days=2,
                               route_justification="A captain offers passage.", border_strategy="permit"))
        self.assertEqual(result.events[0]["minimum_duration_days"], 1)
        self.assertEqual(result.audio_cues[0]["cue"], "harbor")
        self.assertTrue(result.events[0]["border_crossing"])

    def test_missing_coastlines_and_teleport_ability(self):
        action = op("travel", destination_burg_id=2, mode="walk", duration_days=1)
        with self.assertRaisesRegex(WorldOperationError, "coastlines"):
            apply_world_operations(self.world, self.notes, self.gs, [action])
        self.assert_rejected(op("travel", destination_burg_id=3, mode="teleport", duration_days=0,
                                ability_id="blink"), "established player ability")
        self.gs["player"]["abilities"] = ["blink"]
        result = self.apply(op("travel", destination_burg_id=3, mode="teleport", duration_days=0,
                               ability_id="blink"))
        self.assertEqual(result.events[0]["route_status"], "ability")

    def test_npc_create_relationship_status_and_unknown_references(self):
        created = self.apply(op("npc_create", npc_id="orrin", name="Orrin", role="Captain",
                                description="Steady", motivation="Duty", location_burg_id=2))
        self.assertEqual(created.gamestate["npcs"]["orrin"]["state_id"], 1)
        rel = op("npc_relationship_update", "rel_one", npc_id="vera", target_id="player", relation="trusting")
        injured = op("npc_status_update", "status_one", npc_id="vera", status="injured")
        result = self.apply(rel, injured)
        self.assertEqual(result.gamestate["npcs"]["vera"]["player_relationship"], "trusting")
        self.assertEqual(result.gamestate["npcs"]["vera"]["status"], "injured")
        self.assertEqual(len(result.gamestate["npcs"]["vera"]["interaction_history"]), 2)
        self.assert_rejected(op("npc_status_update", npc_id="unknown", status="dead"), "unknown NPC")
        self.assert_rejected(op("npc_create", npc_id="vera", name="Other", role="Guard",
                                description="Test", motivation="Test", location_burg_id=1), "already exists")

    def test_quest_creation_progress_completion_and_terminals(self):
        create = op("quest_create", quest_id="escort", title="Escort", description="Protect a caravan",
                    giver_npc_id="vera", location_burg_id=2, objectives=["Depart", "Arrive"])
        result = self.apply(create)
        self.assertEqual(result.gamestate["quests"]["escort"]["status"], "available")
        actions = [op("quest_activate", "activate", quest_id="ledger"),
                   op("quest_progress", "first", quest_id="ledger", objective_index=0),
                   op("quest_progress", "second", quest_id="ledger", objective_index=1),
                   op("quest_complete", "done", quest_id="ledger")]
        result = self.apply(*actions)
        self.assertEqual(result.gamestate["quests"]["ledger"]["status"], "completed")
        self.assertEqual(len(result.gamestate["quests"]["ledger"]["history"]), 4)
        self.assertEqual(result.audio_cues[0]["cue"], "quest_complete")
        with self.assertRaisesRegex(WorldOperationError, "cannot apply"):
            apply_world_operations(result.world, result.notes, result.gamestate,
                                   [op("quest_activate", "again", quest_id="ledger")], coastlines=self.coasts)

    def test_invalid_quest_progress_and_failure(self):
        self.assert_rejected(op("quest_complete", quest_id="ledger"), "cannot apply")
        self.assert_rejected(op("quest_progress", quest_id="ledger", objective_index=9), "cannot apply")
        result = self.apply(op("quest_fail", quest_id="ledger"))
        self.assertEqual(result.gamestate["quests"]["ledger"]["status"], "failed")
        self.assert_rejected(op("quest_activate", quest_id="missing"), "unknown quest")

    def test_world_changes_and_private_continuity(self):
        condition = op("settlement_condition_update", burg_id=2, condition="occupied", status="active",
                       description="The guild controls the gates.", controller_faction_id="guild")
        event = op("world_event", "news", text="Hill was liberated.", status="established",
                   visibility="public", burg_ids=[2])
        result = self.apply(condition, event)
        self.assertEqual(result.world["pack"]["burgs"][1]["state"], 1)
        self.assertEqual(result.world["pack"]["burgs"][1]["campaign_political_control"], {"faction_id": "guild"})
        self.assertEqual(result.world["world_events"][0]["text"], "Hill was liberated.")
        self.assertEqual(result.notes["event_log"][0]["status"], "established")
        self.assertEqual(result.notes["event_log"][0]["turn"], 4)
        self.assertEqual(result.world["campaign_changelog"][0]["previous_condition"], None)
        self.assertNotIn("Hidden murderer", str(result.events))
        self.assert_rejected(op("settlement_condition_update", burg_id=2, condition="occupied", status="active",
                                description="X", controller_state_id=999), "unknown states")

    def test_planned_and_private_events_do_not_leak(self):
        result = self.apply(op("world_event", text="A coup is planned.", status="planned", visibility="dm_private"))
        self.assertEqual(result.notes["event_log"][0]["text"], "A coup is planned.")
        self.assertNotIn("world_events", result.world)
        self.assertNotIn("coup", str(result.events) + str(result.audio_cues) + str(result.gamestate))
        self.assert_rejected(op("world_event", text="Hidden murderer", status="established",
                                visibility="public"), "unrevealed DM secret")
        self.assert_rejected(op("world_event", text="Fake", status="established", visibility="public",
                                state_ids=[999]), "unknown states")

    def test_faction_and_atomic_batch(self):
        result = self.apply(op("faction_update", faction_id="guild", relationship="friendly", influence_burg_id=2))
        self.assertEqual(result.world["factions"]["guild"]["influence_burg_ids"], [2])
        before = deepcopy((self.world, self.notes, self.gs))
        with self.assertRaises(WorldOperationError):
            self.apply(op("npc_status_update", "ok", npc_id="vera", status="injured"),
                       op("quest_complete", "bad", quest_id="ledger"))
        self.assertEqual(before, (self.world, self.notes, self.gs))

    def test_replay_and_conflicting_id(self):
        action = op("npc_relationship_update", npc_id="vera", target_id="player", relation="friendly")
        first = self.apply(action)
        second = apply_world_operations(first.world, first.notes, first.gamestate, [action], coastlines=self.coasts)
        self.assertEqual(second.events, [])
        self.assertEqual(len(second.gamestate["npcs"]["vera"]["interaction_history"]), 1)
        with self.assertRaisesRegex(WorldOperationError, "reused"):
            apply_world_operations(first.world, first.notes, first.gamestate,
                                   [op("npc_relationship_update", npc_id="vera", target_id="player",
                                       relation="hostile")], coastlines=self.coasts)

    def test_unknown_type_and_unsafe_fields(self):
        self.assert_rejected(op("replace_world", data={}), "unknown operation")
        self.assert_rejected(op("npc_status_update", npc_id="vera", status="dead", x=99), "unsupported fields")
        self.assert_rejected(op("npc_status_update", npc_id="vera", status=[]), "unsupported NPC status")
        self.assert_rejected(op("faction_update", faction_id="guild", relationship=[]), "unsupported faction relationship")
        self.assert_rejected(op("settlement_condition_update", burg_id=1, condition=[], status="active",
                                description="bad"), "unsupported settlement condition")

    def test_real_azgaar_fixture_schema(self):
        import world_parser
        fixture = Path(__file__).parent / "fixtures" / "frepoyland.json.gz"
        with gzip.open(fixture, "rt", encoding="utf-8") as stream:
            data = json.load(stream)
        world = world_parser.parse_world(data["minimal"])
        coast = world_parser.build_coastlines(data["minimal"], data["geometry_source"], world)
        start = world["pack"]["burgs"][0]
        gs = deepcopy(self.gs)
        gs["campaign"]["starting_location"] = {"burg_id": start["i"]}
        action = op("npc_create", npc_id="fixture_npc", name="Guide", role="Guide",
                    description="Knows the city", motivation="Work", location_burg_id=start["i"])
        result = apply_world_operations(world, self.notes, gs, [action], coastlines=coast)
        self.assertEqual(result.gamestate["npcs"]["fixture_npc"]["location_burg_id"], start["i"])
        self.assertEqual(result.world["pack"]["burgs"], world["pack"]["burgs"])


if __name__ == "__main__":
    unittest.main()
