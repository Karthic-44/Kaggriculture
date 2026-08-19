"""
Local Kaggriculture runner.

Run:
    python play.py
    python play.py --steps 720 --seed 17
"""

import argparse
from kaggle_environments import make
from main import agent


def run(steps=720, seed=17):
    env = make(
        "kaggriculture",
        configuration={"episodeSteps": steps, "seed": seed},
        debug=True,
    )

    env.run([agent, "starter"])

    final = env.steps[-1]
    print("\n" + "=" * 60)
    print("KAGGRICULTURE LOCAL GAME")
    print("=" * 60)

    for i, state in enumerate(final):
        print(f"Player {i}: reward={state.reward}, status={state.status}")

    print("=" * 60)
    print("Game finished.")
    print("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--steps", type=int, default=720)
    parser.add_argument("--seed", type=int, default=17)
    args = parser.parse_args()
    run(args.steps, args.seed)
