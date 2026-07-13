# macOS window grid (optional)

Tiles the fleet's bot Terminal windows into a 3x2 grid. Purely cosmetic - it only
moves and resizes windows, and never touches the bot processes.

This extra is optional and macOS-only (it drives Terminal.app through
AppleScript). It does nothing on Linux or Windows.

## Install

Copy `grid.sh` to `$FLEET_HOME/bin/fleet-grid` and make it executable:

```
cp extras/macos-window-grid/grid.sh "$FLEET_HOME/bin/fleet-grid"
chmod +x "$FLEET_HOME/bin/fleet-grid"
```

Then turn the grid on in `fleet.json`:

```json
"options": { "grid": true }
```

With `grid` enabled and `fleet-grid` installed, `fleet-launch` asks the grid to
retile after a bot window comes up.

## Use

```
fleet-grid laptop     # the locked MacBook 16" grid
fleet-grid monitor    # the same grid scaled to fill the active external display
```

## Cell assignment

Cells follow bot order in `fleet.json`:

```
bot 1 (TL)   bot 2 (TM)   bot 3 (TR)
bot 4 (BL)   bot 5 (BM)   bot 6 (BR)
```

Top row left to right, then bottom row. A fleet with fewer than six bots fills as
many cells as it has; bots beyond the first six are left where they are.

## First run

macOS prompts once to let your terminal control Terminal.app (System Settings ->
Privacy & Security -> Automation). Approve it, then run `fleet-grid` again.
