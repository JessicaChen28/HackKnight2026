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
constraints keep one turn as one persistence transaction.
