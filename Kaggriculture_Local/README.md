# Kaggriculture Local Starter

This project gives you a clean local starting point for the Kaggriculture
simulation competition.

## What it contains

- `main.py` - your competition agent
- `play.py` - runs the agent locally against the built-in `starter` opponent
- `requirements.txt` - dependencies

## Setup

Use Python 3.12:

```powershell
py -3.12 -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

## Run a quick game

```powershell
python play.py --steps 100
```

## Run a full season

```powershell
python play.py --steps 720
```

## Competition submission

Kaggle expects `main.py` at the root and an `agent(obs, config=None)`
function.

```powershell
kaggle competitions submit kaggriculture -f main.py -m "starter agent"
```

This is a local development starter, not an official Kaggle client or
replacement for the competition website.
