"""
challenges.py — Claim verification challenges.

Every generator returns (question, correct_answer, shuffled_choices).
Logic is unchanged from v1 so servers see the same difficulty they set.
"""
import random

# ---------------------------------------------------------------------------
# Verification mode registry
# ---------------------------------------------------------------------------

VERIFICATION_MODES: dict[str, str] = {
    "easy":                 "Easy",
    "medium":               "Medium",
    "hard":                 "Hard",
    "difficulty_over_time": "Difficulty Over Time",
    "periodic":             "Periodic Table",
    "random":               "Random",
}

# Periodic table dataset — (symbol, element name)
PERIODIC_TABLE: list[tuple[str, str]] = [
    ("H",  "Hydrogen"),   ("He", "Helium"),     ("Li", "Lithium"),
    ("Be", "Beryllium"),  ("B",  "Boron"),       ("C",  "Carbon"),
    ("N",  "Nitrogen"),   ("O",  "Oxygen"),      ("F",  "Fluorine"),
    ("Ne", "Neon"),       ("Na", "Sodium"),      ("Mg", "Magnesium"),
    ("Al", "Aluminum"),   ("Si", "Silicon"),     ("P",  "Phosphorus"),
    ("S",  "Sulfur"),     ("Cl", "Chlorine"),    ("Ar", "Argon"),
    ("K",  "Potassium"),  ("Ca", "Calcium"),     ("Fe", "Iron"),
    ("Cu", "Copper"),     ("Zn", "Zinc"),        ("Ag", "Silver"),
    ("Au", "Gold"),       ("Hg", "Mercury"),     ("Pb", "Lead"),
    ("Sn", "Tin"),        ("Mn", "Manganese"),   ("Ni", "Nickel"),
    ("Co", "Cobalt"),     ("Cr", "Chromium"),    ("Ti", "Titanium"),
    ("Br", "Bromine"),    ("I",  "Iodine"),      ("Ba", "Barium"),
    ("Pt", "Platinum"),   ("W",  "Tungsten"),    ("Xe", "Xenon"),
    ("Ra", "Radium"),
]

# Difficulty Over Time configuration
DOT_TIERS    = ["hard", "hard_medium", "medium", "medium_easy", "easy"]
DOT_DROPOFF  = 0.75   # Each tier is 75% of the previous duration



def _numeric_decoys(correct: int, lo: int, hi: int, count: int = 3) -> list[int]:
    """Generate plausible wrong answers near correct within [lo, hi]."""
    pool: set[int] = set()
    offsets = [o for o in range(-20, 21) if o != 0]
    random.shuffle(offsets)
    for o in offsets:
        d = correct + o
        if lo <= d <= hi and d != correct:
            pool.add(d)
        if len(pool) >= count:
            break
    while len(pool) < count:
        d = random.randint(lo, hi)
        if d != correct:
            pool.add(d)
    return list(pool)[:count]


def generate_easy_challenge() -> tuple[str, str, list[str]]:
    """Simple addition or subtraction — answer is always 1–9."""
    while True:
        op = random.choice(["add", "sub"])
        if op == "add":
            a = random.randint(1, 8)
            b = random.randint(1, 9 - a)
            answer = a + b
            question = f"{a} + {b}"
        else:
            a = random.randint(2, 9)
            b = random.randint(1, a - 1)
            answer = a - b
            question = f"{a} − {b}"
        if 1 <= answer <= 9:
            pool = [n for n in range(1, 10) if n != answer]
            choices = [str(answer)] + [str(d) for d in random.sample(pool, 3)]
            random.shuffle(choices)
            return question, str(answer), choices


def generate_medium_easy_challenge() -> tuple[str, str, list[str]]:
    """Single operation — answer is 10–49. DOT intermediate tier."""
    for _ in range(50):
        op = random.choice(["add", "sub"])
        if op == "add":
            a = random.randint(5, 40)
            b = random.randint(5, 49 - a)
            ans = a + b
            if 10 <= ans <= 49:
                q = f"{a} + {b}"
                choices = [str(ans)] + [str(d) for d in _numeric_decoys(ans, 10, 49)]
                random.shuffle(choices)
                return q, str(ans), choices
        else:
            a = random.randint(15, 70)
            b = random.randint(1, a - 10)
            ans = a - b
            if 10 <= ans <= 49:
                q = f"{a} − {b}"
                choices = [str(ans)] + [str(d) for d in _numeric_decoys(ans, 10, 49)]
                random.shuffle(choices)
                return q, str(ans), choices
    return generate_easy_challenge()


def generate_medium_challenge() -> tuple[str, str, list[str]]:
    """PEMDAS expression — integer answer 10–99."""
    for _ in range(100):
        pattern = random.choice(["add_mul", "mul_sub", "paren_add_mul", "two_products"])
        ans, q = None, None
        if pattern == "add_mul":
            a, b, c = random.randint(1, 15), random.randint(2, 9), random.randint(2, 9)
            ans, q = a + b * c, f"{a} + {b} × {c}"
        elif pattern == "mul_sub":
            a, b = random.randint(2, 9), random.randint(2, 9)
            c = random.randint(1, 30)
            ans, q = a * b - c, f"{a} × {b} − {c}"
        elif pattern == "paren_add_mul":
            a, b, c = random.randint(1, 9), random.randint(1, 9), random.randint(2, 9)
            ans, q = (a + b) * c, f"({a} + {b}) × {c}"
        elif pattern == "two_products":
            a, b = random.randint(2, 7), random.randint(2, 7)
            c, d = random.randint(2, 7), random.randint(2, 7)
            ans, q = a * b + c * d, f"{a} × {b} + {c} × {d}"
        if ans is not None and 10 <= ans <= 99:
            choices = [str(ans)] + [str(d) for d in _numeric_decoys(ans, 10, 99)]
            random.shuffle(choices)
            return q, str(ans), choices
    return generate_easy_challenge()


def generate_hard_medium_challenge() -> tuple[str, str, list[str]]:
    """Multi-step PEMDAS with nested parentheses — answer 10–99. DOT intermediate tier."""
    for _ in range(100):
        pattern = random.choice(["nested", "two_paren", "paren_sub_mul"])
        ans, q = None, None
        if pattern == "nested":
            a, b, c, d = (random.randint(1, 9), random.randint(2, 9),
                          random.randint(2, 9), random.randint(1, 15))
            ans, q = (a + b * c) - d, f"({a} + {b} × {c}) − {d}"
        elif pattern == "two_paren":
            a, b = random.randint(2, 7), random.randint(1, 7)
            c, d = random.randint(2, 7), random.randint(1, 7)
            e = random.randint(2, 5)
            ans, q = (a + b) * e - c * d, f"({a} + {b}) × {e} − {c} × {d}"
        elif pattern == "paren_sub_mul":
            a, b, c = random.randint(5, 15), random.randint(1, 5), random.randint(2, 8)
            ans, q = (a - b) * c, f"({a} − {b}) × {c}"
        if ans is not None and 10 <= ans <= 99:
            choices = [str(ans)] + [str(d) for d in _numeric_decoys(ans, 10, 99)]
            random.shuffle(choices)
            return q, str(ans), choices
    return generate_medium_challenge()


def generate_hard_challenge() -> tuple[str, str, list[str]]:
    """Algebraic equation — solve for x. Answer is 1–50."""
    for _ in range(100):
        form = random.choice(["linear_add", "linear_sub", "div_add", "combined"])
        ans, q = None, None
        if form == "linear_add":
            x = random.randint(1, 15)
            a, b = random.randint(2, 9), random.randint(1, 20)
            c = a * x + b
            ans, q = x, f"{a}x + {b} = {c}, solve for x"
        elif form == "linear_sub":
            x = random.randint(1, 15)
            a, b = random.randint(2, 9), random.randint(1, 20)
            c = a * x - b
            if c < 1:
                continue
            ans, q = x, f"{a}x − {b} = {c}, solve for x"
        elif form == "div_add":
            a = random.randint(2, 5)
            x_div = random.randint(1, 10)
            x = x_div * a
            b = random.randint(1, 10)
            c = x_div + b
            ans, q = x, f"x ÷ {a} + {b} = {c}, solve for x"
        elif form == "combined":
            x = random.randint(1, 12)
            a, b = random.randint(2, 8), random.randint(2, 8)
            c = (a + b) * x
            ans, q = x, f"{a}x + {b}x = {c}, solve for x"
        if ans is not None and 1 <= ans <= 50:
            choices = [str(ans)] + [str(d) for d in _numeric_decoys(ans, 1, 50)]
            random.shuffle(choices)
            return q, str(ans), choices
    return generate_medium_challenge()


def generate_periodic_challenge() -> tuple[str, str, list[str]]:
    """Periodic table — identify the symbol for a named element."""
    correct_sym, correct_name = random.choice(PERIODIC_TABLE)
    pool = [(sym, name) for sym, name in PERIODIC_TABLE if sym != correct_sym]
    decoys = [sym for sym, _ in random.sample(pool, 3)]
    choices = [correct_sym] + decoys
    random.shuffle(choices)
    q = f"Which symbol represents the element **{correct_name}**?"
    return q, correct_sym, choices


# ---------------------------------------------------------------------------
# Difficulty Over Time helpers
# ---------------------------------------------------------------------------

def _dot_tier_durations(start_seconds: int) -> list[float | None]:
    """
    Returns per-tier durations for the 5 DOT tiers.
    The final entry is None — Easy is the floor and holds indefinitely.
    """
    durations: list[float | None] = []
    t = float(start_seconds)
    for _ in range(4):          # Hard, Hard-Medium, Medium, Medium-Easy
        durations.append(t)
        t = t * DOT_DROPOFF
    durations.append(None)      # Easy floor
    return durations


def _dot_tier_for_elapsed(elapsed_seconds: float, start_seconds: int) -> str:
    """Return the active DOT tier name given elapsed seconds since the drop was posted."""
    durations = _dot_tier_durations(start_seconds)
    accumulated = 0.0
    for i, dur in enumerate(durations):
        if dur is None:
            return DOT_TIERS[i]   # Easy, holds indefinitely
        accumulated += dur
        if elapsed_seconds < accumulated:
            return DOT_TIERS[i]
    return "easy"


def _generate_for_tier(tier: str) -> tuple[str, str, list[str]]:
    if tier == "hard":
        return generate_hard_challenge()
    elif tier == "hard_medium":
        return generate_hard_medium_challenge()
    elif tier == "medium":
        return generate_medium_challenge()
    elif tier == "medium_easy":
        return generate_medium_easy_challenge()
    else:
        return generate_easy_challenge()


def generate_challenge(
    mode: str,
    elapsed_seconds: float = 0.0,
    dot_start_seconds: int = 60,
) -> tuple[str, str, list[str]]:
    """
    Generate a (question, correct_answer, shuffled_choices) tuple
    for the given verification mode.
    """
    if mode == "easy":
        return generate_easy_challenge()
    elif mode == "medium":
        return generate_medium_challenge()
    elif mode == "hard":
        return generate_hard_challenge()
    elif mode == "difficulty_over_time":
        tier = _dot_tier_for_elapsed(elapsed_seconds, dot_start_seconds)
        return _generate_for_tier(tier)
    elif mode == "periodic":
        return generate_periodic_challenge()
    elif mode == "random":
        picked = random.choice(["easy", "medium", "hard", "periodic"])
        return generate_challenge(picked)
    else:
        return generate_easy_challenge()



DOT_TIER_LABELS = {
    "hard":        "Hard",
    "hard_medium": "Hard to Medium",
    "medium":      "Medium",
    "medium_easy": "Medium to Easy",
    "easy":        "Easy",
}

MODE_DESCRIPTIONS = {
    "easy":                 "Simple addition or subtraction, answer under 10",
    "medium":               "Order of operations, answer under 100",
    "hard":                 "Solve for x",
    "difficulty_over_time": "Starts Hard, gets easier the longer a drop sits",
    "periodic":             "Pick the symbol for a named element",
    "random":               "A different mode for every claim",
}
