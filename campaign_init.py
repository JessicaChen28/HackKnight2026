"""Phase 2A: Gemini campaign initialization.

Turns a parsed Azgaar ``world.json`` into an RPG campaign. Gemini only PROPOSES;
this backend validates every proposal and is the only code that touches disk.

Files (all in one world directory, e.g. exports/<world>/):
    world.json      current canonical world (mutable; Azgaar IDs/geometry protected)
    notes.json      the DM's private notepad
    gamestate.json  gameplay state (player stays null until character creation)

Usage:
    export GEMINI_API_KEY=...            # optional: GEMINI_MODEL
    python campaign_init.py exports/<world directory>

This module deliberately does not import Playwright or Shapely.
"""

import argparse
import copy
import json
import math
import os
import re
import shutil
import sys
import time
from pathlib import Path

DEFAULT_MODEL = "gemini-3.5-flash-lite"  # override with GEMINI_MODEL or --model
COLLECTIONS = {
    "burg": "burgs", "state": "states", "province": "provinces", "culture": "cultures",
    "religion": "religions", "marker": "markers", "feature": "features",
    "route": "routes", "river": "rivers", "zone": "zones",
}
# Geography, IDs and structural links Gemini may never change.
PROTECTED_KEYS = {
    "i", "x", "y", "cells", "points", "vertices", "geometry", "feature", "land", "type",
    "subtype", "state", "culture", "religion", "province", "capital", "port", "burg",
    "neighbors", "origins", "group",
}
REF_KEYS = {"burg_ids": "burg", "state_ids": "state", "culture_ids": "culture",
            "religion_ids": "religion", "province_ids": "province"}
SLUG = re.compile(r"^[a-z][a-z0-9_]{1,47}$")
KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,47}$")
MIN_NPCS, MIN_QUESTS, MIN_LORE, MIN_THREADS, MIN_SECRETS = 2, 1, 3, 2, 1


class CampaignInitError(RuntimeError):
    """Initialization failed; no files were modified."""


# --------------------------------------------------------------------------- state

def initial_gamestate() -> dict:
    return {"schema_version": 1, "phase": "uninitialized", "player": None,
            "campaign": None, "npcs": {}, "quests": {}, "history": []}


def initial_notes() -> dict:
    return {"schema_version": 1,
            "campaign": {"title": "", "premise": "", "main_conflict": "", "tone": "", "current_chapter": 1},
            "established_lore": [], "plot_threads": [], "secrets": [], "event_log": []}


# --------------------------------------------------------------------------- prompt

SYSTEM_PROMPT = """You are the Dungeon Master of a single-player, text-based fantasy RPG. \
You are designing the opening of a campaign grounded in a procedurally generated world.

Information hierarchy (never violate):
1. WORLD is authoritative for geography, settlements, states and politics.
2. GAMESTATE is authoritative for the player and completed actions.
3. ESTABLISHED lore and the event log are authoritative for campaign history.
4. PLANNED plot threads and secrets are private possibilities, never automatic changes to the world.

Rules:
- Use only settlement/state/culture/religion IDs that appear in the WORLD brief. Never invent IDs.
- Never change coordinates, IDs, geography or structural links (state, culture, capital, port...).
- Prefer adding new facts over overwriting existing ones. To change an existing value you must set
  "overrides_existing": true and give a strong reason; avoid this.
- Mark what has already happened as "established" and what you may introduce later as "planned".
- Do NOT create or describe the player's character. Do not start gameplay.
- Secrets must stay hidden: never reveal them in "message".
- Respond with a single JSON object only, no markdown fences."""

OUTPUT_SPEC = """Return exactly this JSON shape (all keys required unless marked optional):
{
  "message": "Atmospheric introduction for the player. Name the real starting settlement and its realm, set the tone, then ask who they are: name, background and abilities.",
  "world_updates": [
    {"entity_type": "burg|state|province|culture|religion|marker|feature|route|river|zone",
     "entity_id": <existing integer ID>, "changes": {"new_key": "value"}, "reason": "why",
     "overrides_existing": false (optional)},
    {"entity_type": "faction", "entity_id": "snake_case_new_id",
     "changes": {"name": "...", "description": "...", "goal": "(optional)", "home_burg_id": <existing id, optional>,
                 "operates_in_burg_ids": [..optional], "operates_in_state_ids": [..optional]},
     "reason": "why"}
  ],
  "gamestate_updates": {
    "phase": "character_creation",
    "campaign": {"title": "...", "starting_location": {"burg_id": <existing populated burg id>}, "current_turn": 0},
    "npcs": {"snake_case_key": {"name": "...", "role": "...", "description": "...", "motivation": "...",
             "location_burg_id": <existing id>, "faction": "(optional faction id)",
             "relationships": [{"npc_id": "other_key", "relation": "..."}] (optional)}},
    "quests": {"snake_case_key": {"title": "...", "description": "...", "giver_npc_id": "npc_key",
              "location_burg_id": <existing id>, "objectives": ["..."], "status": "available",
              "related_thread_id": "(optional plot thread id)"}}
  },
  "notes_updates": {
    "campaign": {"title": "(same as gamestate title)", "premise": "...", "main_conflict": "...", "tone": "...", "current_chapter": 1},
    "established_lore": [{"text": "history that is already true", "category": "(optional)",
                          "refs": {"burg_ids": [], "state_ids": []} (optional)}],
    "plot_threads": [{"id": "snake_case_id", "title": "...", "description": "...", "status": "established|planned",
                      "related_npc_ids": [], "related_burg_ids": [], "planned_beats": ["future, not yet happened"]}],
    "secrets": [{"text": "...", "status": "established|planned", "known_by": ["npc_key"], "reveal_conditions": "(optional)"}],
    "event_log": []
  }
}
Minimums: 2+ NPCs, 1+ quests, 3+ lore entries, 2+ plot threads (at least one "planned"), 1+ secret.
Put backstory in established_lore; leave event_log empty (the backend logs the campaign start).
Quest/NPC/plot thread keys are your own snake_case IDs."""


def _names(records):
    return {r["i"]: r.get("name") for r in records}


def build_world_brief(world: dict, max_burgs: int = 80) -> dict:
    """Compact, ID-bearing summary of world.json for the prompt (full file stays on disk)."""
    pack = world["pack"]
    states, cultures = _names(pack["states"]), _names(pack["cultures"])
    burgs = [b for b in pack["burgs"] if b.get("population", 0) > 0]
    chosen = {b["i"] for b in burgs if b.get("capital")}
    by_pop = sorted(burgs, key=lambda b: -b.get("population", 0))
    per_state = {}
    for b in by_pop:
        if per_state.get(b.get("state"), 0) < 3:
            per_state[b.get("state")] = per_state.get(b.get("state"), 0) + 1
            chosen.add(b["i"])
    for b in by_pop:
        if len(chosen) >= max_burgs:
            break
        chosen.add(b["i"])
    burg_names = _names(pack["burgs"])
    state_rows = []
    for s in pack["states"]:
        rel = {}
        for j, value in enumerate(s.get("diplomacy") or []):
            if j != s["i"] and j in states and isinstance(value, str) and value != "x":
                rel[states[j]] = value
        state_rows.append({"id": s["i"], "name": s.get("name"), "fullName": s.get("fullName"),
                           "form": s.get("form"), "capital_burg_id": s.get("capital"),
                           "capital": burg_names.get(s.get("capital")), "culture": cultures.get(s.get("culture")),
                           "neighbor_state_ids": s.get("neighbors"), "diplomacy": rel})
    burg_rows = [{"id": b["i"], "name": b.get("name"), "state_id": b.get("state"), "state": states.get(b.get("state")),
                  "culture": cultures.get(b.get("culture")), "population": b.get("population"),
                  "capital": bool(b.get("capital")), "port": bool(b.get("port")), "type": b.get("type"),
                  "walls": bool(b.get("walls")), "temple": bool(b.get("temple"))}
                 for b in by_pop if b["i"] in chosen]
    return {
        "metadata": world.get("metadata", {}),
        "states": state_rows,
        "settlements_shown": f"{len(burg_rows)} of {len(burgs)} populated settlements (valid IDs are only those listed)",
        "settlements": burg_rows,
        "cultures": [{"id": c["i"], "name": c.get("name"), "type": c.get("type")} for c in pack["cultures"]],
        "religions": [{"id": r["i"], "name": r.get("name"), "type": r.get("type"), "form": r.get("form"),
                       "deity": r.get("deity"), "culture_id": r.get("culture")} for r in pack["religions"]],
        "provinces": [{"id": p["i"], "name": p.get("name"), "state_id": p.get("state")} for p in pack["provinces"]],
        "markers": [{"id": m["i"], "name": m.get("name"), "type": m.get("type"), "note": str(m.get("note", ""))[:200]}
                    for m in pack["markers"][:30]],
        "named_features": [{"id": f["i"], "name": f.get("name"), "type": f.get("type")}
                           for f in pack["features"] if f.get("name")][:40],
        "factions": world.get("factions", {}),
    }


def build_user_prompt(world, gamestate, notes) -> str:
    return (
        "Create the campaign for this world.\n\n"
        f"WORLD BRIEF:\n{json.dumps(build_world_brief(world), separators=(',', ':'))}\n\n"
        f"GAMESTATE:\n{json.dumps(gamestate, separators=(',', ':'))}\n\n"
        f"NOTES:\n{json.dumps(notes, separators=(',', ':'))}\n\n"
        "Choose a starting settlement that actually exists, a central conflict rooted in the real kingdoms, "
        "cultures, religions and rivalries above, initial NPCs, quest hooks, established lore, and a few "
        "planned threads and secrets.\n\n" + OUTPUT_SPEC)


def build_correction_prompt(errors) -> str:
    listed = "\n".join(f"- {e}" for e in errors[:15])
    return ("Your response was rejected by the backend validator and nothing was saved. Problems:\n"
            f"{listed}\n\nReturn the complete corrected JSON object (same shape), fixing every problem. "
            "Use only IDs from the WORLD brief.")


# --------------------------------------------------------------------------- Gemini

def _diagnostic(progress, message):
    """Report only stage names and durations, never prompts or response data."""
    print(message, flush=True)
    if progress is not None:
        progress(message)


def _check_cancelled(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise CampaignInitError("Campaign initialization cancelled.")


def _safe_api_message(error, api_key: str) -> str:
    """Keep the provider's reason while removing credentials and request data."""
    message = getattr(error, "message", None)
    if not isinstance(message, str) or not message.strip():
        return "No error detail supplied by Gemini."
    # API errors normally contain a short reason. Never include subsequent
    # lines, which can contain request fields or values echoed by a server.
    message = message.splitlines()[0]
    for marker in ("WORLD BRIEF", "GAMESTATE", "NOTES:", "Authorization", "x-goog-api-key", "contents:"):
        index = message.lower().find(marker.lower())
        if index >= 0:
            message = message[:index] + "[request detail removed]"
    message = re.sub(r"(?i)Bearer\s+\S+", "Bearer [credential removed]", message)
    message = re.sub(r"(?i)(?:api[_-]?key|key)=\S+", "key=[credential removed]", message)
    message = re.sub(r"AIza[A-Za-z0-9_-]{12,}", "[credential removed]", message)
    if api_key:
        message = message.replace(api_key, "[credential removed]")
    message = re.sub(r"[\x00-\x1f\x7f]", " ", message).strip()
    return message[:240] or "Request detail removed."


class GeminiClient:
    """Thin wrapper over google-genai. Tests can substitute any object with .generate()."""

    def __init__(self, api_key=None, model=None, temperature=0.9, timeout_seconds=120,
                 max_request_attempts=2, progress=None, cancel_event=None):
        key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not key:
            raise CampaignInitError("Set GEMINI_API_KEY (or GOOGLE_API_KEY) to call Gemini.")
        try:
            from google import genai
            from google.genai import errors, types
        except ImportError as error:
            raise CampaignInitError("Install the SDK: python -m pip install google-genai") from error
        self._types, self._errors = types, errors
        self.model = (model or os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL).strip()
        print(f"Using Gemini model: {self.model}", flush=True)
        self.temperature = temperature
        if timeout_seconds <= 0 or not 1 <= max_request_attempts <= 3:
            raise ValueError("Gemini timeout and retry settings are invalid.")
        self._max_request_attempts = max_request_attempts
        self._progress = progress
        self._cancel_event = cancel_event
        self._api_key = key
        # The SDK can retry on its own. Disable those retries so the limits
        # below are observable and bounded at the application layer.
        self._client = genai.Client(
            api_key=key,
            http_options=types.HttpOptions(timeout=int(timeout_seconds * 1000),
                                           retry_options=types.HttpRetryOptions(attempts=1)))

    def generate(self, system_prompt: str, turns) -> str:
        types = self._types
        contents = [types.Content(role=role, parts=[types.Part.from_text(text=text)]) for role, text in turns]
        config_fields = {"system_instruction": system_prompt, "response_mime_type": "application/json"}
        # Gemini 3 no longer accepts custom sampling parameters. Keep the
        # configurable temperature for older model overrides only.
        if not self.model.startswith("gemini-3"):
            config_fields["temperature"] = self.temperature
        config = types.GenerateContentConfig(**config_fields)
        for attempt in range(1, self._max_request_attempts + 1):
            _check_cancelled(self._cancel_event)
            started = time.perf_counter()
            _diagnostic(self._progress, f"Gemini API request {attempt}/{self._max_request_attempts} started.")
            try:
                response = self._client.models.generate_content(model=self.model, contents=contents, config=config)
                if not response.text:
                    raise CampaignInitError("Gemini returned no text (possibly blocked by safety filters).")
                _diagnostic(self._progress, f"Gemini API request {attempt} completed in {time.perf_counter() - started:.3f}s.")
                return response.text
            except self._errors.APIError as error:
                code = getattr(error, "code", None)
                status = code if type(code) is int else "unknown"
                reason = _safe_api_message(error, self._api_key)
                quota_exhausted = code == 429 and any(
                    marker in reason.lower() for marker in ("quota", "billing", "insufficient credits"))
                transient = code in (429, 500, 502, 503, 504) and not quota_exhausted
                _diagnostic(self._progress, f"Gemini API request {attempt} failed after {time.perf_counter() - started:.3f}s (HTTP {status}): {reason}")
                if not transient or attempt == self._max_request_attempts:
                    raise CampaignInitError(f"Gemini API request failed (HTTP {status}): {reason}") from error
            except CampaignInitError:
                _diagnostic(self._progress, f"Gemini API request {attempt} returned no usable text after {time.perf_counter() - started:.3f}s.")
                raise
            except Exception as error:  # network, DNS, timeouts
                _diagnostic(self._progress, f"Gemini API request {attempt} failed after {time.perf_counter() - started:.3f}s ({type(error).__name__}).")
                if attempt == self._max_request_attempts:
                    raise CampaignInitError("Could not reach Gemini within the request limit.") from error
            delay = 2 ** attempt
            _diagnostic(self._progress, f"Retrying Gemini API request in {delay}s.")
            if self._cancel_event is not None:
                self._cancel_event.wait(delay)
            else:
                time.sleep(delay)

    def close(self) -> None:
        self._client.close()


def parse_model_json(raw: str) -> dict:
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        value = json.loads(text)
    except ValueError as error:
        raise ValueError(f"Response was not valid JSON: {error}") from error
    if not isinstance(value, dict):
        raise ValueError("Response must be a JSON object")
    return value


# --------------------------------------------------------------------------- validation

def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _text(value, path, errors, max_len=1500, required=True):
    if not isinstance(value, str) or not value.strip():
        if required:
            errors.append(f"{path} must be a non-empty string")
        return None
    if len(value) > max_len:
        errors.append(f"{path} is too long (max {max_len} characters)")
        return None
    return value.strip()


def _json_ok(value, limit=2000):
    try:
        return len(json.dumps(value, allow_nan=False)) <= limit
    except (TypeError, ValueError):
        return False


class WorldIndex:
    def __init__(self, world):
        pack = world["pack"]
        self.by_type = {t: {r["i"]: r for r in pack.get(c, []) if isinstance(r, dict) and "i" in r}
                        for t, c in COLLECTIONS.items()}

    def has(self, etype, eid):
        return _is_int(eid) and eid in self.by_type[etype]


def _check_ids(values, etype, path, idx, errors):
    if values is None:
        return []
    if not isinstance(values, list) or not all(idx.has(etype, v) for v in values):
        errors.append(f"{path} must be a list of existing {etype} IDs")
        return []
    return list(values)


def _check_refs(refs, path, idx, errors):
    if refs is None:
        return {}
    if not isinstance(refs, dict):
        errors.append(f"{path} must be an object")
        return {}
    return {k: _check_ids(refs.get(k), t, f"{path}.{k}", idx, errors) for k, t in REF_KEYS.items() if k in refs}


def _validate_world_updates(updates, idx, world, errors):
    out, seen = [], set()
    factions = set(world.get("factions") or {})
    previous = {(c.get("entity_type"), c.get("entity_id"), k)
                for c in world.get("campaign_changelog", []) for k in c.get("changes", {})}
    if not isinstance(updates, list):
        errors.append("world_updates must be a list")
        return out, factions
    for n, u in enumerate(updates):
        p = f"world_updates[{n}]"
        if not isinstance(u, dict):
            errors.append(f"{p} must be an object")
            continue
        etype, eid, changes = u.get("entity_type"), u.get("entity_id"), u.get("changes")
        reason = _text(u.get("reason"), f"{p}.reason", errors, 500)
        if not isinstance(changes, dict) or not changes:
            errors.append(f"{p}.changes must be a non-empty object")
            continue
        if etype == "faction":
            if not isinstance(eid, str) or not SLUG.match(eid) or eid in factions:
                errors.append(f"{p}.entity_id must be a NEW snake_case faction id (got {eid!r})")
                continue
            clean = {"id": eid}
            clean["name"] = _text(changes.get("name"), f"{p}.changes.name", errors, 120)
            clean["description"] = _text(changes.get("description"), f"{p}.changes.description", errors)
            if changes.get("goal") is not None:
                clean["goal"] = _text(changes["goal"], f"{p}.changes.goal", errors)
            if changes.get("home_burg_id") is not None:
                clean["home_burg_id"] = _check_ids([changes["home_burg_id"]], "burg", f"{p}.changes.home_burg_id", idx, errors)[:1] and changes["home_burg_id"]
            for key, etype2 in (("operates_in_burg_ids", "burg"), ("operates_in_state_ids", "state")):
                if key in changes:
                    clean[key] = _check_ids(changes[key], etype2, f"{p}.changes.{key}", idx, errors)
            factions.add(eid)
            out.append({"entity_type": "faction", "entity_id": eid, "changes": clean, "reason": reason})
        elif etype in COLLECTIONS:
            if not idx.has(etype, eid):
                errors.append(f"{p}: {etype} with id {eid!r} does not exist in world.json")
                continue
            record, override = idx.by_type[etype][eid], u.get("overrides_existing") is True
            for key, value in changes.items():
                if key in PROTECTED_KEYS:
                    errors.append(f"{p}: '{key}' is protected (IDs, coordinates, geography and structural links cannot change)")
                elif not KEY.match(str(key)) or not _json_ok(value):
                    errors.append(f"{p}: invalid key or value for '{key}'")
                elif (etype, eid, key) in seen or (etype, eid, key) in previous:
                    errors.append(f"{p}: '{key}' on {etype} {eid} was already set by another campaign update")
                elif key in record and record[key] != value and not override:
                    errors.append(f"{p}: {etype} {eid} already has '{key}' = {record[key]!r}; add new facts under a new key, "
                                  "or set overrides_existing true with a justified reason")
                seen.add((etype, eid, key))
            out.append({"entity_type": etype, "entity_id": eid, "changes": changes, "reason": reason,
                        "overrides_existing": override})
        else:
            errors.append(f"{p}.entity_type {etype!r} is not supported")
    return out, factions


def _validate_npcs(npcs, idx, factions, errors):
    out = {}
    if not isinstance(npcs, dict) or len(npcs) < MIN_NPCS:
        errors.append(f"gamestate_updates.npcs must be an object with at least {MIN_NPCS} NPCs")
        return out
    names = set()
    for key, npc in npcs.items():
        p = f"npcs.{key}"
        if not SLUG.match(str(key)) or not isinstance(npc, dict):
            errors.append(f"{p}: key must be snake_case and value an object")
            continue
        clean = {"id": key, "name": _text(npc.get("name"), p + ".name", errors, 100),
                 "role": _text(npc.get("role"), p + ".role", errors, 150),
                 "description": _text(npc.get("description"), p + ".description", errors),
                 "motivation": _text(npc.get("motivation"), p + ".motivation", errors)}
        if clean["name"] and clean["name"].lower() in names:
            errors.append(f"{p}.name duplicates another NPC")
        names.add((clean["name"] or "").lower())
        loc = npc.get("location_burg_id")
        if not idx.has("burg", loc):
            errors.append(f"{p}.location_burg_id {loc!r} is not an existing settlement ID")
        else:
            clean["location_burg_id"] = loc
            clean["state_id"] = idx.by_type["burg"][loc].get("state")
        if npc.get("faction") is not None:
            if npc["faction"] not in factions:
                errors.append(f"{p}.faction {npc['faction']!r} is not an existing or newly proposed faction")
            else:
                clean["faction"] = npc["faction"]
        clean["relationships"] = []
        out[key] = clean
    for key, npc in npcs.items():  # relationships need all keys present
        rels = npc.get("relationships") if isinstance(npc, dict) else None
        for r in rels or []:
            ok = isinstance(r, dict) and r.get("npc_id") in npcs and r.get("npc_id") != key
            relation = _text(r.get("relation"), f"npcs.{key}.relationships", errors, 200) if isinstance(r, dict) else None
            if not ok:
                errors.append(f"npcs.{key}.relationships must reference other NPC keys")
            elif relation and key in out:
                out[key]["relationships"].append({"npc_id": r["npc_id"], "relation": relation})
    return out


def _validate_notes(nu, idx, npcs, errors):
    notes = {"campaign": {}, "established_lore": [], "plot_threads": [], "secrets": [], "event_log": []}
    if not isinstance(nu, dict):
        errors.append("notes_updates must be an object")
        return notes
    extra = set(nu) - set(notes)
    if extra:
        errors.append(f"notes_updates has unsupported keys: {sorted(extra)}")
    c = nu.get("campaign") if isinstance(nu.get("campaign"), dict) else {}
    notes["campaign"] = {k: _text(c.get(k), f"notes_updates.campaign.{k}", errors, 1500)
                         for k in ("title", "premise", "main_conflict", "tone")}
    if c.get("current_chapter", 1) != 1:
        errors.append("notes_updates.campaign.current_chapter must be 1")
    notes["campaign"]["current_chapter"] = 1

    lore = nu.get("established_lore")
    if not isinstance(lore, list) or len(lore) < MIN_LORE:
        errors.append(f"established_lore needs at least {MIN_LORE} entries")
    for n, item in enumerate(lore if isinstance(lore, list) else []):
        item = item if isinstance(item, dict) else {"text": item}
        entry = {"id": f"lore_{n + 1}", "status": "established",
                 "text": _text(item.get("text"), f"established_lore[{n}].text", errors)}
        if item.get("category") is not None:
            entry["category"] = _text(item["category"], f"established_lore[{n}].category", errors, 60)
        entry["refs"] = _check_refs(item.get("refs"), f"established_lore[{n}].refs", idx, errors)
        notes["established_lore"].append(entry)

    threads = nu.get("plot_threads")
    if not isinstance(threads, list) or len(threads) < MIN_THREADS:
        errors.append(f"plot_threads needs at least {MIN_THREADS} entries")
    used = set()
    for n, t in enumerate(threads if isinstance(threads, list) else []):
        p = f"plot_threads[{n}]"
        if not isinstance(t, dict):
            errors.append(f"{p} must be an object")
            continue
        tid = t.get("id") if isinstance(t.get("id"), str) and SLUG.match(t["id"]) and t["id"] not in used else f"thread_{n + 1}"
        used.add(tid)
        if t.get("status") not in ("established", "planned"):
            errors.append(f"{p}.status must be 'established' or 'planned' at initialization (not 'resolved')")
        rel_npcs = t.get("related_npc_ids") or []
        if not isinstance(rel_npcs, list) or any(k not in npcs for k in rel_npcs):
            errors.append(f"{p}.related_npc_ids must reference NPC keys from gamestate_updates.npcs")
            rel_npcs = []
        beats = t.get("planned_beats") or []
        if not isinstance(beats, list) or not all(isinstance(b, str) for b in beats):
            errors.append(f"{p}.planned_beats must be a list of strings")
            beats = []
        notes["plot_threads"].append({
            "id": tid, "title": _text(t.get("title"), p + ".title", errors, 150),
            "description": _text(t.get("description"), p + ".description", errors), "status": t.get("status"),
            "related_npc_ids": rel_npcs, "related_burg_ids": _check_ids(t.get("related_burg_ids"), "burg", p + ".related_burg_ids", idx, errors),
            "planned_beats": beats})
    if notes["plot_threads"] and not any(t["status"] == "planned" for t in notes["plot_threads"]):
        errors.append("At least one plot thread must have status 'planned'")

    secrets = nu.get("secrets")
    if not isinstance(secrets, list) or len(secrets) < MIN_SECRETS:
        errors.append(f"secrets needs at least {MIN_SECRETS} entry")
    for n, s in enumerate(secrets if isinstance(secrets, list) else []):
        p = f"secrets[{n}]"
        s = s if isinstance(s, dict) else {}
        if s.get("revealed") not in (None, False):
            errors.append(f"{p}.revealed must be false at initialization")
        if s.get("status") not in ("established", "planned"):
            errors.append(f"{p}.status must be 'established' or 'planned'")
        known = s.get("known_by") or []
        if not isinstance(known, list) or any(k not in npcs for k in known):
            errors.append(f"{p}.known_by must reference NPC keys")
            known = []
        entry = {"id": f"secret_{n + 1}", "text": _text(s.get("text"), p + ".text", errors), "status": s.get("status"),
                 "known_by": known, "revealed": False}
        if s.get("reveal_conditions") is not None:
            entry["reveal_conditions"] = _text(s["reveal_conditions"], p + ".reveal_conditions", errors)
        notes["secrets"].append(entry)

    events = nu.get("event_log", [])
    if not isinstance(events, list):
        errors.append("event_log must be a list")
        events = []
    for n, e in enumerate(events):
        e = e if isinstance(e, dict) else {}
        if e.get("turn", 0) != 0:
            errors.append(f"event_log[{n}].turn must be 0 at initialization")
        notes["event_log"].append({"turn": 0, "status": "established", "text": _text(e.get("text"), f"event_log[{n}].text", errors)})
    return notes


def _validate_quests(quests, idx, npcs, thread_ids, errors):
    out = {}
    if not isinstance(quests, dict) or len(quests) < MIN_QUESTS:
        errors.append(f"gamestate_updates.quests needs at least {MIN_QUESTS} quest")
        return out
    for key, q in quests.items():
        p = f"quests.{key}"
        if not SLUG.match(str(key)) or not isinstance(q, dict):
            errors.append(f"{p}: key must be snake_case and value an object")
            continue
        giver = q.get("giver_npc_id")
        if giver not in npcs:
            errors.append(f"{p}.giver_npc_id {giver!r} is not one of the NPC keys")
        loc = q.get("location_burg_id")
        if not idx.has("burg", loc):
            errors.append(f"{p}.location_burg_id {loc!r} is not an existing settlement ID")
        objectives = q.get("objectives")
        if not isinstance(objectives, list) or not objectives or not all(isinstance(o, str) and o.strip() for o in objectives):
            errors.append(f"{p}.objectives must be a non-empty list of strings")
            objectives = []
        status = q.get("status", "available")
        if status not in ("available", "active"):
            errors.append(f"{p}.status must be 'available' or 'active'")
        entry = {"id": key, "title": _text(q.get("title"), p + ".title", errors, 150),
                 "description": _text(q.get("description"), p + ".description", errors), "giver_npc_id": giver,
                 "location_burg_id": loc, "objectives": objectives, "status": status}
        if q.get("related_thread_id") is not None:
            if q["related_thread_id"] not in thread_ids:
                errors.append(f"{p}.related_thread_id {q['related_thread_id']!r} is not a plot thread ID")
            else:
                entry["related_thread_id"] = q["related_thread_id"]
        out[key] = entry
    return out


def apply_world_updates(world, updates):
    """Return a deep-copied world with validated updates applied, plus an audit changelog."""
    new = copy.deepcopy(world)
    idx = WorldIndex(new)
    log = new.setdefault("campaign_changelog", [])
    for u in updates:
        if u["entity_type"] == "faction":
            new.setdefault("factions", {})[u["entity_id"]] = {**u["changes"], "reason": u["reason"]}
            log.append({"turn": 0, "entity_type": "faction", "entity_id": u["entity_id"], "changes": u["changes"], "reason": u["reason"]})
            continue
        record = idx.by_type[u["entity_type"]][u["entity_id"]]
        previous = {k: record[k] for k in u["changes"] if k in record}
        record.update(copy.deepcopy(u["changes"]))
        entry = {"turn": 0, "entity_type": u["entity_type"], "entity_id": u["entity_id"], "changes": u["changes"], "reason": u["reason"]}
        if previous:
            entry["previous"] = previous
        log.append(entry)
    return new


def check_world_integrity(old, new):
    """Final guard: IDs, coordinates, geography and structural links are unchanged."""
    for etype, coll in COLLECTIONS.items():
        before, after = old["pack"].get(coll, []), new["pack"].get(coll, [])
        if len(before) != len(after):
            raise CampaignInitError(f"pack.{coll} changed length")
        for a, b in zip(before, after):
            if isinstance(a, dict):
                for key in PROTECTED_KEYS:
                    if a.get(key) != b.get(key):
                        raise CampaignInitError(f"Protected field {coll}[{a.get('i')}].{key} would change")


def validate_and_build(response, world, gamestate, notes):
    """Return (errors, (new_world, new_gamestate, new_notes, message)). Nothing is mutated."""
    errors = []
    unknown = set(response) - {"message", "world_updates", "gamestate_updates", "notes_updates"}
    if unknown:
        errors.append(f"Unsupported top-level keys: {sorted(unknown)}")
    idx = WorldIndex(world)
    message = _text(response.get("message"), "message", errors, 4000)
    updates, factions = _validate_world_updates(response.get("world_updates", []), idx, world, errors)

    gu = response.get("gamestate_updates")
    if not isinstance(gu, dict):
        errors.append("gamestate_updates must be an object")
        return errors, None
    extra = set(gu) - {"phase", "campaign", "npcs", "quests"}
    if extra:
        errors.append(f"gamestate_updates has unsupported keys: {sorted(extra)} (the player must stay null)")
    if gu.get("phase") != "character_creation":
        errors.append("gamestate_updates.phase must be 'character_creation'")
    camp = gu.get("campaign") if isinstance(gu.get("campaign"), dict) else {}
    if not camp:
        errors.append("gamestate_updates.campaign is required")
    title = _text(camp.get("title"), "gamestate_updates.campaign.title", errors, 150)
    if camp.get("current_turn", 0) != 0:
        errors.append("gamestate_updates.campaign.current_turn must be 0")
    loc = (camp.get("starting_location") or {}).get("burg_id") if isinstance(camp.get("starting_location"), dict) else None
    burg = idx.by_type["burg"].get(loc) if _is_int(loc) else None
    if burg is None or burg.get("population", 0) <= 0:
        errors.append(f"starting_location.burg_id {loc!r} is not an existing populated settlement")

    npcs = _validate_npcs(gu.get("npcs"), idx, factions, errors)
    new_notes_data = _validate_notes(response.get("notes_updates"), idx, npcs, errors)
    quests = _validate_quests(gu.get("quests"), idx, npcs, {t["id"] for t in new_notes_data["plot_threads"]}, errors)
    if title and new_notes_data["campaign"].get("title") and title != new_notes_data["campaign"]["title"]:
        errors.append("Campaign title must match in gamestate_updates and notes_updates")

    state = idx.by_type["state"].get(burg.get("state")) if burg else None
    if burg and message:
        anchors = [burg.get("name"), (state or {}).get("name"), (state or {}).get("fullName")]
        if not any(a and a.lower() in message.lower() for a in anchors):
            errors.append("message must name the starting settlement or its realm")
    if errors:
        return errors, None

    new_world = apply_world_updates(world, updates)
    check_world_integrity(world, new_world)

    new_gs = copy.deepcopy(gamestate)
    new_gs.update({
        "phase": "character_creation", "player": None,
        "campaign": {"title": title, "current_turn": 0,
                     "starting_location": {"burg_id": loc, "name": burg.get("name"), "state_id": burg.get("state"),
                                           "state_name": (state or {}).get("name")}},
        "npcs": npcs, "quests": quests})
    new_gs["history"] = list(new_gs.get("history", [])) + [
        {"turn": 0, "type": "campaign_initialized", "text": f"Campaign '{title}' created, starting in {burg.get('name')}."}]

    new_notes = copy.deepcopy(notes)
    for key in ("campaign", "established_lore", "plot_threads", "secrets"):
        new_notes[key] = new_notes_data[key]
    log = new_notes_data["event_log"]
    log.append({"turn": 0, "status": "established", "text": f"The campaign begins in {burg.get('name')}."})
    for n, e in enumerate(log):
        e["id"] = f"evt_{n + 1}"
    new_notes["event_log"] = log
    return [], (new_world, new_gs, new_notes, message)


# --------------------------------------------------------------------------- persistence

def read_json(path):
    with Path(path).open(encoding="utf-8-sig") as stream:
        return json.load(stream)


def _dump(path, data, compact=False):
    with Path(path).open("w", encoding="utf-8") as stream:
        if compact:
            json.dump(data, stream, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
        else:
            json.dump(data, stream, ensure_ascii=False, allow_nan=False, indent=2)


def save_all(directory, files):
    """Write several JSON files all-or-nothing. files: {filename: (data, compact)}."""
    directory = Path(directory)
    temps, backups = {}, {}
    try:
        for name, (data, compact) in files.items():
            temps[name] = directory / f".{name}.tmp"
            _dump(temps[name], data, compact)
            read_json(temps[name])  # verify what we wrote parses
        for name in files:
            target = directory / name
            if target.exists():
                backups[name] = directory / f".{name}.bak"
                shutil.copy2(target, backups[name])
        try:
            for name in files:
                os.replace(temps[name], directory / name)
        except BaseException:
            for name, backup in backups.items():
                os.replace(backup, directory / name)
            for name in files:
                if name not in backups:
                    (directory / name).unlink(missing_ok=True)
            raise
    finally:
        for path in list(temps.values()) + list(backups.values()):
            path.unlink(missing_ok=True)


# --------------------------------------------------------------------------- public API

def initialize_campaign(world: dict, gamestate: dict, notes: dict, directory=None,
                        client=None, max_attempts: int = 3, progress=None, cancel_event=None) -> str:
    """Ask Gemini for a campaign, validate it, apply it, and return the player-facing intro.

    On success the passed dicts are updated in place (and the three files saved
    atomically when ``directory`` is given). On any failure nothing is modified.
    """
    if gamestate.get("phase") != "uninitialized" or gamestate.get("player") is not None:
        raise CampaignInitError("Campaign is already initialized (phase is not 'uninitialized').")
    if not world.get("pack", {}).get("burgs"):
        raise CampaignInitError("world.json has no settlements.")
    client = client or GeminiClient()
    _check_cancelled(cancel_event)
    stage_started = time.perf_counter()
    turns = [("user", build_user_prompt(world, gamestate, notes))]
    _diagnostic(progress, f"Campaign prompt prepared in {time.perf_counter() - stage_started:.3f}s.")
    errors, built = ["no attempt made"], None
    for attempt in range(1, max_attempts + 1):
        _check_cancelled(cancel_event)
        _diagnostic(progress, f"Campaign proposal {attempt}/{max_attempts}: requesting Gemini response.")
        stage_started = time.perf_counter()
        try:
            raw = client.generate(SYSTEM_PROMPT, turns)
        finally:
            _diagnostic(progress, f"Campaign proposal {attempt}: API stage ended in {time.perf_counter() - stage_started:.3f}s.")
        _check_cancelled(cancel_event)
        stage_started = time.perf_counter()
        try:
            parsed = parse_model_json(raw)
        except ValueError as error:
            errors, built = [str(error)], None
            _diagnostic(progress, f"Campaign response parsing failed in {time.perf_counter() - stage_started:.3f}s.")
        else:
            _diagnostic(progress, f"Campaign response parsed in {time.perf_counter() - stage_started:.3f}s.")
            stage_started = time.perf_counter()
            try:
                errors, built = validate_and_build(parsed, world, gamestate, notes)
            finally:
                _diagnostic(progress, f"Campaign validation stage ended in {time.perf_counter() - stage_started:.3f}s.")
        if not errors:
            break
        _diagnostic(progress, f"Campaign proposal rejected ({len(errors)} validation problems); requesting correction.")
        turns += [("model", raw), ("user", build_correction_prompt(errors))]
    if errors or built is None:
        raise CampaignInitError(f"Gemini could not produce a valid campaign after {max_attempts} attempts. "
                                f"Last problems: {errors[:5]}")
    _check_cancelled(cancel_event)
    new_world, new_gs, new_notes, message = built
    if directory is not None:
        _diagnostic(progress, "Persisting validated campaign files...")
        stage_started = time.perf_counter()
        directory = Path(directory)
        base = directory / "world.base.json"
        if not base.exists() and (directory / "world.json").exists():
            shutil.copy2(directory / "world.json", base)  # pristine Azgaar world for --reset
        try:
            save_all(directory, {"world.json": (new_world, True), "gamestate.json": (new_gs, False),
                                 "notes.json": (new_notes, False)})
        finally:
            _diagnostic(progress, f"Campaign persistence stage ended in {time.perf_counter() - stage_started:.3f}s.")
    for target, source in ((world, new_world), (gamestate, new_gs), (notes, new_notes)):
        target.clear()
        target.update(source)
    return message


def initialize_campaign_from_directory(directory, client=None, max_attempts=3, reset=False,
                                       progress=None, cancel_event=None) -> str:
    """Load (or create) the three files in a world directory and initialize the campaign."""
    directory = Path(directory)
    world_path = directory / "world.json"
    if reset and (directory / "world.base.json").exists():
        shutil.copy2(directory / "world.base.json", world_path)
    if not world_path.exists():
        raise CampaignInitError(f"{world_path} not found. Run generate_world.py first.")
    _check_cancelled(cancel_event)
    stage_started = time.perf_counter()
    world = read_json(world_path)
    gamestate = initial_gamestate() if reset or not (directory / "gamestate.json").exists() else read_json(directory / "gamestate.json")
    notes = initial_notes() if reset or not (directory / "notes.json").exists() else read_json(directory / "notes.json")
    _diagnostic(progress, f"Campaign input files loaded in {time.perf_counter() - stage_started:.3f}s.")
    return initialize_campaign(world, gamestate, notes, directory=directory, client=client,
                               max_attempts=max_attempts, progress=progress, cancel_event=cancel_event)


def main() -> int:
    parser = argparse.ArgumentParser(description="Initialize a Gemini-written campaign for a generated world.")
    parser.add_argument("directory", type=Path, help="World directory containing world.json")
    parser.add_argument("--model", help=f"Gemini model (default: $GEMINI_MODEL or {DEFAULT_MODEL})")
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--reset", action="store_true", help="Restore world.base.json and re-initialize from scratch")
    args = parser.parse_args()
    try:
        message = initialize_campaign_from_directory(args.directory, GeminiClient(model=args.model),
                                                     max(1, args.attempts), args.reset)
    except (CampaignInitError, OSError, ValueError) as error:
        print(f"Campaign initialization failed (no files changed): {error}", file=sys.stderr)
        return 1
    print("\n" + message + "\n\nPhase: character_creation. Files saved in", args.directory)
    return 0


if __name__ == "__main__":
    sys.exit(main())
