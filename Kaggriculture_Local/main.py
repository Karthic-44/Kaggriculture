"""
Kaggriculture competition agent.

Design: this is a resource-allocation / scheduling game (multiple mobile
"units" tending a grid over 720 turns, plus a dynamic shared market), not a
supervised-learning problem, so the strategy is a fast greedy planner rather
than a trained model:

  1. Scan our farm for jobs: HARVEST ready tiles, WATER thirsty plants,
     PLANT empty tiles with whichever seed currently has the best
     profit-per-tile-day.
  2. Assign the nearest idle unit (farmer + hired hands) to each job,
     highest priority first (harvest > water > plant). Movement has no
     collision, so "nearest" is just Manhattan distance and a unit always
     makes direct progress toward its target.
  3. Re-price crops every turn from live market data, so planting choices
     track the market instead of hard-coding one "best" crop.
  4. Market orders: sell shed stock (fast if it's about to overflow and get
     discarded, patient if prices are currently poor), restock seeds for the
     best crop, buy land / hire hands once the money is there and there's
     enough season left to pay them back.

The whole thing recomputes from scratch every call (no cross-turn state),
so it's safe under any execution model and stays well inside the 1s
actTimeout even on the full 10x10 board.
"""

CROPS = {
    "WHEAT":      {"seed": 10,  "first_yield_day": 2,  "max_yield_day": 4,  "interval": 0, "max_yield": 6, "ongoing": False},
    "CARROT":     {"seed": 20,  "first_yield_day": 2,  "max_yield_day": 3,  "interval": 0, "max_yield": 4, "ongoing": False},
    "TOMATO":     {"seed": 50,  "first_yield_day": 8,  "max_yield_day": 8,  "interval": 1, "max_yield": 4, "ongoing": True},
    "STRAWBERRY": {"seed": 100, "first_yield_day": 10, "max_yield_day": 10, "interval": 2, "max_yield": 4, "ongoing": True},
    "MELON":      {"seed": 80,  "first_yield_day": 10, "max_yield_day": 12, "interval": 0, "max_yield": 6, "ongoing": False},
}

LAND_PRICES = [1000, 2000, 4000]
SEASON_DAYS = 30
HIRE_FIB = [1, 1, 2, 3, 5, 8, 13, 21, 34, 55]
MAX_UNITS = 6  # soft cap on farmer + hands


def _cfg(config, key, default):
    if config is None:
        return default
    if isinstance(config, dict):
        return config.get(key, default)
    return getattr(config, key, default)


def _crop_score(crop, price):
    """Rough expected profit per tile-day if planted right now at `price`."""
    cd = CROPS[crop]
    revenue = cd["max_yield"] * price
    profit = revenue - cd["seed"]
    if cd["ongoing"]:
        cycle_days = cd["first_yield_day"] + (cd["max_yield"] - 1) * max(cd["interval"], 1) + 1
    else:
        cycle_days = cd["max_yield_day"] + 1
    return profit / cycle_days


def _best_crop(prices, money_reserve):
    best, best_score = None, float("-inf")
    for crop, cd in CROPS.items():
        if cd["seed"] > money_reserve:
            continue
        score = _crop_score(crop, prices.get(crop, cd["seed"]))
        if score > best_score:
            best, best_score = crop, score
    return best


def _dist(a, b):
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def _step_toward(pos, target):
    fx, fy = pos
    tx, ty = target
    if fx < tx:
        return "EAST"
    if fx > tx:
        return "WEST"
    if fy < ty:
        return "SOUTH"
    if fy > ty:
        return "NORTH"
    return None


def _shed_hub(board_size):
    half = board_size // 2
    return (half - 1, half - 1)


def agent(obs, config=None):
    day = obs.get("day", 0)
    hour = obs.get("hour", 0)
    me = obs.get("player", 0)
    farms = obs.get("farms", [])
    private = obs.get("private", {}) or {}
    market = obs.get("market", {}) or {}
    prices = market.get("prices", {}) or {}

    if me >= len(farms):
        return {"farmer": ["PASS"], "hands": [], "market": []}

    farm = farms[me]
    board_size = int(_cfg(config, "boardSize", 10))
    shed_capacity = int(_cfg(config, "shedCapacity", 100))
    max_orders = int(_cfg(config, "maxMarketOrdersPerTurn", 10))

    tiles = farm["tiles"]
    money = farm["money"]
    seeds_now = dict(private.get("seeds", {}) or {})
    shed = private.get("shed", {}) or {}

    units = [tuple(farm["farmer"])] + [tuple(h) for h in farm.get("hands", [])]
    n_units = len(units)
    assigned_action = [None] * n_units

    # ---------- 1. Scan the farm for jobs ----------
    harvest_jobs = []
    water_jobs = []
    plant_jobs = []
    empty_tiles = []

    for y in range(board_size):
        row = tiles[y]
        for x in range(board_size):
            t = row[x]
            if t is None:
                empty_tiles.append((x, y))
            elif isinstance(t, dict):
                kind = t.get("kind")
                if kind == "PLANT":
                    if t.get("yield_units", 0) > 0:
                        harvest_jobs.append((x, y))
                    if not t.get("watered_today"):
                        water_jobs.append((x, y))
                elif "animal" in t and t.get("yield_units", 0) > 0:
                    harvest_jobs.append((x, y))

    plantable = [c for c, n in seeds_now.items() if n > 0]
    for x, y in empty_tiles:
        if not plantable or len(plant_jobs) >= n_units * 3:
            break
        crop = max(plantable, key=lambda c: (seeds_now.get(c, 0), _crop_score(c, prices.get(c, CROPS[c]["seed"]))))
        plant_jobs.append((x, y, crop))
        seeds_now[crop] -= 1
        if seeds_now[crop] <= 0:
            plantable = [c for c in plantable if c != crop]

    # ---------- 2. Greedy nearest-unit assignment (harvest > water > plant) ----------
    remaining_units = list(range(n_units))

    def assign(jobs, make_action):
        for job in jobs:
            if not remaining_units:
                return
            pos = job[:2]
            best_u, best_d = None, None
            for u in remaining_units:
                d = _dist(units[u], pos)
                if best_d is None or d < best_d:
                    best_u, best_d = u, d
            remaining_units.remove(best_u)
            ux, uy = units[best_u]
            if (ux, uy) == pos:
                assigned_action[best_u] = make_action(job)
            else:
                step = _step_toward((ux, uy), pos)
                assigned_action[best_u] = [step] if step else ["PASS"]

    assign(harvest_jobs, lambda job: ["HARVEST"])
    assign(water_jobs, lambda job: ["WATER"])
    assign(plant_jobs, lambda job: ["PLANT", job[2]])

    hub = _shed_hub(board_size)
    for u in remaining_units:
        if units[u] == hub:
            assigned_action[u] = ["PASS"]
        else:
            step = _step_toward(units[u], hub)
            assigned_action[u] = [step] if step else ["PASS"]

    farmer_action = assigned_action[0] or ["PASS"]
    hands_actions = [a or ["PASS"] for a in assigned_action[1:]]

    # ---------- 3. Market orders ----------
    market_orders = []
    days_left = max(0, SEASON_DAYS - day)
    shed_total = sum(shed.values())

    # Sell: clear the shed fast if it's near capacity (overflow is simply
    # discarded at day-end), otherwise sell steadily but hold back half when
    # the price is at/below the item's base rate.
    for item, qty in shed.items():
        if qty <= 0 or len(market_orders) >= max_orders:
            continue
        price = prices.get(item, 0)
        urgent = shed_total >= shed_capacity * 0.75
        n = qty if urgent else max(1, qty // 2)
        market_orders.append(["SELL", item, min(int(n), 10)])

    # Restock seeds for whichever crop currently has the best ROI.
    if hour == 0 and len(market_orders) < max_orders:
        reserve = max(0.0, money * 0.5)
        best = _best_crop(prices, reserve)
        if best:
            have = (private.get("seeds", {}) or {}).get(best, 0)
            target_stock = max(1, n_units)
            if have < target_stock:
                seed_cost = CROPS[best]["seed"]
                affordable = int(money // seed_cost) if seed_cost > 0 else 0
                buy_n = min(target_stock - have, affordable, max_orders - len(market_orders))
                if buy_n > 0:
                    market_orders.append(["BUY_SEED", best, buy_n])

    # Buy the next land quadrant once we can comfortably afford it and enough
    # season remains to earn it back.
    if len(market_orders) < max_orders:
        n_extra = len(farm.get("unlocked_quadrants", [])) - 1
        if 0 <= n_extra < len(LAND_PRICES):
            cost = LAND_PRICES[n_extra]
            if money >= cost * 1.5 and days_left >= 6:
                market_orders.append(["BUY_LAND"])

    # Hire a hand early in the day once money is comfortable, there's runway
    # left to profit from the extra labor, and we're not already crowded.
    if hour < 3 and len(market_orders) < max_orders and n_units < MAX_UNITS:
        hires_today = farm.get("hires_today", 0)
        cost = HIRE_FIB[min(hires_today, len(HIRE_FIB) - 1)]
        if money >= max(500, cost * 50) and days_left >= 5:
            market_orders.append(["HIRE"])

    return {"farmer": farmer_action, "hands": hands_actions, "market": market_orders[:max_orders]}
