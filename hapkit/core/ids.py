"""android.hardware.vibrator Effect and CompositePrimitive ids."""

EFFECTS = {
    "CLICK": 0,
    "DOUBLE_CLICK": 1,
    "TICK": 2,
    "THUD": 3,
    "POP": 4,
    "HEAVY_CLICK": 5,
    **{f"RINGTONE_{i}": 5 + i for i in range(1, 16)},
    "TEXTURE_TICK": 21,
}

PRIMITIVES = {
    "NOOP": 0,
    "CLICK": 1,
    "THUD": 2,
    "SPIN": 3,
    "QUICK_RISE": 4,
    "SLOW_RISE": 5,
    "QUICK_FALL": 6,
    "LIGHT_TICK": 7,
    "LOW_TICK": 8,
}

# required when CAP_COMPOSE_EFFECTS is reported (CompositePrimitive.aidl)
REQUIRED_PRIMITIVES = ("CLICK", "QUICK_RISE", "SLOW_RISE", "QUICK_FALL", "LIGHT_TICK", "LOW_TICK")

EFFECT_NAMES = {v: k for k, v in EFFECTS.items()}
PRIMITIVE_NAMES = {v: k for k, v in PRIMITIVES.items()}


def name_of(kind: str, aidl: int) -> str:
    table = EFFECT_NAMES if kind == "effect" else PRIMITIVE_NAMES
    return table.get(aidl, f"{kind}_{aidl}")


def id_of(kind: str, name: str) -> int:
    table = EFFECTS if kind == "effect" else PRIMITIVES
    return table[name.upper()]
