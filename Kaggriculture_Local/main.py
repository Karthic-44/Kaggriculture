
"""
Kaggriculture agent.

A deterministic, market-aware farming agent for the advanced Kaggriculture
competition.

Main goals:
- Never issue malformed actions.
- Work only on unlocked tiles.
- Correctly handle farmer/hand inventories and the shed.
- Water crops every day.
- Harvest ready crops before they decay.
- Plant profitable crops using live market prices.
- Sell produce after it reaches the shed.
- Expand land and hire hands conservatively.
- Stay fast enough for the 1-second action limit.
"""

# ---------------------------------------------------------------------------
# Game data from the Kaggriculture environment
# ---------------------------------------------------------------------------

CROPS = {
    "WHEAT": {
        "seed": 10,
        "first_yield_day": 2,
        "max_yield_day": 4,
        "interval": 0,
        "max_yield": 6,
        "ongoing": False,
    },
    "CARROT": {
        "seed": 20,
        "first_yield_day": 2,
        "max_yield_day": 3,
        "interval": 0,
        "max_yield": 4,
        "ongoing": False,
    },
    "TOMATO": {
        "seed": 50,
        "first_yield_day": 8,
        "max_yield_day": 8,
        "interval": 1,
        "max_yield": 4,
        "ongoing": True,
    },
    "STRAWBERRY": {
        "seed": 100,
        "first_yield_day": 10,
        "max_yield_day": 10,
        "interval": 2,
        "max_yield": 4,
        "ongoing": True,
    },
    "MELON": {
        "seed": 80,
        "first_yield_day": 10,
        "max_yield_day": 12,
        "interval": 0,
        "max_yield": 6,
        "ongoing": False,
    },
}

LAND_PRICES = [1000, 2000, 4000]

SEASON_DAYS = 30
TURNS_PER_DAY = 24
SHED_CAPACITY = 100

# Default farm-hand costs follow Fibonacci numbers.
FIB = [1, 1, 2, 3, 5, 8, 13, 21, 34, 55]

# Keep this deliberately conservative.
MAX_HANDS = 5


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
    """Return one legal movement step toward target."""
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


def _is_animal_tile(tile):
    return (
        isinstance(tile, dict)
        and tile.get("kind") in ("COOP", "PASTURE")
        and tile.get("animal") is not None
    )


def _is_shed_adjacent(pos, board_size):
    """
    Official shed-access tiles for a 10x10 board are:
        (4,4), (5,4), (4,5), (5,5)

    Generalize this for other even board sizes.
    """
    x, y = pos
    h = board_size // 2

    return (x in (h - 1, h)) and (y in (h - 1, h))


def _unlocked(tile):
    return tile != "LOCKED"


def _crop_score(crop, price):
    """
    Approximate profit per tile-day.

    This is intentionally simple. The actual market price is dynamic, so
    live price is more important than a hard-coded ranking.
    """
    c = CROPS[crop]

    revenue = c["max_yield"] * max(1, price)
    profit = revenue - c["seed"]

    if c["ongoing"]:
        # Approximate time until the first useful production cycle.
        cycle_days = c["first_yield_day"] + 1
    else:
        cycle_days = c["max_yield_day"] + 1

    return profit / max(1, cycle_days)


def _best_crop(prices, money):
    best = None
    best_score = float("-inf")

    for crop, data in CROPS.items():
        if money < data["seed"]:
            continue

        price = prices.get(crop, 0)

        if price <= 0:
            # Fall back to a conservative base-price estimate.
            fallback = {
                "WHEAT": 25,
                "CARROT": 35,
                "TOMATO": 60,
                "STRAWBERRY": 120,
                "MELON": 250,
            }
            price = fallback[crop]

        score = _crop_score(crop, price)

        if score > best_score:
            best_score = score
            best = crop

    return best


def _safe_int(value, default=0):
    try:
        return int(value)
    except Exception:
        return default


# ---------------------------------------------------------------------------
# Main agent
# ---------------------------------------------------------------------------

def agent(obs, config=None):
    # -----------------------------------------------------------------------
    # Basic observation extraction
    # -----------------------------------------------------------------------

    farms = obs.get("farms", [])
    player = _safe_int(obs.get("player", 0))
    day = _safe_int(obs.get("day", 0))
    hour = _safe_int(obs.get("hour", 0))

    if not farms or player < 0 or player >= len(farms):
        return {
            "farmer": ["PASS"],
            "hands": [],
            "market": [],
        }

    farm = farms[player]

    board_size = _safe_int(
        _cfg(config, "boardSize", 10),
        10,
    )

    max_market_orders = _safe_int(
        _cfg(config, "maxMarketOrdersPerTurn", 10),
        10,
    )

    shed_capacity = _safe_int(
        _cfg(config, "shedCapacity", SHED_CAPACITY),
        SHED_CAPACITY,
    )

    tiles = farm.get("tiles", [])
    money = float(farm.get("money", 0))

    private = obs.get("private", {}) or {}
    shed = dict(private.get("shed", {}) or {})
    seeds = dict(private.get("seeds", {}) or {})
    inventories = list(private.get("inventories", []) or [])

    market = obs.get("market", {}) or {}
    prices = dict(market.get("prices", {}) or {})

    farmer_pos = tuple(farm.get("farmer", [0, 0]))

    hands = [
        tuple(h)
        for h in (farm.get("hands", []) or [])
    ]

    units = [farmer_pos] + hands
    n_units = len(units)

    # -----------------------------------------------------------------------
    # Defensive dimensions
    # -----------------------------------------------------------------------

    height = min(board_size, len(tiles))

    # -----------------------------------------------------------------------
    # Inventory awareness
    #
    # Harvested items go into the acting unit's inventory.
    # Therefore a good agent needs to periodically return to the shed and
    # DROP its inventory before SELL orders can make any money.
    # -----------------------------------------------------------------------

    def inventory_total(unit_index):
        if unit_index >= len(inventories):
            return 0

        inv = inventories[unit_index] or {}

        total = 0
        for item, qty in inv.items():
            if item in CROPS or item in (
                "EGG",
                "MILK",
                "WOOL",
                "FERTILIZER",
            ):
                total += max(0, _safe_int(qty))

        return total

    unit_inventory = [
        inventory_total(i)
        for i in range(n_units)
    ]

    # -----------------------------------------------------------------------
    # Find all useful jobs.
    #
    # IMPORTANT:
    # - only unlocked tiles can be acted upon
    # - plant tiles are identified by kind == "PLANT"
    # - weeds should be cleared
    # -----------------------------------------------------------------------

    harvest_jobs = []
    water_jobs = []
    plant_jobs = []
    weed_jobs = []

    for y in range(height):
        row = tiles[y]

        width = min(board_size, len(row))

        for x in range(width):
            tile = row[x]

            if _is_locked(tile):
                continue

            pos = (x, y)

            if _is_plant(tile):
                crop = tile.get("crop")
                yield_units = _safe_int(tile.get("yield_units", 0))
                planted_day = _safe_int(tile.get("planted_day", day))

                age = max(0, day - planted_day)

                # Harvest whenever there is usable yield and the crop is
                # mature enough.
                if yield_units > 0 and crop in CROPS:
                    crop_data = CROPS[crop]

                    if age >= crop_data["first_yield_day"]:
                        harvest_jobs.append(
                            (
                                pos,
                                yield_units,
                                crop,
                            )
                        )

                # Plants need daily watering.
                if not tile.get("watered_today", False):
                    water_jobs.append(
                        (
                            pos,
                            age,
                            crop,
                        )
                    )

            elif _is_weed(tile):
                weed_jobs.append(pos)

    # -----------------------------------------------------------------------
    # Decide what to plant.
    #
    # We don't blindly fill the entire board. We reserve enough workers for
    # watering/harvesting and plant only a small number of tiles per turn.
    # -----------------------------------------------------------------------

    free_tiles = []

    for y in range(height):
        row = tiles[y]

        width = min(board_size, len(row))

        for x in range(width):
            tile = row[x]

            if tile is None:
                free_tiles.append((x, y))

    # Estimate a sensible number of planting jobs.
    max_new_plants = max(2,n_units * 4)
    # Keep some cash for emergency seeds / land / hires.
    cash_reserve = max(250.0, money * 0.25)

    best_crop = _best_crop(
        prices,
        max(0.0, money - cash_reserve),
    )

    if best_crop is not None:
        available_seed_count = _safe_int(
            seeds.get(best_crop, 0)
        )

        # Do not consume every available seed in one turn.
        number_to_plant = min(
            len(free_tiles),
            available_seed_count,
            max_new_plants,
        )

        for pos in free_tiles[:number_to_plant]:
            plant_jobs.append(
                (
                    pos,
                    best_crop,
                )
            )

    # -----------------------------------------------------------------------
    # Unit assignment
    # -----------------------------------------------------------------------

    actions = [None] * n_units
    remaining_units = list(range(n_units))

    def assign_job(job_pos, action):
        if not remaining_units:
            return False

        # Nearest currently-unassigned worker.
        unit = min(
            remaining_units,
            key=lambda i: _distance(units[i], job_pos),
        )

        remaining_units.remove(unit)

        if units[unit] == job_pos:
            actions[unit] = action
        else:
            actions[unit] = [
                _step_toward(units[unit], job_pos)
            ]

        return True

    # -----------------------------------------------------------------------
    # 1. Emergency logistics:
    # If a worker is carrying products, return it to the shed.
    #
    # We do this before farming jobs because otherwise the worker could spend
    # many turns harvesting while carrying everything and never bank it.
    # -----------------------------------------------------------------------

    logistics_units = [
        i for i in range(n_units)
        if unit_inventory[i] > 0
    ]

    for i in sorted(
        logistics_units,
        key=lambda u: unit_inventory[u],
        reverse=True,
    ):
        if i not in remaining_units:
            continue

        pos = units[i]

        if _is_shed_adjacent(pos, board_size):
            actions[i] = ["DROP"]
            remaining_units.remove(i)
        else:
            # Send the worker toward the nearest shed-access tile.
            h = board_size // 2

            shed_targets = [
                (h - 1, h - 1),
                (h, h - 1),
                (h - 1, h),
                (h, h),
            ]

            target = min(
                shed_targets,
                key=lambda p: _distance(pos, p),
            )

            actions[i] = [
                _step_toward(pos, target)
            ]

            remaining_units.remove(i)

    # -----------------------------------------------------------------------
    # 2. Harvest
    # -----------------------------------------------------------------------

    # Highest yield first, then nearest.
    harvest_jobs.sort(
        key=lambda j: (-j[1], j[0][1], j[0][0])
    )

    for pos, yield_units, crop in harvest_jobs:
        if not remaining_units:
            break

        assign_job(
            pos,
            ["HARVEST"],
        )

    # -----------------------------------------------------------------------
    # 3. Water
    #
    # Watering is more important than planting. A dead plant is lost capital.
    # -----------------------------------------------------------------------

    water_jobs.sort(
        key=lambda j: (
            -j[1],
            j[0][1],
            j[0][0],
        )
    )

    for pos, age, crop in water_jobs:
        if not remaining_units:
            break

        assign_job(
            pos,
            ["WATER"],
        )

    # -----------------------------------------------------------------------
    # 4. Plant
    # -----------------------------------------------------------------------

    for pos, crop in plant_jobs:
        if not remaining_units:
            break

        assign_job(
            pos,
            ["PLANT", crop],
        )

    # -----------------------------------------------------------------------
    # 5. Clear weeds if there is nobody else to use.
    # -----------------------------------------------------------------------

    for pos in weed_jobs:
        if not remaining_units:
            break

        assign_job(
            pos,
            ["DIG"],
        )

    # -----------------------------------------------------------------------
    # 6. Idle workers return toward the shed.
    # -----------------------------------------------------------------------

    h = board_size // 2

    shed_targets = [
        (h - 1, h - 1),
        (h, h - 1),
        (h - 1, h),
        (h, h),
    ]

    for unit in remaining_units:
        pos = units[unit]

        target = min(
            shed_targets,
            key=lambda p: _distance(pos, p),
        )

        if _is_shed_adjacent(pos, board_size):
            actions[unit] = ["PASS"]
        else:
            actions[unit] = [
                _step_toward(pos, target)
            ]

    # -----------------------------------------------------------------------
    # Defensive fallback.
    # -----------------------------------------------------------------------

    for i in range(n_units):
        if actions[i] is None:
            actions[i] = ["PASS"]

    farmer_action = actions[0]

    hands_actions = actions[1:]

    # -----------------------------------------------------------------------
    # MARKET
    # -----------------------------------------------------------------------

    market_orders = []

    def add_order(order):
        if len(market_orders) < max_market_orders:
            market_orders.append(order)
            return True
        return False

    # -----------------------------------------------------------------------
    # 1. Sell shed inventory.
    #
    # Do not sell seeds; seeds are not part of shed inventory.
    # Fertilizer is not sellable.
    # -----------------------------------------------------------------------

    sell_items = [
        item
        for item, qty in shed.items()
        if _safe_int(qty) > 0
        and item != "FERTILIZER"
    ]

    # Prefer items with stronger current prices.
    sell_items.sort(
        key=lambda item: prices.get(item, 0),
        reverse=True,
    )

    shed_total = sum(
        max(0, _safe_int(v))
        for v in shed.values()
    )

    for item in sell_items:
        if len(market_orders) >= max_market_orders:
            break

        qty = max(0, _safe_int(shed.get(item, 0)))

        if qty <= 0:
            continue

        price = prices.get(item, 0)

        # If the shed is getting full, liquidate aggressively.
        if shed_total >= shed_capacity * 0.75:
            amount = qty
        else:
            # Sell a moderate amount each turn rather than flooding the
            # market unnecessarily.
            amount = min(qty, 10)

        if price > 0:
            add_order(
                ["SELL", item, int(amount)]
            )

    # -----------------------------------------------------------------------
    # 2. Buy seeds.
    #
    # We only buy when current seed inventory is low.
    # -----------------------------------------------------------------------

    if hour in (0, 1, 2):
        cash_for_seeds = max(
            0.0,
            money - 500.0,
        )

        crop = _best_crop(
            prices,
            cash_for_seeds,
        )

        if crop is not None:
            have = _safe_int(seeds.get(crop, 0))

            # Target a modest rolling inventory.
            target = max(
                4,
                n_units * 5,
            )

            if have < target:
                seed_price = CROPS[crop]["seed"]

                if seed_price > 0:
                    affordable = int(
                        cash_for_seeds // seed_price
                    )

                    buy_amount = min(
                        target - have,
                        affordable,
                        15,
                    )

                    if buy_amount > 0:
                        add_order(
                            [
                                "BUY_SEED",
                                crop,
                                int(buy_amount),
                            ]
                        )

     # -----------------------------------------------------------------------
    # 3. SMART LAND EXPANSION
    #
    # Land is an investment, not automatically a profit.
    # Only expand when existing land is being heavily utilized.
    # -----------------------------------------------------------------------

    unlocked = list(
        farm.get("unlocked_quadrants", []) or []
    )

    # Number of currently unlocked quadrants.
    unlocked_count = len(unlocked)

    # Count usable and occupied tiles.
    usable_tiles = 0
    occupied_tiles = 0

    for y in range(height):
        row = tiles[y]
        width = min(board_size, len(row))

        for x in range(width):
            tile = row[x]

            if tile == "LOCKED":
                continue

            usable_tiles += 1

            if tile is not None:
                occupied_tiles += 1

    # How efficiently are we using the land we already own?
    utilization = (
        occupied_tiles / max(1, usable_tiles)
    )

    days_left = max(
        0,
        SEASON_DAYS - day
    )

    # Land purchases are only worthwhile when:
    #   1. Existing land is mostly occupied.
    #   2. There is enough season remaining.
    #   3. We have enough cash after purchasing it.
    #
    # Thresholds are intentionally conservative because the objective
    # is final cash, not number of unlocked tiles.

    land_buy = False

    if unlocked_count == 1:
        # First expansion: cheapest and usually the best ROI.
        if (
            utilization >= 0.80
            and days_left >= 16
            and money >= 3000
            and len(hands) >= 1
        ):
            land_buy = True

    elif unlocked_count == 2:
        # Second expansion costs $2,000.
        if (
            utilization >= 0.85
            and days_left >= 18
            and money >= 5000
            and len(hands) >= 2
        ):
            land_buy = True

    elif unlocked_count == 3:
        # Third expansion costs $4,000.
        # Be extremely conservative with this one.
        if (
            utilization >= 0.90
            and days_left >= 20
            and money >= 8000
            and len(hands) >= 3
        ):
            land_buy = True

    if land_buy:
        add_order(["BUY_LAND"])
    # -----------------------------------------------------------------------
    # 4. Hire a hand.
    #
    # Hiring is cheap early in each day, but there is no reason to create
    # a huge workforce when the farm has little work to do.
    # -----------------------------------------------------------------------

    current_hands = len(hands)

    if (
        current_hands < MAX_HANDS
        and hour <= 2
        and days_left >= 5
    ):
        hires_today = _safe_int(
            farm.get("hires_today", 0)
        )

        hire_index = min(
            hires_today,
            len(FIB) - 1,
        )

        hire_cost = FIB[hire_index]

        # Require substantial cash relative to the tiny hiring cost so we
        # don't accidentally expand labor before buying seeds/land.
        if money >= 1500 and money >= hire_cost * 100:
            add_order(["HIRE"])

    # -----------------------------------------------------------------------
    # Final defensive cleanup.
    # -----------------------------------------------------------------------

    market_orders = market_orders[:max_market_orders]

    return {
        "farmer": farmer_action,
        "hands": hands_actions,
        "market": market_orders,
    }
