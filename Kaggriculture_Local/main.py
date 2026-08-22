"""
Kaggriculture agent — v2

A deterministic, market-aware farming agent for the advanced Kaggriculture
competition.

What this version adds on top of the v1 (wheat/crops-only) baseline:
- Fertilizer: buys it, carries it to eligible plants, applies FERTILIZE
  during the bonus window (one-time crops) / before scheduled production
  (ongoing crops), and sells surplus.
- Animals: builds coops/pastures, buys animals, carries and PLACEs them,
  FEEDs and CAREs for them daily, HARVESTs product, COLLECT_FERTILIZERs.
- Shops: a light demand-awareness bonus nudges crop/animal choice toward
  goods that unlocked town shops are consuming (price already captures
  most of this signal; this is a small forward-looking tilt).
- Same conservative, defensive style as v1: never emits malformed actions,
  only acts on unlocked tiles, stays well under the 1s turn budget.

Design notes / assumptions (the spec is silent on a couple of points):
- FEED is assumed to auto-consume WHEAT from the shed (mirrors how seeds
  are "automatically available" to PLANT) rather than requiring a unit to
  physically carry wheat to the animal. We keep a small wheat buffer in
  the shed (grown or bought) so FEED never stalls.
- FERTILIZE is assumed to consume 1 FERTILIZER from the *carrying unit's*
  inventory (mirrors PICKUP/DROP being the mechanism that moves shed
  items into play). Units therefore PICKUP FERTILIZER before fertilizing.
- BUY_ANIMAL deposits the animal into the shed (like BUY_PRODUCT), so a
  unit must PICKUP the animal before it can PLACE it on a coop/pasture.
"""

# ---------------------------------------------------------------------------
# Game data
# ---------------------------------------------------------------------------

CROPS = {
    "WHEAT":      {"seed": 10,  "first_yield_day": 2,  "max_yield_day": 4,  "interval": 0, "max_yield": 6, "ongoing": False},
    "CARROT":     {"seed": 20,  "first_yield_day": 2,  "max_yield_day": 3,  "interval": 0, "max_yield": 4, "ongoing": False},
    "TOMATO":     {"seed": 50,  "first_yield_day": 8,  "max_yield_day": 11, "interval": 1, "max_yield": 4, "ongoing": True},
    "STRAWBERRY": {"seed": 100, "first_yield_day": 10, "max_yield_day": 16, "interval": 2, "max_yield": 4, "ongoing": True},
    "MELON":      {"seed": 80,  "first_yield_day": 10, "max_yield_day": 10, "interval": 0, "max_yield": 6, "ongoing": False},
}

ANIMALS = {
    "GOOSE": {"cost": 300, "structure": "COOP",    "product": "EGG",  "interval": 1, "max_yield": 4, "first_yield_day": 4},
    "COW":   {"cost": 400, "structure": "PASTURE", "product": "MILK", "interval": 2, "max_yield": 6, "first_yield_day": 8},
    "SHEEP": {"cost": 500, "structure": "PASTURE", "product": "WOOL", "interval": 3, "max_yield": 6, "first_yield_day": 6},
}

PRODUCT_TO_ANIMAL = {v["product"]: k for k, v in ANIMALS.items()}

FALLBACK_PRICES = {
    "WHEAT": 25, "CARROT": 35, "TOMATO": 60, "STRAWBERRY": 120, "MELON": 250,
    "EGG": 50, "MILK": 160, "WOOL": 200, "FERTILIZER": 100,
}

# Rough town-shop demand table used only as a small forward-looking tilt on
# top of live prices. Keys are normalized (upper, non-alnum -> '_').
SHOP_DEMAND = {
    "BAKERY":        ["EGG", "WHEAT"],
    "PIZZA_SHOP":     ["MILK", "TOMATO", "WHEAT"],
    "BRUNCH_SPOT":    ["EGG", "WHEAT", "STRAWBERRY"],
    "YARN_STORE":     ["WOOL", "WOOL"],  # 2x wool
    "ICE_CREAM_SHOP": ["STRAWBERRY", "MILK", "WHEAT"],
    "PET_CAFE":       ["CARROT", "CARROT"],  # 2x carrot
    "SMOOTHIE_SHOP":  ["STRAWBERRY", "MILK"],
    "FARMERS_MARKET": ["WHEAT", "CARROT", "TOMATO", "STRAWBERRY"],
}

LAND_PRICES = [1000, 2000, 4000]

SEASON_DAYS = 30
TURNS_PER_DAY = 24
SHED_CAPACITY = 100

FIB = [1, 1, 2, 3, 5, 8, 13, 21, 34, 55]

MAX_HANDS = 5
MAX_ANIMALS = 4          # total live animals we'll ever try to keep
FERTILIZER_RESERVE = 3   # keep this many units of fertilizer on hand, sell rest
WHEAT_FEED_BUFFER = 4    # keep this much wheat in the shed to cover FEED


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------

def _cfg(config, key, default):
    if config is None:
        return default
    if isinstance(config, dict):
        return config.get(key, default)
    return getattr(config, key, default)


def _distance(a, b):
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def _step_toward(pos, target):
    x, y = pos
    tx, ty = target
    if x < tx:
        return "EAST"
    if x > tx:
        return "WEST"
    if y < ty:
        return "SOUTH"
    if y > ty:
        return "NORTH"
    return "PASS"


def _is_locked(tile):
    return tile == "LOCKED"


def _is_plant(tile):
    return isinstance(tile, dict) and tile.get("kind") == "PLANT"


def _is_weed(tile):
    return isinstance(tile, dict) and tile.get("kind") == "WEED"


def _is_structure(tile):
    return isinstance(tile, dict) and tile.get("kind") in ("COOP", "PASTURE")


def _safe_int(value, default=0):
    try:
        return int(value)
    except Exception:
        return default


def _norm(name):
    return "".join(c if c.isalnum() else "_" for c in str(name).upper()).strip("_")


def _shed_targets(board_size):
    h = board_size // 2
    return [(h - 1, h - 1), (h, h - 1), (h - 1, h), (h, h)]


def _is_shed_adjacent(pos, board_size):
    x, y = pos
    h = board_size // 2
    return (x in (h - 1, h)) and (y in (h - 1, h))


def _demand_counts(town):
    """
    Shop names can differ slightly between environment versions.
    Match normalized names exactly first, then by substring.  This keeps
    shop information useful without making the agent depend on one spelling.
    """
    counts = {}
    for raw_shop in (town.get("unlocked_shops", []) or []):
        shop = _norm(raw_shop)
        matched = False
        for key, items in SHOP_DEMAND.items():
            k = _norm(key)
            if shop == k or k in shop or shop in k:
                for item in items:
                    counts[item] = counts.get(item, 0) + 1
                matched = True
                break
        # If a future shop exposes its demanded goods directly, use them.
        if not matched and isinstance(raw_shop, dict):
            for item in raw_shop.get("demand", raw_shop.get("products", [])) or []:
                item = _norm(item)
                if item in CROPS or item in ("EGG", "MILK", "WOOL"):
                    counts[item] = counts.get(item, 0) + 1
    return counts


def _price(prices, item):
    p = prices.get(item, 0)
    if p and p > 0:
        return p
    return FALLBACK_PRICES.get(item, 1)


def _crop_score(crop, prices, demand, day=0, season_days=SEASON_DAYS):
    """
    Estimate profit per tile-day over the remaining season.
    Ongoing crops get credit for repeated harvests; one-shot crops get
    credit only when there is enough time left to finish them.
    """
    c = CROPS[crop]
    price = _price(prices, crop)
    left = max(0, season_days - day)

    if c["ongoing"]:
        first = c["first_yield_day"]
        interval = max(1, c["interval"])
        if left < first:
            return -1e9
        cycles = 1 + (left - first) // interval
        # Last cycle may be harvested near the end; cap is intentionally
        # generous because the environment itself controls yield availability.
        revenue = cycles * c["max_yield"] * price
        effective_days = max(1, first + (cycles - 1) * interval)
    else:
        if left < c["max_yield_day"]:
            return -1e9
        revenue = c["max_yield"] * price
        effective_days = max(1, c["max_yield_day"])

    profit = revenue - c["seed"]
    score = profit / effective_days

    # Shop demand is a small tie-breaker; live market price remains dominant.
    score *= (1.0 + min(0.20, 0.04 * demand.get(crop, 0)))
    return score


def _best_crop(prices, money, demand, candidates=None, day=0, season_days=SEASON_DAYS):
    best, best_score = None, float("-inf")
    for crop, data in CROPS.items():
        if candidates is not None and crop not in candidates:
            continue
        if money < data["seed"]:
            continue
        score = _crop_score(crop, prices, demand, day, season_days)
        if score > best_score:
            best_score, best = score, crop
    return best


def _animal_score(animal, prices, demand, day=0, season_days=SEASON_DAYS):
    a = ANIMALS[animal]
    left = max(0, season_days - day)
    if left < a["first_yield_day"]:
        return -1e9

    product_price = _price(prices, a["product"])
    wheat_cost = _price(prices, "WHEAT")

    # One FEED per day; production repeats after the first yield.
    cycles = 1 + max(0, left - a["first_yield_day"]) // max(1, a["interval"])
    revenue = cycles * a["max_yield"] * product_price
    feed_cost = left * wheat_cost
    setup = a["cost"]

    score = (revenue - feed_cost - setup) / max(1, left)
    score *= (1.0 + min(0.20, 0.04 * demand.get(a["product"], 0)))
    return score


def _best_animal(prices, money, demand, current_count, day, season_days=SEASON_DAYS):
    # Animals are a big up-front commitment (cost + a structure) — only
    # pursue them once the farm has real working capital and has had time
    # to establish a cash-flow crop.
    if current_count >= MAX_ANIMALS or day < 6:
        return None
    best, best_score = None, float("-inf")
    for animal, data in ANIMALS.items():
        if money < data["cost"] + 500:  # keep buffer for the structure/day-to-day
            continue
        score = _animal_score(animal, prices, demand, day, season_days)
        if score > best_score:
            best_score, best = score, animal
    if best_score <= 0:
        return None
    return best


# ---------------------------------------------------------------------------
# Main agent
# ---------------------------------------------------------------------------

def agent(obs, config=None):
    farms = obs.get("farms", [])
    player = _safe_int(obs.get("player", 0))
    day = _safe_int(obs.get("day", 0))
    hour = _safe_int(obs.get("hour", 0))

    if not farms or player < 0 or player >= len(farms):
        return {"farmer": ["PASS"], "hands": [], "market": []}

    farm = farms[player]

    board_size = _safe_int(_cfg(config, "boardSize", 10), 10)
    max_market_orders = _safe_int(_cfg(config, "maxMarketOrdersPerTurn", 10), 10)
    shed_capacity = _safe_int(_cfg(config, "shedCapacity", SHED_CAPACITY), SHED_CAPACITY)

    tiles = farm.get("tiles", [])
    money = float(farm.get("money", 0))

    private = obs.get("private", {}) or {}
    shed = dict(private.get("shed", {}) or {})
    seeds = dict(private.get("seeds", {}) or {})
    inventories = list(private.get("inventories", []) or [])

    market = obs.get("market", {}) or {}
    prices = dict(market.get("prices", {}) or {})

    town = obs.get("town", {}) or {}
    demand = _demand_counts(town)

    farmer_pos = tuple(farm.get("farmer", [0, 0]))
    hands = [tuple(h) for h in (farm.get("hands", []) or [])]
    units = [farmer_pos] + hands
    n_units = len(units)

    height = min(board_size, len(tiles))
    shed_targets = _shed_targets(board_size)

    # -----------------------------------------------------------------
    # Inventory helpers
    # -----------------------------------------------------------------

    SELLABLE = set(CROPS) | {"EGG", "MILK", "WOOL"}

    def inv_of(i):
        if i >= len(inventories):
            return {}
        return inventories[i] or {}

    def sellable_qty(i):
        inv = inv_of(i)
        return sum(max(0, _safe_int(q)) for item, q in inv.items() if item in SELLABLE)

    def fert_qty(i):
        return max(0, _safe_int(inv_of(i).get("FERTILIZER", 0)))

    def carried_animal(i):
        inv = inv_of(i)
        for a in ANIMALS:
            if max(0, _safe_int(inv.get(a, 0))) > 0:
                return a
        return None

    # -----------------------------------------------------------------
    # Scan the board
    # -----------------------------------------------------------------

    harvest_jobs = []       # (pos, yield_units)
    water_jobs = []         # (pos, age)
    weed_jobs = []          # pos
    fertilize_jobs = []     # pos (plants eligible right now)
    free_tiles = []         # pos, empty (buildable / plantable)
    structures = []         # (pos, kind, animal or None)

    for y in range(height):
        row = tiles[y] if y < len(tiles) else []
        width = min(board_size, len(row))
        for x in range(width):
            tile = row[x]
            if _is_locked(tile):
                continue
            pos = (x, y)

            if tile is None:
                free_tiles.append(pos)
                continue

            if _is_plant(tile):
                crop = tile.get("crop")
                yield_units = _safe_int(tile.get("yield_units", 0))
                planted_day = _safe_int(tile.get("planted_day", day))
                age = max(0, day - planted_day)

                if crop in CROPS:
                    cdata = CROPS[crop]
                    if yield_units > 0 and age >= cdata["first_yield_day"]:
                        harvest_jobs.append((pos, yield_units))

                    if not tile.get("watered_today", False):
                        water_jobs.append((pos, age))

                    fertilized_until = _safe_int(tile.get("fertilized_until_day", -1))
                    has_bonus = fertilized_until >= day
                    if tile.get("watered_today", False) and not has_bonus:
                        if cdata["ongoing"]:
                            eligible = True
                        else:
                            half = -(-cdata["max_yield_day"] // 2)  # ceil
                            eligible = half <= age <= cdata["max_yield_day"] + 1
                        if eligible:
                            fertilize_jobs.append(pos)
                continue

            if _is_weed(tile):
                weed_jobs.append(pos)
                continue

            if _is_structure(tile):
                structures.append((pos, tile.get("kind"), tile.get("animal")))
                continue

    animal_feed_jobs = []
    animal_care_jobs = []
    animal_harvest_jobs = []
    animal_fert_jobs = []
    empty_structures = {"COOP": [], "PASTURE": []}

    for pos, kind, animal in structures:
        if animal is None:
            empty_structures[kind].append(pos)
            continue
        # find the raw tile dict again for details
        x, y = pos
        tile = tiles[y][x]
        if not tile.get("fed_today", False):
            animal_feed_jobs.append(pos)
        if not tile.get("cared_today", False):
            animal_care_jobs.append(pos)
        if _safe_int(tile.get("yield_units", 0)) > 0:
            animal_harvest_jobs.append(pos)
        if tile.get("fertilizer_available", False):
            animal_fert_jobs.append(pos)

    # -----------------------------------------------------------------
    # Planning: what to plant / raise / build
    # -----------------------------------------------------------------

    cash_reserve = max(250.0, money * 0.25)
    plant_budget = max(0.0, money - cash_reserve)

    # Diversify plantings instead of dumping every free tile into a single
    # crop: a portion always goes to a fast-cycle crop (wheat/carrot) so the
    # farm keeps steady cash flow while slower, higher-value crops mature.
    plant_jobs = []
    if free_tiles:
        fast_crop = _best_crop(
            prices,
            plant_budget,
            demand,
            candidates=("WHEAT", "CARROT"),
            day=day,
        )
        best_crop = _best_crop(prices, plant_budget, demand, day=day)

        # Early season = cash-flow crops. Mid season = profitable mix.
        # Late season = only crops that can definitely finish before day 30.
        fast_share = 0.75 if day < 8 else (0.40 if day < 18 else 0.15)
        max_new_plants = max(1, n_units * 2)

        if SEASON_DAYS - day <= 5:
            # Avoid planting capital that cannot be harvested in time.
            fast_share = 1.0
        n_total = min(len(free_tiles), max_new_plants)

        remaining_tiles = list(free_tiles[:n_total])

        if fast_crop is not None:
            have_fast = _safe_int(seeds.get(fast_crop, 0))
            n_fast = min(len(remaining_tiles), have_fast, max(1, int(round(n_total * fast_share))))
            for pos in remaining_tiles[:n_fast]:
                plant_jobs.append((pos, fast_crop))
            remaining_tiles = remaining_tiles[n_fast:]

        if best_crop is not None and remaining_tiles:
            have_best = _safe_int(seeds.get(best_crop, 0))
            n_best = min(len(remaining_tiles), have_best)
            for pos in remaining_tiles[:n_best]:
                plant_jobs.append((pos, best_crop))
    else:
        best_crop = _best_crop(prices, plant_budget, demand, day=day)

    total_animals = sum(1 for _, _, a in structures if a is not None)
    want_animal = _best_animal(
        prices, plant_budget, demand, total_animals, day, SEASON_DAYS
    )

    unplaced_animals = {a: max(0, _safe_int(shed.get(a, 0))) for a in ANIMALS}
    need_build = None
    if want_animal is not None:
        astruct = ANIMALS[want_animal]["structure"]
        if not empty_structures[astruct] and unplaced_animals[want_animal] == 0:
            # need a fresh structure before we buy this animal
            need_build = astruct

    # -----------------------------------------------------------------
    # Unit assignment
    # -----------------------------------------------------------------

    actions = [None] * n_units
    remaining_units = list(range(n_units))

    def assign_job(job_pos, action):
        if not remaining_units:
            return False
        unit = min(remaining_units, key=lambda i: _distance(units[i], job_pos))
        remaining_units.remove(unit)
        if units[unit] == job_pos:
            actions[unit] = action
        else:
            actions[unit] = [_step_toward(units[unit], job_pos)]
        return True

    def send_toward_shed(unit):
        pos = units[unit]
        target = min(shed_targets, key=lambda p: _distance(pos, p))
        actions[unit] = ["PASS"] if pos == target else [_step_toward(pos, target)]

    # --- Step A: units carrying a live animal -> deliver to a structure ---
    for i in list(remaining_units):
        animal = carried_animal(i)
        if animal is None:
            continue
        astruct = ANIMALS[animal]["structure"]
        targets = empty_structures.get(astruct, [])
        pos = units[i]
        if targets:
            target = min(targets, key=lambda p: _distance(pos, p))
            if pos == target:
                actions[i] = ["PLACE", animal]
                empty_structures[astruct].remove(target)
            else:
                actions[i] = [_step_toward(pos, target)]
        else:
            # no room yet; stash it back in the shed rather than wander
            if _is_shed_adjacent(pos, board_size):
                actions[i] = ["DROP"]
            else:
                target = min(shed_targets, key=lambda p: _distance(pos, p))
                actions[i] = [_step_toward(pos, target)]
        remaining_units.remove(i)

    # --- Step B: units carrying fertilizer -> deliver to nearest job ---
    open_fert_jobs = list(fertilize_jobs)
    for i in list(remaining_units):
        if fert_qty(i) <= 0 or not open_fert_jobs:
            continue
        pos = units[i]
        target = min(open_fert_jobs, key=lambda p: _distance(pos, p))
        if pos == target:
            actions[i] = ["FERTILIZE"]
            open_fert_jobs.remove(target)
        else:
            actions[i] = [_step_toward(pos, target)]
        remaining_units.remove(i)

    # --- Step C: units carrying sellable produce (or spare fertilizer
    #     with nowhere to go) -> return to shed and DROP ---
    for i in list(remaining_units):
        if sellable_qty(i) <= 0 and fert_qty(i) <= 0:
            continue
        pos = units[i]
        if _is_shed_adjacent(pos, board_size):
            actions[i] = ["DROP"]
        else:
            target = min(shed_targets, key=lambda p: _distance(pos, p))
            actions[i] = [_step_toward(pos, target)]
        remaining_units.remove(i)

    # --- Step D: harvest crops (highest yield first, then nearest) ---
    harvest_jobs.sort(key=lambda j: (-j[1], j[0][1], j[0][0]))
    for pos, _yield in harvest_jobs:
        if not remaining_units:
            break
        assign_job(pos, ["HARVEST"])

    # --- Step E: harvest animal product ---
    for pos in animal_harvest_jobs:
        if not remaining_units:
            break
        assign_job(pos, ["HARVEST"])

    # --- Step F: collect fertilizer from animals ---
    for pos in animal_fert_jobs:
        if not remaining_units:
            break
        assign_job(pos, ["COLLECT_FERTILIZER"])

    # --- Step G: water plants (more important than planting) ---
    water_jobs.sort(key=lambda j: (-j[1], j[0][1], j[0][0]))
    for pos, _age in water_jobs:
        if not remaining_units:
            break
        assign_job(pos, ["WATER"])

    # --- Step H: feed animals ---
    for pos in animal_feed_jobs:
        if not remaining_units:
            break
        assign_job(pos, ["FEED"])

    # --- Step I: care for animals ---
    for pos in animal_care_jobs:
        if not remaining_units:
            break
        assign_job(pos, ["CARE"])

    # --- Step J: kick off fertilizer delivery for any jobs still open ---
    # (pick up fertilizer from the shed; delivery happens next turn via Step B)
    if open_fert_jobs and remaining_units and _safe_int(shed.get("FERTILIZER", 0)) > 0:
        n_fetch = min(len(remaining_units), len(open_fert_jobs), _safe_int(shed.get("FERTILIZER", 0)))
        for _ in range(n_fetch):
            if not remaining_units:
                break
            # nearest free unit to the shed fetches fertilizer
            unit = min(remaining_units, key=lambda i: min(_distance(units[i], t) for t in shed_targets))
            pos = units[unit]
            if _is_shed_adjacent(pos, board_size):
                actions[unit] = ["PICKUP", "FERTILIZER", 1]
            else:
                target = min(shed_targets, key=lambda p: _distance(pos, p))
                actions[unit] = [_step_toward(pos, target)]
            remaining_units.remove(unit)

    # --- Step K: plant crops ---
    for pos, crop in plant_jobs:
        if not remaining_units:
            break
        assign_job(pos, ["PLANT", crop])

    # --- Step L: fetch + place a bought-but-unplaced animal ---
    for animal, qty in unplaced_animals.items():
        if qty <= 0 or not remaining_units:
            continue
        astruct = ANIMALS[animal]["structure"]
        if not empty_structures.get(astruct):
            continue
        unit = min(remaining_units, key=lambda i: min(_distance(units[i], t) for t in shed_targets))
        pos = units[unit]
        if _is_shed_adjacent(pos, board_size):
            actions[unit] = ["PICKUP", animal, 1]
        else:
            target = min(shed_targets, key=lambda p: _distance(pos, p))
            actions[unit] = [_step_toward(pos, target)]
        remaining_units.remove(unit)

    # --- Step M: build a coop/pasture if we're about to want one ---
    if need_build is not None and free_tiles and remaining_units:
        pos = free_tiles[0]
        assign_job(pos, [f"BUILD_{need_build}"])

    # --- Step N: clear weeds with anyone left over ---
    for pos in weed_jobs:
        if not remaining_units:
            break
        assign_job(pos, ["DIG"])

    # --- Step O: idle units drift back toward the shed ---
    for unit in remaining_units:
        send_toward_shed(unit)

    for i in range(n_units):
        if actions[i] is None:
            actions[i] = ["PASS"]

    farmer_action = actions[0]
    hands_actions = actions[1:]

    # -----------------------------------------------------------------
    # Market orders
    # -----------------------------------------------------------------

    market_orders = []

    def add_order(order):
        if len(market_orders) < max_market_orders:
            market_orders.append(order)
            return True
        return False

    # 1. Sell shed inventory (crops, animal products, and surplus fertilizer).
    shed_total = sum(max(0, _safe_int(v)) for v in shed.values())
    sell_items = [
        item for item, qty in shed.items()
        if _safe_int(qty) > 0 and item in (set(CROPS) | {"EGG", "MILK", "WOOL", "FERTILIZER"})
    ]

    # Score by current price first, then shop demand. This avoids selling a
    # shop-linked product at a weak price when a better item is available.
    sell_items.sort(
        key=lambda item: (
            _price(prices, item) * (1.0 + 0.05 * demand.get(item, 0)),
            _price(prices, item),
        ),
        reverse=True,
    )

    for item in sell_items:
        if len(market_orders) >= max_market_orders:
            break
        qty = max(0, _safe_int(shed.get(item, 0)))
        if qty <= 0:
            continue

        if item == "FERTILIZER":
            qty = max(0, qty - FERTILIZER_RESERVE)
            if qty <= 0:
                continue
        if item == "WHEAT":
            # keep a small feed buffer if we have (or want) animals
            if total_animals > 0 or want_animal is not None:
                qty = max(0, qty - WHEAT_FEED_BUFFER)
                if qty <= 0:
                    continue

        price = _price(prices, item)
        if price <= 0:
            continue

        amount = qty if shed_total >= shed_capacity * 0.75 else min(qty, 10)
        add_order(["SELL", item, int(amount)])

    # 2. Buy seeds — both the fast cash-flow crop and the best overall crop,
    #    so Step K always has something to plant on both fronts.
    if hour in (0, 1, 2):
        cash_for_seeds = max(0.0, money - 500.0)
        candidates = []
        fast = _best_crop(prices, cash_for_seeds, demand, candidates=("WHEAT", "CARROT"))
        if fast is not None:
            candidates.append(fast)
        best = _best_crop(prices, cash_for_seeds, demand)
        if best is not None and best not in candidates:
            candidates.append(best)

        for crop in candidates:
            if len(market_orders) >= max_market_orders:
                break
            have = _safe_int(seeds.get(crop, 0))
            target = max(2, n_units * 2)
            if have >= target:
                continue
            seed_price = CROPS[crop]["seed"]
            if seed_price <= 0:
                continue
            affordable = int(cash_for_seeds // seed_price)
            buy_amount = min(target - have, affordable, 5)
            if buy_amount > 0:
                add_order(["BUY_SEED", crop, int(buy_amount)])
                cash_for_seeds = max(0.0, cash_for_seeds - buy_amount * seed_price)

    # 3. Buy fertilizer if we're running low and can afford it.
    if hour in (0, 1, 2):
        have_fert = _safe_int(shed.get("FERTILIZER", 0))
        if have_fert < FERTILIZER_RESERVE and money >= 500 and shed_total < shed_capacity:
            fert_price = _price(prices, "FERTILIZER")
            if fert_price > 0:
                affordable = int(max(0.0, money - 500.0) // fert_price)
                buy_amount = min(FERTILIZER_RESERVE - have_fert, affordable, 3)
                if buy_amount > 0:
                    add_order(["BUY_PRODUCT", "FERTILIZER", int(buy_amount)])

    # 4. Buy wheat to top up the feed buffer if animals are around and we
    #    aren't harvesting enough wheat ourselves.
    if hour in (0, 1, 2) and total_animals > 0:
        have_wheat = _safe_int(shed.get("WHEAT", 0))
        if have_wheat < WHEAT_FEED_BUFFER and money >= 300:
            wheat_price = _price(prices, "WHEAT")
            if wheat_price > 0:
                affordable = int(max(0.0, money - 300.0) // wheat_price)
                buy_amount = min(WHEAT_FEED_BUFFER - have_wheat, affordable, 4)
                if buy_amount > 0:
                    add_order(["BUY_PRODUCT", "WHEAT", int(buy_amount)])

    # 5. Buy an animal once we know it has (or will soon have) a home.
    if hour in (0, 1, 2) and want_animal is not None and need_build is None:
        adata = ANIMALS[want_animal]
        if money >= adata["cost"] + 300:
            add_order(["BUY_ANIMAL", want_animal, 1])

    # End-game: once there are <= 3 days left, stop buying long-horizon
    # assets and liquidate accumulated output. This prevents dead capital.
    days_left = max(0, SEASON_DAYS - day)
    if days_left <= 3:
        for item, qty in sorted(
            shed.items(), key=lambda kv: _price(prices, kv[0]), reverse=True
        ):
            if len(market_orders) >= max_market_orders:
                break
            q = _safe_int(qty)
            if q <= 0:
                continue
            if item == "FERTILIZER":
                continue
            if item == "WHEAT" and total_animals > 0:
                q = max(0, q - WHEAT_FEED_BUFFER)
            if q > 0:
                add_order(["SELL", item, int(q)])

    # 6. Land expansion — one quadrant at a time, preserve working capital.
    unlocked = list(farm.get("unlocked_quadrants", []) or [])
    extra_quadrants = max(0, len(unlocked) - 1)
    if (
        extra_quadrants < len(LAND_PRICES)
        and days_left >= 12
        and money >= LAND_PRICES[extra_quadrants] + 1000
    ):
        add_order(["BUY_LAND"])

    # 7. Hire a hand if there's plenty of cash and plenty of season left.
    current_hands = len(hands)
    if current_hands < MAX_HANDS and hour <= 2 and days_left >= 5:
        hires_today = _safe_int(farm.get("hires_today", 0))
        hire_index = min(hires_today, len(FIB) - 1)
        hire_cost = FIB[hire_index]

        # Estimate today's actionable workload. A hand is useful only when
        # there are enough jobs to keep it busy.
        workload = (
            len(harvest_jobs) + len(water_jobs) + len(animal_feed_jobs)
            + len(animal_care_jobs) + len(animal_harvest_jobs)
            + len(fertilize_jobs) + len(plant_jobs) + len(weed_jobs)
        )
        useful = workload >= max(3, (n_units + 1) * 2)

        if useful and money >= max(250.0, hire_cost * 8 + 150.0):
            add_order(["HIRE"])

    market_orders = market_orders[:max_market_orders]

    return {
        "farmer": farmer_action,
        "hands": hands_actions,
        "market": market_orders,
    }