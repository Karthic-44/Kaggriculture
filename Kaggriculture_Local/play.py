"""
Local Kaggriculture runner.

Run:
    python play.py

This starts the real Kaggriculture environment locally, runs your agent
against the built-in starter opponent, and prints a compact result.

Use --steps 100 for a quick test or --steps 720 for a full season.
"""

import argparse
from kaggle_environments import make
from main import agent


def run(steps=200, seed=17):
    env = make(
        "kaggriculture",
        configuration={"episodeSteps": steps, "seed": seed},
        debug=True,
    )

    # "starter" is provided by the Kaggriculture environment.
    env.run([agent, "starter"])

    final = env.steps[-1]
    print("\n" + "=" * 60)
    print("KAGGRICULTURE LOCAL GAME")
    print("=" * 60)

    for i, state in enumerate(final):
        print(f"Player {i}: reward={state.reward}, status={state.status}")

    print("=" * 60)
    print("Game finished.")
    print("To run a full season: python play.py --steps 720")
    print("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--steps", type=int, default=200)
    parser.add_argument("--seed", type=int, default=17)
    args = parser.parse_args()
    run(args.steps, args.seed)
