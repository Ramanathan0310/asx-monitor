name: Wally - 52-Week Low Screen

on:
  schedule:
    # Tue + Fri at 9:00 AEST (23:00 UTC Mon/Thu)
    - cron: "0 23 * * 1"   # Tuesday
    - cron: "0 23 * * 4"   # Friday
  workflow_dispatch:
    inputs:
      threshold:
        description: "% distance from 52-week low to flag (default: 5)"
        required: false
        default: "5"

permissions:
  contents: write

concurrency:
  group: wally-screen
  cancel-in-progress: false

jobs:
  screen:
    runs-on: ubuntu-latest
    timeout-minutes: 20

    steps:
      - uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip

      - name: Install dependencies
        run: |
          pip install --upgrade pip
          pip install yfinance matplotlib

      - name: Run Wally
        env:
          SMTP_HOST:     ${{ secrets.SMTP_HOST }}
          SMTP_PORT:     ${{ secrets.SMTP_PORT }}
          SMTP_USER:     ${{ secrets.SMTP_USER }}
          SMTP_PASSWORD: ${{ secrets.SMTP_PASSWORD }}
          EMAIL_FROM:    ${{ secrets.EMAIL_FROM }}
          EMAIL_TO:      ${{ secrets.EMAIL_TO }}
        run: |
          THRESHOLD="${{ inputs.threshold || '5' }}"
          python wally.py $THRESHOLD

      - name: Commit reports
        run: |
          git config user.name "wally-bot"
          git config user.email "wally-bot@users.noreply.github.com"
          git add reports/wally_*.json reports/wally_*.html watchlist.json || true
          if git diff --cached --quiet; then
            echo "No changes to commit."
          else
            git commit -m "Wally screen $(date -u +'%Y-%m-%d') [skip ci]"
            git push
          fi
