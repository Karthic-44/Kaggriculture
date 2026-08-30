"""
Kaggriculture agent.

Restored proven crop strategy + controlled one-cow experiment.
"""

# ---------------------------------------------------------------------------
# GAME DATA
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

FIB = [1, 1, 2, 3, 5, 8, 13, 21, 34, 55]
MAX_HANDS = 5

# ---------------------------------------------------------------------------
# CONTROLLED COW EXPERIMENT
# ---------------------------------------------------------------------------

COW_COST = 400
COW_START_DAY = 8
COW_MAX = 1
COW_MIN_CASH = 2500
WHEAT_FEED_BUFFER = 3


# ---------------------------------------------------------------------------
# UTILITIES
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


def _shed_targets(board_size):
    h = board_size // 2

    return [
        (h - 1, h - 1),
        (h, h - 1),
        (h - 1, h),
        (h, h),
    ]


def _is_locked(tile):
    return tile == "LOCKED"


def _is_plant(tile):
    return (
        isinstance(tile, dict)
        and tile.get("kind") == "PLANT"
    )


def _is_weed(tile):
    return (
        isinstance(tile, dict)
        and tile.get("kind") == "WEED"
    )


def _is_animal_tile(tile):
    return (
        isinstance(tile, dict)
        and tile.get("kind") in ("COOP", "PASTURE")
    )


def _is_shed_adjacent(pos, board_size):
    x, y = pos
    h = board_size // 2

    return (
        x in (h - 1, h)
        and y in (h - 1, h)
    )


def _safe_int(value, default=0):
    try:
        return int(value)
    except Exception:
        return default


# ---------------------------------------------------------------------------
# ORIGINAL PROVEN CROP SCORING
# ---------------------------------------------------------------------------

def _crop_score(crop, price):
    c = CROPS[crop]

    if price <= 0:
        return -999999

    profit = (
        c["max_yield"] * price
        - c["seed"]
    )

    if profit <= 0:
        return -999999

    if crop == "WHEAT":
        return profit / 4

    if crop == "CARROT":
        return profit / 3

    if crop == "TOMATO":
        return profit / 8

    if crop == "STRAWBERRY":
        return profit / 10

    if crop == "MELON":
        return profit / 12

    return profit


def _best_crop(prices, money):
    best = None
    best_score = float("-inf")

    fallback = {
        "WHEAT": 25,
        "CARROT": 35,
        "TOMATO": 60,
        "STRAWBERRY": 120,
        "MELON": 250,
    }

    for crop, data in CROPS.items():

        if money < data["seed"]:
            continue

        price = prices.get(crop, 0)

        if price <= 0:
            price = fallback[crop]

        score = _crop_score(
            crop,
            price,
        )

        if score > best_score:
            best_score = score
            best = crop

    return best


# ---------------------------------------------------------------------------
# MAIN AGENT
# ---------------------------------------------------------------------------

def agent(obs, config=None):

    # -----------------------------------------------------------------------
    # BASIC OBSERVATION
    # -----------------------------------------------------------------------

    farms = obs.get("farms", [])
    player = _safe_int(obs.get("player", 0))
    day = _safe_int(obs.get("day", 0))
    hour = _safe_int(obs.get("hour", 0))

    if (
        not farms
        or player < 0
        or player >= len(farms)
    ):
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
        _cfg(
            config,
            "maxMarketOrdersPerTurn",
            10,
        ),
        10,
    )

    shed_capacity = _safe_int(
        _cfg(
            config,
            "shedCapacity",
            SHED_CAPACITY,
        ),
        SHED_CAPACITY,
    )

    tiles = farm.get("tiles", [])
    money = float(farm.get("money", 0))

    private = obs.get("private", {}) or {}

    shed = dict(
        private.get("shed", {}) or {}
    )

    seeds = dict(
        private.get("seeds", {}) or {}
    )

    inventories = list(
        private.get("inventories", []) or []
    )

    market = obs.get("market", {}) or {}

    prices = dict(
        market.get("prices", {}) or {}
    )

    farmer_pos = tuple(
        farm.get("farmer", [0, 0])
    )

    hands = [
        tuple(h)
        for h in (
            farm.get("hands", []) or []
        )
    ]

    units = [farmer_pos] + hands
    n_units = len(units)

    height = min(
        board_size,
        len(tiles)
    )

    # -----------------------------------------------------------------------
    # INVENTORY
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
            ):
                total += max(
                    0,
                    _safe_int(qty)
                )

        return total

    unit_inventory = [
        inventory_total(i)
        for i in range(n_units)
    ]

    # -----------------------------------------------------------------------
    # JOB LISTS
    # -----------------------------------------------------------------------

    harvest_jobs = []
    water_jobs = []
    plant_jobs = []
    weed_jobs = []

    animal_feed_jobs = []
    animal_care_jobs = []
    animal_harvest_jobs = []

    empty_pastures = []

    cow_count = 0

    # -----------------------------------------------------------------------
    # SCAN BOARD
    # -----------------------------------------------------------------------

    for y in range(height):

        row = tiles[y]

        width = min(
            board_size,
            len(row)
        )

        for x in range(width):

            tile = row[x]

            if _is_locked(tile):
                continue

            pos = (x, y)

            # ---------------------------------------------------------------
            # CROPS
            # ---------------------------------------------------------------

            if _is_plant(tile):

                crop = tile.get("crop")

                yield_units = _safe_int(
                    tile.get("yield_units", 0)
                )

                planted_day = _safe_int(
                    tile.get(
                        "planted_day",
                        day,
                    )
                )

                age = max(
                    0,
                    day - planted_day
                )

                if (
                    yield_units > 0
                    and crop in CROPS
                ):

                    crop_data = CROPS[crop]

                    # Do not harvest merely because the crop has produced
                    # its first yield.  Wait for the maximum useful yield when
                    # possible, while still harvesting close to expiry.
                    max_yield = crop_data["max_yield"]
                    near_expiry = (
                        age >= crop_data["max_yield_day"] + 1
                    )

                    if (
                        yield_units >= max_yield
                        or near_expiry
                    ):
                        harvest_jobs.append(
                            (
                                pos,
                                yield_units,
                                crop,
                            )
                        )

                if not tile.get(
                    "watered_today",
                    False,
                ):
                    water_jobs.append(
                        (
                            pos,
                            age,
                            crop,
                        )
                    )

            # ---------------------------------------------------------------
            # ANIMALS
            # ---------------------------------------------------------------

            elif _is_animal_tile(tile):

                animal = tile.get("animal")

                if tile.get("kind") == "PASTURE":

                    if animal is None:
                        empty_pastures.append(pos)

                    elif animal == "COW":

                        cow_count += 1

                        if not tile.get(
                            "fed_today",
                            False,
                        ):
                            animal_feed_jobs.append(
                                pos
                            )

                        if not tile.get(
                            "cared_today",
                            False,
                        ):
                            animal_care_jobs.append(
                                pos
                            )

                        if (
                            _safe_int(
                                tile.get(
                                    "yield_units",
                                    0,
                                )
                            ) > 0
                        ):
                            animal_harvest_jobs.append(
                                pos
                            )

            # ---------------------------------------------------------------
            # WEEDS
            # ---------------------------------------------------------------

            elif _is_weed(tile):
                weed_jobs.append(pos)

    # -----------------------------------------------------------------------
    # FREE LAND
    # -----------------------------------------------------------------------

    free_tiles = []

    for y in range(height):

        row = tiles[y]

        width = min(
            board_size,
            len(row)
        )

        for x in range(width):

            if row[x] is None:
                free_tiles.append(
                    (x, y)
                )

    # -----------------------------------------------------------------------
    # ORIGINAL PLANTING RATE
    # -----------------------------------------------------------------------

    max_new_plants = max(
        2,
        n_units * 5
    )

    cash_reserve = max(
        250.0,
        money * 0.25
    )

    best_crop = _best_crop(
        prices,
        max(
            0.0,
            money - cash_reserve,
        ),
    )

    if best_crop is not None:

        available_seed_count = _safe_int(
            seeds.get(best_crop, 0)
        )

        number_to_plant = min(
            len(free_tiles),
            available_seed_count,
            max_new_plants,
        )

        for pos in free_tiles[
            :number_to_plant
        ]:
            plant_jobs.append(
                (
                    pos,
                    best_crop,
                )
            )

    # -----------------------------------------------------------------------
    # UNIT ASSIGNMENT
    # -----------------------------------------------------------------------

    actions = [None] * n_units

    remaining_units = list(
        range(n_units)
    )

    def assign_job(
        job_pos,
        action,
    ):

        if not remaining_units:
            return False

        unit = min(
            remaining_units,
            key=lambda i:
                _distance(
                    units[i],
                    job_pos,
                ),
        )

        remaining_units.remove(unit)

        if units[unit] == job_pos:

            actions[unit] = action

        else:

            actions[unit] = [
                _step_toward(
                    units[unit],
                    job_pos,
                )
            ]

        return True

    # -----------------------------------------------------------------------
    # COW CARRYING CHECK
    # -----------------------------------------------------------------------

    def carried_cow(i):

        if i >= len(inventories):
            return False

        inv = inventories[i] or {}

        return (
            _safe_int(
                inv.get("COW", 0)
            ) > 0
        )

    # -----------------------------------------------------------------------
    # 0. COW DELIVERY
    # -----------------------------------------------------------------------

    for i in list(
        remaining_units
    ):

        if not carried_cow(i):
            continue

        if empty_pastures:

            pos = units[i]

            target = min(
                empty_pastures,
                key=lambda p:
                    _distance(
                        pos,
                        p,
                    ),
            )

            if pos == target:

                actions[i] = [
                    "PLACE",
                    "COW",
                ]

                empty_pastures.remove(
                    target
                )

            else:

                actions[i] = [
                    _step_toward(
                        pos,
                        target,
                    )
                ]

        else:

            pos = units[i]

            if _is_shed_adjacent(
                pos,
                board_size,
            ):

                actions[i] = [
                    "DROP"
                ]

            else:

                target = min(
                    _shed_targets(
                        board_size
                    ),
                    key=lambda p:
                        _distance(
                            pos,
                            p,
                        ),
                )

                actions[i] = [
                    _step_toward(
                        pos,
                        target,
                    )
                ]

        remaining_units.remove(i)

    # -----------------------------------------------------------------------
    # 1. RETURN PRODUCE TO SHED
    # -----------------------------------------------------------------------

    logistics_units = [
        i
        for i in range(n_units)
        if unit_inventory[i] > 0
    ]

    for i in sorted(
        logistics_units,
        key=lambda u:
            unit_inventory[u],
        reverse=True,
    ):

        if i not in remaining_units:
            continue

        pos = units[i]

        if _is_shed_adjacent(
            pos,
            board_size,
        ):

            actions[i] = [
                "DROP"
            ]

            remaining_units.remove(i)

        else:

            target = min(
                _shed_targets(
                    board_size
                ),
                key=lambda p:
                    _distance(
                        pos,
                        p,
                    ),
            )

            actions[i] = [
                _step_toward(
                    pos,
                    target,
                )
            ]

            remaining_units.remove(i)

    # -----------------------------------------------------------------------
    # 2. HARVEST
    # -----------------------------------------------------------------------

    harvest_jobs.sort(
        key=lambda j: (
            -j[1],
            j[0][1],
            j[0][0],
        )
    )

    for (
        pos,
        yield_units,
        crop,
    ) in harvest_jobs:

        if not remaining_units:
            break

        assign_job(
            pos,
            ["HARVEST"],
        )

    # -----------------------------------------------------------------------
    # 3. WATER
    # -----------------------------------------------------------------------

    water_jobs.sort(
        key=lambda j: (
            -j[1],
            j[0][1],
            j[0][0],
        )
    )

    for (
        pos,
        age,
        crop,
    ) in water_jobs:

        if not remaining_units:
            break

        assign_job(
            pos,
            ["WATER"],
        )

    # -----------------------------------------------------------------------
    # 3A. COW HARVEST
    # -----------------------------------------------------------------------

    for pos in animal_harvest_jobs:

        if not remaining_units:
            break

        assign_job(
            pos,
            ["HARVEST"],
        )

    # -----------------------------------------------------------------------
    # 3B. FEED
    # -----------------------------------------------------------------------

    for pos in animal_feed_jobs:

        if not remaining_units:
            break

        assign_job(
            pos,
            ["FEED"],
        )

    # -----------------------------------------------------------------------
    # 3C. CARE
    # -----------------------------------------------------------------------

    for pos in animal_care_jobs:

        if not remaining_units:
            break

        assign_job(
            pos,
            ["CARE"],
        )

    # -----------------------------------------------------------------------
    # 4. PLANT
    # -----------------------------------------------------------------------

    for pos, crop in plant_jobs:

        if not remaining_units:
            break

        assign_job(
            pos,
            [
                "PLANT",
                crop,
            ],
        )

    # -----------------------------------------------------------------------
    # 4B. BUILD PASTURE
    # -----------------------------------------------------------------------

    if (
        COW_START_DAY <= day
        < SEASON_DAYS - 8
        and cow_count < COW_MAX
        and money >= COW_MIN_CASH
        and not empty_pastures
        and free_tiles
        and remaining_units
    ):

        assign_job(
            free_tiles[0],
            ["BUILD_PASTURE"],
        )

    # -----------------------------------------------------------------------
    # 5. WEEDS
    # -----------------------------------------------------------------------

    for pos in weed_jobs:

        if not remaining_units:
            break

        assign_job(
            pos,
            ["DIG"],
        )

    # -----------------------------------------------------------------------
    # 6. IDLE WORKERS
    # -----------------------------------------------------------------------

    for unit in remaining_units:

        pos = units[unit]

        target = min(
            _shed_targets(
                board_size
            ),
            key=lambda p:
                _distance(
                    pos,
                    p,
                ),
        )

        if _is_shed_adjacent(
            pos,
            board_size,
        ):

            actions[unit] = [
                "PASS"
            ]

        else:

            actions[unit] = [
                _step_toward(
                    pos,
                    target,
                )
            ]

    # -----------------------------------------------------------------------
    # FALLBACK
    # -----------------------------------------------------------------------

    for i in range(n_units):

        if actions[i] is None:
            actions[i] = [
                "PASS"
            ]

    farmer_action = actions[0]
    hands_actions = actions[1:]

    # -----------------------------------------------------------------------
    # MARKET
    # -----------------------------------------------------------------------

    market_orders = []

    def add_order(order):

        if (
            len(market_orders)
            < max_market_orders
        ):

            market_orders.append(order)
            return True

        return False

    # -----------------------------------------------------------------------
    # 1. SELL SHED INVENTORY
    # -----------------------------------------------------------------------

    sell_items = [
        item
        for item, qty in shed.items()
        if (
            _safe_int(qty) > 0
            and item != "FERTILIZER"
        )
    ]

    sell_items.sort(
        key=lambda item:
            prices.get(item, 0),
        reverse=True,
    )

    shed_total = sum(
        max(
            0,
            _safe_int(v)
        )
        for v in shed.values()
    )

    for item in sell_items:

        if (
            len(market_orders)
            >= max_market_orders
        ):
            break

        qty = max(
            0,
            _safe_int(
                shed.get(item, 0)
            ),
        )

        if qty <= 0:
            continue

        price = prices.get(
            item,
            0,
        )

        if (
            shed_total
            >= shed_capacity * 0.75
        ):

            amount = qty

        else:

            amount = min(
                qty,
                20,
            )

        if price > 0:

            add_order(
                [
                    "SELL",
                    item,
                    int(amount),
                ]
            )

    # -----------------------------------------------------------------------
    # 2. ORIGINAL SEED BUYING
    # -----------------------------------------------------------------------

    if hour in (
        0,
        1,
        2,
    ):

        cash_for_seeds = max(
            0.0,
            money - 500.0,
        )

        crop = _best_crop(
            prices,
            cash_for_seeds,
        )

        if crop is not None:

            have = _safe_int(
                seeds.get(crop, 0)
            )

            target = max(
                4,
                n_units * 5,
            )

            if have < target:

                seed_price = CROPS[
                    crop
                ]["seed"]

                if seed_price > 0:

                    affordable = int(
                        cash_for_seeds
                        // seed_price
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
                                int(
                                    buy_amount
                                ),
                            ]
                        )

    # -----------------------------------------------------------------------
    # 2B. CONTROLLED COW INVESTMENT
    # -----------------------------------------------------------------------

    unplaced_cows = _safe_int(
        shed.get("COW", 0)
    )

    for inv in inventories:

        unplaced_cows += _safe_int(
            (inv or {}).get(
                "COW",
                0,
            )
        )

    if (
        COW_START_DAY <= day
        < SEASON_DAYS - 8
        and cow_count + unplaced_cows
        < COW_MAX
        and money >= COW_MIN_CASH
    ):

        if empty_pastures:

            if (
                len(market_orders)
                < max_market_orders
            ):

                add_order(
                    [
                        "BUY_ANIMAL",
                        "COW",
                        1,
                    ]
                )

    # -----------------------------------------------------------------------
    # WHEAT BUFFER FOR COW
    # -----------------------------------------------------------------------

    if (
        cow_count > 0
        and hour in (
            0,
            1,
            2,
        )
        and _safe_int(
            shed.get(
                "WHEAT",
                0,
            )
        ) < WHEAT_FEED_BUFFER
        and money >= 3000
        and len(market_orders)
        < max_market_orders
    ):

        wheat_price = prices.get(
            "WHEAT",
            0,
        )

        if wheat_price > 0:

            buy_wheat = min(
                WHEAT_FEED_BUFFER
                - _safe_int(
                    shed.get(
                        "WHEAT",
                        0,
                    )
                ),
                3,
                int(
                    max(
                        0.0,
                        money - 1000.0,
                    )
                    // wheat_price
                ),
            )

            if buy_wheat > 0:

                add_order(
                    [
                        "BUY_PRODUCT",
                        "WHEAT",
                        int(
                            buy_wheat
                        ),
                    ]
                )

    # -----------------------------------------------------------------------
    # 3. SMART LAND EXPANSION
    # -----------------------------------------------------------------------

    unlocked = list(
        farm.get(
            "unlocked_quadrants",
            [],
        ) or []
    )

    unlocked_count = len(
        unlocked
    )

    usable_tiles = 0
    occupied_tiles = 0

    for y in range(height):

        row = tiles[y]

        width = min(
            board_size,
            len(row)
        )

        for x in range(width):

            tile = row[x]

            if tile == "LOCKED":
                continue

            usable_tiles += 1

            if tile is not None:
                occupied_tiles += 1

    utilization = (
        occupied_tiles
        / max(
            1,
            usable_tiles,
        )
    )

    days_left = max(
        0,
        SEASON_DAYS - day,
    )

    land_buy = False

    if unlocked_count == 1:

        if (
            utilization >= 0.80
            and days_left >= 16
            and money >= 3000
            and len(hands) >= 1
        ):
            land_buy = True

    elif unlocked_count == 2:

        if (
            utilization >= 0.85
            and days_left >= 18
            and money >= 5000
            and len(hands) >= 2
        ):
            land_buy = True

    elif unlocked_count == 3:

        if (
            utilization >= 0.90
            and days_left >= 20
            and money >= 8000
            and len(hands) >= 3
        ):
            land_buy = True

    if land_buy:
        add_order(
            ["BUY_LAND"]
        )

    # -----------------------------------------------------------------------
    # 4. HIRE
    # -----------------------------------------------------------------------

    current_hands = len(hands)

    if (
        current_hands < MAX_HANDS
        and hour <= 2
        and days_left >= 5
    ):

        hires_today = _safe_int(
            farm.get(
                "hires_today",
                0,
            )
        )

        hire_index = min(
            hires_today,
            len(FIB) - 1,
        )

        hire_cost = FIB[
            hire_index
        ]

        if (
            money >= 1500
            and money >= hire_cost * 100
        ):

            add_order(
                ["HIRE"]
            )

    # -----------------------------------------------------------------------
    # FINAL
    # -----------------------------------------------------------------------

    market_orders = market_orders[
        :max_market_orders
    ]

    return {
        "farmer": farmer_action,
        "hands": hands_actions,
        "market": market_orders,
    }