from collections import defaultdict

# A deterministic, competition-safe heuristic agent for Kaggriculture.
# The official submission contract expects: agent(obs, config=None) -> action dict.

def agent(obs, config=None):
    day = obs.get("day", 0)
    hour = obs.get("hour", 0)
    private = obs.get("private", {})
    farms = obs.get("farms", [])
    me = obs.get("player", 0)

    seeds = private.get("seeds", {})
    shed = private.get("shed", {})
    market = obs.get("market", {})
    prices = market.get("prices", {})
    farm = farms[me] if me < len(farms) else {}

    # We keep the policy intentionally simple and robust:
    # 1) buy cheap crop seeds early
    # 2) plant empty cells
    # 3) water every day
    # 4) harvest mature crops
    # 5) sell harvested goods
    #
    # The policy is designed to be a clean starting point for experimentation.

    farmer = ["PASS"]
    market_orders = []

    # Early-game seed purchases. Wheat is cheap and forgiving.
    if hour == 0:
        if day < 3:
            market_orders += [["BUY_SEED", "WHEAT", 3]]
            market_orders += [["BUY_SEED", "CARROT", 2]]
        elif day < 10:
            market_orders += [["BUY_SEED", "WHEAT", 2]]

    # Inspect our farm. Kaggriculture farm tiles are represented as dictionaries.
    plants = []
    empty = []
    for tile in farm.get("tiles", []):
        if isinstance(tile, dict):
            if tile.get("type") in ("WHEAT", "CARROT", "TOMATO", "STRAWBERRY", "MELON"):
                plants.append(tile)
            elif not tile.get("type"):
                empty.append(tile)

    # If the observation format has positions, use them; otherwise the engine
    # still receives a valid PASS and market policy.
    if plants:
        # Prefer watering/harvesting. Exact movement planning is intentionally
        # delegated to later strategy versions.
        mature = any(
            p.get("ready_to_harvest") or p.get("harvestable") or
            p.get("yield_units", 0) > 0
            for p in plants
        )
        if mature:
            farmer = ["HARVEST"]
        else:
            farmer = ["WATER"]

    # Sell products currently visible in the player's inventory/shed.
    for item, qty in list(shed.items()):
        if item in {"WHEAT", "CARROT", "TOMATO", "STRAWBERRY", "MELON", "EGG", "MILK", "WOOL"}:
            if qty > 0:
                market_orders.append(["SELL", item, min(int(qty), 10)])

    return {"farmer": farmer, "market": market_orders[:10]}
