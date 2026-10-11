# Phase 3A/3B/3C integration contract

The desktop calls `character_creation.create_character(description, directory,
api_key=..., progress=..., cancel_event=...)` after Phase 2 has initialized a
world. An accepted result persists the character in `gamestate.json` and sets
`phase` to `playing`. A revision result returns questions without changing
the campaign. Both requests run in a Qt worker.

`gameplay_turn.process_turn(directory, player_action, *, client=None,
dispatcher=None, expected_turn=None, max_proposals=2, cancel_event=None,
progress=None) -> TurnResult` takes a natural-language attempt and reads
`world.json`, `gamestate.json`, `notes.json`, and any `coastlines.json`.
It requires an accepted character and `phase == "playing"`. The desktop passes
the UI's `expected_turn`; a per-campaign lock rejects simultaneous turns.

Gemini proposes a JSON object with required `narration` and optional
`dialogue`, `player_changes`, `operations`, `dm_note`, `audio_cues`, and
`status`. Dialogue uses existing NPC keys as `speaker_id`. Player changes
support bounded health changes when health exists, additions/removals from the
character's existing inventory format, existing condition lists, and a status
condition when the character has a status object. Arbitrary JSON paths and
unsupported fields are rejected. Audio cues are at most three SFX descriptions
and one ambience description.

The default world-operation service is Phase 3C's
`world_interactions.apply_world_operations(world, notes, gamestate, operations,
coastlines=...)`. Its full JSON schema and travel constraints are in
[WORLD_INTERACTIONS.md](WORLD_INTERACTIONS.md). Phase 3B assigns each proposed
operation a stable `turn_<number>_op_<ordinal>` ID, passes detached candidate
dictionaries to Phase 3C, and uses its validated results and replay receipts.
World-operation errors reject the proposal before persistence. The earlier
`world_operations.WorldDispatcher` remains available as an optional test and
adapter interface; desktop gameplay uses Phase 3C directly.

`TurnResult` contains the committed turn number, narration, NPC dialogue,
validated world events, audio cues (including Phase 3C suggestions), status,
player snapshot, location, and active quests. The desktop rebuilds its display
from the authoritative files after each turn. It sends narration and audio cues
to ElevenLabs only after the turn has committed. Audio failures do not roll back
gameplay.

Phase 2's `save_all` writes staged files and rolls back on caught failures;
it is not a crash-atomic transaction across multiple JSON files. The game save
button writes a small version-2 pointer to the world directory, retaining the
introduction and character opening. Loading it always rereads the authoritative
campaign files, so an older save pointer resumes the latest committed turn.
The offline demo keeps its version-1 narrative save format.
