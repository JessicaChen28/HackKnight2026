# Phase 3B/3C world operation contract

The shared Python API is [`world_operations.py`](world_operations.py). A Gemini
turn response may propose `operations`, but Phase 3B parses each proposal into
one of four immutable classes. It rejects unknown fields and arbitrary patches.

| JSON `type` | Required fields | Optional fields |
| --- | --- | --- |
| `travel_request` | either `destination_burg_id` or `direction` plus `distance` | none |
| `npc_interaction` | `npc_id`, `intent` | none |
| `quest_update` | `quest_id`, `event` | none |
| `world_event` | `description` | none |

`direction` is one of north, northeast, east, southeast, south, southwest,
west, northwest. `distance` is a positive map distance at most 1000. A
destination uses an Azgaar burg ID. Phase 3C must validate the destination,
travel rules, NPC/quest references, and consequences against its own world
model; Phase 3B does not interpret these requests as completed actions.

Register a handler with `WorldDispatcher.register(kind, handler)`. Its signature
is `handler(operation, world, gamestate, notes) -> OperationOutcome`. The three
dicts are detached copies of the authoritative files. Handler code may edit
them and must return a short, player-safe summary plus optional structured
events. Any handler failure aborts the whole turn before persistence. A turn
with an operation whose handler is absent is rejected. Register handlers before
calling `process_turn`; the desktop can continue using text-only turns until
Phase 3C supplies them.

Phase 3B commits validated turn data only after every handler succeeds. Phase
3C should not write files or call Gemini/ElevenLabs inside a handler. These
constraints let Phase 3B validate every proposed effect before it invokes the
existing rollback helper.

## Turn engine API

`gameplay_turn.process_turn(directory, player_action, *, client=None,
dispatcher=None, expected_turn=None, max_proposals=2, cancel_event=None,
progress=None) -> TurnResult` loads the three authoritative JSON files and
requires `gamestate.phase == "playing"` with a nonempty `player` object. Pass
`expected_turn` from the current UI session to reject a stale submission.
The engine locks the campaign directory while the request and commit run, so
a second submission is rejected. The caller may inject a fake Gemini client
with `generate(system_prompt, turns) -> str` for offline tests.

The structured Gemini response is an object with required `narration` text and
optional `dialogue`, `player_changes`, `operations`, `dm_note`, `audio_cues` and
`status`. Dialogue entries use existing `speaker_id` values. Player changes
allow bounded `health_delta` only when the character has numeric health, plus
inventory and condition additions/removals against existing lists. Audio cues
contain up to three sound-effect descriptions and one ambience description;
Phase 3B returns them without making audio requests. `status` is `continue`
or `campaign_complete`. Phase 3C operations use the schema above.

`TurnResult` contains the committed turn number, narration, dialogue, world
events, audio cues, status, a player snapshot, location and active quest names.
The engine uses Phase 2's `save_all` rollback helper for files changed by the
turn. This is a staged multi-file write with rollback on caught failures, not
an atomic filesystem transaction across multiple JSON files. A process crash
between file replacements still needs recovery work in a future phase.

Phase 3A has not landed in this checkout. Its accepted-character step should
set `gamestate.phase` to `playing`, populate `gamestate.player`, and keep
`campaign.current_turn` at zero. The desktop adapter uses this engine once
those conditions are true; earlier mock character creation continues to use
its mock gameplay path.
