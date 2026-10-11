# Phase 3C world-operation contract

`world_interactions.apply_world_operations(world, notes, gamestate, operations, *, coastlines=None)`
returns `WorldOperationResult(world, notes, gamestate, events, audio_cues)` or raises
`WorldOperationError(index, message)`. It reads only the supplied dictionaries, deep-copies
them, and never writes files, calls an API, advances the turn counter, or changes its inputs.
The result dictionaries are the candidates for Phase 3B's **single final transaction**
alongside its player and turn changes. `events` contain public, validated outcomes suitable
for narration; `audio_cues` contain `{operation_id, kind, cue}` metadata for an optional
audio service. Neither contains DM-private text. On error, discard the entire proposal.

The repository has no Phase 3B interface yet. This service accepts the normalized
`world.json` made by `world_parser.parse_world`: `metadata` and `pack` collections with
stable Azgaar integer IDs. `coastlines.json` supplies `landmasses`/`water_bodies`
GeoJSON, seed, dimensions, and map coordinates. The initializer writes
`gamestate.json` with `campaign.starting_location.burg_id`, `npcs` and `quests`
dictionaries keyed by stable snake_case IDs, plus player/history; and `notes.json`
with `campaign`, `established_lore`, `plot_threads`, `secrets`, and `event_log`.
The desktop's separate `GameSaveStore` is a mock narrative-session format, not this
authoritative campaign transaction. Character creation and the real turn engine
are not present in this checkout.

Every operation is a JSON object with required `type` and stable `id` (snake_case),
plus optional `reason` (at most 500 characters). Unknown fields/types are rejected.
Use a new ID for each logical operation; an exact replay of a committed operation is
skipped via `gamestate.world_operation_receipts`. Reusing an ID with different content
fails. The caller must preserve those receipts when it commits the returned gamestate.
IDs should be derived from a stable turn proposal ID and operation ordinal.

| Type | Required fields | Optional fields |
| --- | --- | --- |
| `travel` | `destination_burg_id`, `mode`, `duration_days` | `border_strategy`, `route_justification`, `ability_id` |
| `npc_create` | `npc_id`, `name`, `role`, `description`, `motivation`, `location_burg_id` | `faction_id` |
| `npc_relationship_update` | `npc_id`, `target_id`, `relation` | |
| `npc_status_update` | `npc_id`, `status` | |
| `quest_create` | `quest_id`, `title`, `description`, `giver_npc_id`, `location_burg_id`, `objectives` | |
| `quest_activate`, `quest_complete`, `quest_fail`, `quest_abandon` | `quest_id` | |
| `quest_progress` | `quest_id`, `objective_index` (zero based) | |
| `world_event` | `text`, `status`, `visibility` | `burg_ids`, `state_ids` |
| `settlement_condition_update` | `burg_id`, `condition`, `status`, `description` | `controller_state_id` or `controller_faction_id` |
| `faction_update` | `faction_id`, `relationship` | `influence_burg_id` |

Relationships are `hostile`, `wary`, `neutral`, `friendly`, or `trusting`; `target_id`
may be `player` or an existing NPC ID. NPC statuses are `active`, `injured`,
`missing`, `imprisoned`, or `dead`. Dead is terminal. A campaign-generated quest's
existing `objectives` list is accepted; new `completed_objectives` and `history`
fields appear when it progresses. A quest goes `available` → `active` → `completed`,
`failed`, or `abandoned`; completion requires every objective to be marked. Failure
and abandonment can also end an available quest. Terminal quests cannot reopen.
Settlement conditions are `occupied`, `damaged`, `recovering`, `prosperous`,
`under_siege`, `unsafe`, `safe`, `plague`, `flooded`, or `burned`, each with `active`
or `resolved` status. A resolution requires an active prior condition. World-event
statuses are `established`, `planned`, or `rumor`; visibility is `public` or
`dm_private`.

Travel starts at `campaign.current_location.burg_id`, falling back to
`campaign.starting_location.burg_id`; successful travel writes `current_location`.
All non-teleport travel requires matching coastline data. Walking/riding requires
both settlements on the same land feature and a direct line contained in its land
polygon. That verifies a land corridor, **not a road**. If the direct line crosses
water or leaves the feature, the operation rejects rather than inventing a route.
Ships require ports at both ends referencing the same mapped water body, a water
crossing, and `route_justification` for the narrative service or vessel; the map
alone cannot prove a shipping service.
Crossing a state border requires `border_strategy` (`permit` or `covert`) and a
justification. Teleport requires an `ability_id` already present in
`player.abilities`. These are narrative checks; the caller must ensure claimed
permissions and passage are actually established in its story.

Distance is straight-line map distance. For `mi`/`km` worlds with a positive
`distanceScale`, minimum durations use conservative rates of 20/40/80 miles per
day for walk/ride/ship, converted for kilometers. Otherwise rates are 5/10/20 map
units per day. Distances over two map units round up to quarter days; local travel
can take zero days. These are lower bounds, not pathfinding or guarantees of safe
passage. The result returns distance, required duration, border crossing, and
route uncertainty for Gemini to narrate accurately.

World conditions live on burg records in `campaign_conditions`; political control
is a separate `campaign_political_control` overlay. The original Azgaar `state`,
coordinates, feature, and IDs stay intact. `campaign_changelog` records previous
conditions and political control so major changes retain their history.
Faction influence and player relationship are explicit fields on existing faction
records. `world_event` with `established` + `public` goes to the world and notes
and returns a public event for Phase 3B to add to player history. Planned,
rumored, or DM-private events stay only in notes. Its `turn` comes from the
campaign's current turn; Phase 3B should set the turn on its candidate state before
calling if it wants the new turn number. No operation exposes or alters
`notes.secrets`.

Example Phase 3B handoff:

```python
from world_interactions import apply_world_operations

operations = [{"id": "turn_5_travel_1", "type": "travel",
               "destination_burg_id": selected_burg_id, "mode": "walk", "duration_days": 2}]
proposal = apply_world_operations(world, notes, gamestate, operations,
                                  coastlines=coastlines)
# Phase 3B combines proposal.gamestate with its player/turn updates, then commits
# proposal.world, proposal.notes, and the combined gamestate together.
```

`audio_cues` currently emits `harbor` ambience for validated arrivals at ports,
`quest_complete` SFX for completed quests, and `danger` SFX for an active siege or
burning settlement. They are suggestions only; no audio generation occurs here.
