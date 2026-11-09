"""Real-world scale for kit libraries whose file units are not metres.

Kenney kits each use their own unit (a Mini Characters person is 0.7 units tall, a Blocky Characters
person 2.7). Each calibrated kit names a reference object with a known real size; the factor is
computed from that object's measured size, so it stays right when a kit is updated. Kits without
a trustworthy reference (toys, abstract tiles, stylised pets) stay uncalibrated: their sizes move
to attributes.size_units and the size fields stay empty, so size filters never treat them as metres.
"""
from __future__ import annotations

import fnmatch
import statistics

# kit slug -> (model name pattern, measure, real size in metres, what the reference is)
# measure: height (up axis), length (longest horizontal side)
KENNEY: dict[str, tuple[str, str, float, str]] = {
    "blocky-characters": ("character-*", "height", 1.75, "a person is 1.75 m tall"),
    "building-kit": ("door-rotate-round-a", "height", 2.1, "a door is 2.1 m tall"),
    "car-kit": ("sedan", "length", 4.5, "a sedan is 4.5 m long"),
    "castle-kit": ("door", "height", 2.2, "a castle door is 2.2 m tall"),
    "factory-kit": ("door", "height", 2.1, "a door is 2.1 m tall"),
    "furniture-kit": ("bedSingle", "length", 2.0, "a single bed is 2.0 m long"),
    "graveyard-kit": ("coffin", "length", 2.0, "a coffin is 2.0 m long"),
    "mini-arcade": ("character-*", "height", 1.75, "a person is 1.75 m tall"),
    "mini-arena": ("character-*", "height", 1.75, "a person is 1.75 m tall"),
    "mini-characters": ("character-*", "height", 1.75, "a person is 1.75 m tall"),
    "mini-dungeon": ("character-*", "height", 1.75, "a person is 1.75 m tall"),
    "mini-forest": ("character-*", "height", 1.75, "a person is 1.75 m tall"),
    "mini-shop": ("character-*", "height", 1.75, "a person is 1.75 m tall"),
    "mini-skate": ("character-*", "height", 1.75, "a person is 1.75 m tall"),
    "modular-buildings": ("door-brown-glass", "height", 2.1, "a door is 2.1 m tall"),
    "nature-kit": ("tent_detailedClosed", "height", 1.6, "a tent is 1.6 m tall"),
    "pirate-kit": ("barrel", "height", 0.9, "a barrel is 0.9 m tall"),
    "platformer-kit": ("door-rotate", "height", 2.1, "a door is 2.1 m tall"),
    "prototype-kit": ("door-rotate", "height", 2.1, "a door is 2.1 m tall"),
    "racing-kit": ("raceCarRed", "length", 4.5, "a race car is 4.5 m long"),
    "retro-urban-kit": ("door-type-a", "height", 2.1, "a door is 2.1 m tall"),
    "space-kit": ("astronautA", "height", 1.8, "an astronaut is 1.8 m tall"),
    "space-station-kit": ("door-single", "height", 2.1, "a door is 2.1 m tall"),
    "survival-kit": ("workbench", "height", 0.9, "a workbench is 0.9 m tall"),
    "train-kit": ("train-locomotive-a", "height", 4.0, "a locomotive is 4.0 m tall"),
    "watercraft-kit": ("boat-row-small", "length", 3.0, "a rowing boat is 3.0 m long"),
    "food-kit": ("apple", "height", 0.08, "an apple is 8 cm tall"),
}
# Quaternius: the newer megakits are already in metres (a door is 2.1-2.2 m); older packs are not
# (Animated Men are 4.8 units tall, Animated Woman 0.05). Each entry was checked against a second
# object in the same pack (e.g. Modular Dungeon: barrel 0.9 m makes its door 2.1 m). Packs whose
# objects disagree with each other (Medieval Village: barrel vs houses) stay uncalibrated.
QUATERNIUS: dict[str, tuple[str, str, float, str]] = {
    "animatedman": ("Animated Human", "height", 1.75, "a person is 1.75 m tall"),
    "animatedmen": ("Male_*", "height", 1.75, "a man is 1.75 m tall"),
    "animatedwoman": ("Animated Woman*", "height", 1.7, "a woman is 1.7 m tall"),
    "animatedwomen": ("Female_*", "height", 1.7, "a woman is 1.7 m tall"),
    "animatedzombie": ("Zombie*", "height", 1.75, "a zombie is 1.75 m tall"),
    "backgroundposedhumans": ("Female_LookingUp", "height", 1.7, "a standing woman is 1.7 m tall"),
    "cars": ("Taxi", "length", 4.6, "a taxi is 4.6 m long"),
    "cubeworldkit": ("Character_*", "height", 1.75, "a person is 1.75 m tall"),
    "cyberpunkgamekit": ("Character", "height", 1.75, "a person is 1.75 m tall"),
    "downtowncitymegakit": ("Door_1", "height", 2.2, "a door is 2.2 m tall (kit in metres)"),
    "fantasypropsmegakit": ("Barrel", "height", 0.9, "a barrel is 0.9 m tall (kit in metres)"),
    "farmanimal": ("Cow", "length", 2.4, "a cow is 2.4 m long"),
    "farmbuildings": ("Fence", "height", 1.1, "a farm fence is 1.1 m tall"),
    "furniture": ("Bed", "length", 2.0, "a bed is 2.0 m long"),
    "junkfood": ("Burger", "height", 0.1, "a burger is 10 cm tall"),
    "knightcharacter": ("KnightCharacter", "height", 1.8, "a knight is 1.8 m tall"),
    "medievaldungeon": ("Barrel", "height", 0.9, "a barrel is 0.9 m tall"),
    "medievalvillagemegakit": ("Door_1_Flat", "height", 2.1, "a door is 2.1 m tall (kit in metres)"),
    "medievalweapons": ("Sword", "height", 1.0, "a sword is 1.0 m long (modelled upright)"),
    "modulardungeon": ("Barrel", "height", 0.9, "a barrel is 0.9 m tall"),
    "modularmedievalbuildings": ("Door", "height", 2.1, "a door is 2.1 m tall"),
    "modularplatformer": ("Fence", "height", 1.1, "a fence is 1.1 m tall"),
    "piratekit": ("Characters_Anne", "height", 1.65, "Anne is 1.65 m tall"),
    "publictransport": ("Taxi", "length", 4.6, "a taxi is 4.6 m long"),
    "rpg": ("Sword", "height", 1.0, "a sword is 1.0 m long (modelled upright)"),
    "rpgcharacters": ("*", "height", 1.75, "a person is 1.75 m tall"),
    "scifiessentialskit": ("Gun_Rifle", "length", 1.0, "a rifle is 1.0 m long"),
    "survival": ("Pistol_1", "length", 0.25, "a pistol is 25 cm long"),
    "toonshootergamekit": ("Character_*", "height", 1.8, "a soldier is 1.8 m tall"),
    "ultimateanimatedanimals": ("Cow", "length", 2.4, "a cow is 2.4 m long"),
    "ultimatedanimatedcharacter": ("*", "height", 1.75, "a person is 1.75 m tall"),
    "ultimatefood": ("Apple", "height", 0.08, "an apple is 8 cm tall"),
    "ultimatefurniture": ("Door1", "height", 2.1, "a door is 2.1 m tall"),
    "ultimategun": ("AssaultRifle2_1", "length", 0.9, "an assault rifle is 0.9 m long"),
    "ultimatehomeinterior": ("Bed_Single", "length", 2.0, "a single bed is 2.0 m long"),
    "ultimatemodularcharacters": ("Adventurer", "height", 1.8, "a person is 1.8 m tall (kit in metres)"),
    "ultimatemodularruins": ("Barrel", "height", 0.9, "a barrel is 0.9 m tall"),
    "ultimatemodularwomen": ("Adventurer", "height", 1.75, "a woman is 1.75 m tall (kit in metres)"),
    "ultimateplatformer": ("Character", "height", 1.8, "a person is 1.8 m tall"),
    "ultimaterpg": ("Sword", "height", 1.0, "a sword is 1.0 m long (modelled upright)"),
    "ultimatespacekit": ("House_Door", "height", 2.1, "a door is 2.1 m tall"),
    "ultimatetexturedbuildings": ("Door1", "height", 2.1, "a door is 2.1 m tall"),
    "universalanimationlibrary": ("*", "height", 1.8, "the mannequin is 1.8 m tall (kit in metres)"),
    "universalanimationlibrary2": ("Mannequin_F", "height", 1.8, "the mannequin is 1.8 m tall (kit in metres)"),
    "universalbasecharacters": ("Superhero_Female_FullBody", "height", 1.75, "a person is 1.75 m tall (kit in metres)"),
    "zombieapocalypsekit": ("Characters_Matt", "height", 1.8, "Matt is 1.8 m tall"),
}
# KayKit: about 0.75 m per unit across the Bits packs (a door is 2.8 units, a table 1.0), checked
# against a second object in each pack. City Builder is a miniature town (a sedan is 0.94 units).
# Medieval Hexagon is a board game (a barrel is a fifth of a tree) and Space Base has no object of
# a known size; both stay uncalibrated.
KAYKIT: dict[str, tuple[str, str, float, str]] = {
    "character-pack-adventures": ("Knight", "height", 1.8, "a knight is 1.8 m tall"),
    "character-pack-skeletons": ("Skeleton_Warrior", "height", 1.8, "a skeleton warrior is 1.8 m tall"),
    "city-builder-bits": ("car_sedan", "length", 4.5, "a sedan is 4.5 m long"),
    "dungeon-remastered": ("chair", "height", 0.9, "a chair is 0.9 m tall"),
    "furniture-bits": ("table_medium", "height", 0.75, "a table is 0.75 m tall"),
    "halloween-bits": ("coffin", "length", 2.1, "a coffin is 2.1 m long"),
    "prototype-bits": ("Door_A", "height", 2.1, "a door is 2.1 m tall"),
    "restaurant-bits": ("door_A", "height", 2.1, "a door is 2.1 m tall"),
}
# Polygonal Mind packs come from Unity scenes and are in metres (a bench is 0.55-0.9 m tall).
TABLES = {"kenney": KENNEY, "quaternius": QUATERNIUS, "kaykit": KAYKIT}


def _measure(record: dict, how: str) -> float | None:
    x, y, z = record.get("size_x"), record.get("size_y"), record.get("size_z")
    if how == "height":
        return y
    if how == "length" and x and z:
        return max(x, z)
    return None


def kit_of(record: dict) -> str:
    return record["source_id"].split("/", 1)[0]


def factors(records: list[dict], table: dict[str, tuple[str, str, float, str]]) -> dict[str, tuple[float, str]]:
    """kit slug -> (factor, note) for every kit whose reference object is present."""
    by_kit: dict[str, list[dict]] = {}
    for record in records:
        by_kit.setdefault(kit_of(record), []).append(record)
    out = {}
    for kit, (pattern, how, real, note) in table.items():
        sizes = [m for r in by_kit.get(kit, []) if fnmatch.fnmatchcase(r["source_id"].split("/", 1)[1], pattern)
                 and (m := _measure(r, how))]
        if sizes:
            out[kit] = (real / statistics.median(sizes), note)
    return out


def calibrate(records: list[dict], source: str) -> dict:
    """Scale calibrated kits to metres in place; move uncalibrated sizes aside. Returns a summary."""
    table = TABLES.get(source)
    if table is None:  # libraries whose files are trusted to be in metres
        return {"calibrated": [], "uncalibrated": []}
    found = factors(records, table)
    for record in records:
        kit = kit_of(record)
        attributes = record.setdefault("attributes", {})
        sizes = [record.get(k) for k in ("size_x", "size_y", "size_z")]
        if kit in found:
            factor, note = found[kit]
            for key, value in zip(("size_x", "size_y", "size_z"), sizes):
                record[key] = round(value * factor, 4) if value else None
            record["size_source"] = "measured, calibrated"
            attributes["scale"] = {"factor": round(factor, 4), "reference": note}
        else:
            attributes["size_units"] = sizes if any(sizes) else None
            attributes["scale"] = {"factor": None, "reference": "no real-world reference in this kit; "
                                   "size_units are the file's own units"}
            record["size_x"] = record["size_y"] = record["size_z"] = None
            record["size_source"] = None
    return {"calibrated": sorted(found), "uncalibrated": sorted({kit_of(r) for r in records} - set(found))}
