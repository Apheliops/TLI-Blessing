# TLI Blessing Helper

A small Windows companion app for **Torchlight: Infinite** with a transparent in-game overlay.

- **DS Calculator** — reads the *Blessings from Gods* offers on your screen and tells you which ones are worth buying with Divinity Stones (DS).
- **Void Sea advisor** — on the *Void Sea Seal* and *Void Sea Terminal* screens it recommends the flame / Seal Whisper that gives the best chance of **red (Ultimate) Hero Memories**, and it learns from your own runs.

The app only **reads the game's log file and takes screenshots**. It does not read or change game memory, does not send any input to the game and does not automate anything. All your data stays on your PC.

---

## Requirements

- Windows 10 / 11
- [Python 3.9+](https://www.python.org/downloads/) — tick **"Add python.exe to PATH"** during setup
- Game language: **English** (the screen reader expects English text)
- Screen: 16:9, tested at **2560×1440** and **1920×1080** (other resolutions are scaled automatically; below 1080p reading gets less reliable)
- Fullscreen, borderless and **windowed** mode all work — the app finds the game window and reads only that

## Installation

1. Download the latest **`TLI-Blessing-vX.Y.zip`** from the [Releases](https://github.com/Apheliops/TLI-Blessing/releases/latest) page and extract it anywhere, e.g. `C:\TLI-Blessing`.
   When updating, extract the new version over the old folder — your data in `userdata` is kept.
2. Double-click **`Setup.bat`**. It installs the Python packages (`requirements.txt`) and Tesseract-OCR.
3. Turn the game log on (see below).
4. Double-click **`Start.bat`**.

### Turning on the game log

The app needs the game's `UE_game.log`.

1. Log in to a character.
2. **Settings → Other → Enable Log**.
3. Go back to the **character select screen and log in again** — the log only starts after this.

The app finds the log automatically. If it can't, click **📂 Select log…** and choose
`<game folder>\UE_game\TorchLight\Saved\Logs\UE_game.log`. The window explains what to do when the log is missing or not being written.

---

## Usage

Closing the main window (X) does not quit the app; it goes to the system tray. **Start with Windows** starts it hidden in the tray. Use **Quit app** to close it.

### The overlay

The small transparent panel is visible **only while the game is in the foreground**.

| Element | Meaning |
|---|---|
| **Blessings / Void Sea** tabs | switch between the two helpers |
| **Read** | read the screen again manually |
| **LOG ●** | green = the log is being read, red = log problem |
| **Picture ●** | green = screen reading works, red = the last read failed or the reader is not installed |
| ⚙ / – / ✕ | open the main window / collapse / quit |

Drag the title bar to move it, drag the ◢ corner to resize it.

### DS Calculator

1. Open the **Exchange** once and search **Divinity Stone**, then search the items you see in Blessings (Netherrealm Resonance, Transcendence Wedges, Energy Core, Ultimate Ember, …). Prices are learned from these searches through the log — search again from time to time to keep them fresh.
2. Open or refresh **Blessings from Gods**. The screen is read automatically and the overlay shows **only the profitable offers**, most profitable first.

How the verdict is calculated:

```
net value of 1 DS = DS market price × (1 − tax)
offer value       = quantity × item market price × (1 − tax)   (FE offers: quantity)
max DS            = offer value / net value of 1 DS
```

Market price = average of the 3 cheapest Exchange listings. An offer is **BUY** when its DS price is below *max DS* and the profit is at least your *Min profit %*.

**Items sold for DS** lists every item seen in Blessings, from the least to the most valuable. Click a row to switch it between **recommend** and **hidden**. Hidden items are still read but never recommended. By default **Flame Sand** and **Energy Core** are hidden.

---

## Void Sea advisor — how it works

### When it runs

The app watches the log. When the **Void Sea Seal** (`VoidBeacon`) or the **Void Sea Terminal** (`VoidPort`) page opens, it reads the screen once and switches the overlay to the Void Sea tab. If a read fails, press **Read**.

Keep the **Seal Whispers Owned** panel on the right side of the screen **open**: the advice is always based on the whispers you already own. If the panel can't be read, an orange warning tells you so.

### What it recommends

- **Void Sea Seal (flames):** compares the *Armament* (top-left) and *Wonder* (top-right) flames. The flame colour is its quality — blue < purple < yellow. A yellow flame guarantees a *Rare or higher* whisper, purple *Magic or higher*, blue is assumed to be *Normal or higher*. When both flames give almost the same gain, the higher-quality flame is recommended.
- **Void Sea Terminal (3 cards):** recommends the card that raises your expected number of red Hero Memories the most.
- A pink **Why:** line explains the decision (e.g. *"Rarity raises the red rate 5.0% → 9.4%; the Armament card only +14%"*).

### The model

```
memories = base × (1 + base quantity) × (1 + treasure quantity) × (1 + appear chance) × (1 + memory quantity)
           + flat extra + lucky extra
red      = (base memories × red rate × (1 + Rarity)^k  +  extra memories × red rate) × (1 + replication)
```

Bonuses of the same type add up, different types multiply. Rarity only affects the base drops, not the flat / lucky extra memories (as described in the community *Blacksail Guide* on r/TorchlightInfinite).

### Learning from your runs

Every time you enter the Void Sea map the app records your 6 whispers and **automatically counts the Hero Memories you pick up in the Void Sea map**. Red (Ultimate) quality is not written in the game log, so **press +1 on the overlay for every red memory** you get in that run (−1 undoes a wrong click).

After **5 recorded runs** the advice is tuned to your own data:

- A run is a **success** if you got a red memory, or if you picked up more memories than your **success threshold** — the median memory count of your runs without a red. The threshold moves as your data grows.
- The app learns how strong each whisper type really is from both the memory counts and the success / failure labels, and shows a **Target combination** (plus 2 alternatives) — the 6 whispers that currently look best.
- With little data it stays close to the model above; with more runs it trusts your data more.

### ⚠ Your data must be consistent

The app can only count what **you pick up**, and only in the **Void Sea map**. The game log does not show items that dropped but were not picked up, nor memories hidden by your loot filter. So the learning is only as good as your habits:

- **Use a loot filter that shows Hero Memories** (at least the qualities you care about) and **keep the same filter**. If you change it, old and new runs are no longer comparable.
- **Pick up memories the same way every run** (e.g. every memory the filter shows).
- **Keep the app running for the whole run.** If it is restarted in the middle of a run, that run is left out.
- **Press +1 for every red memory**, every run.
- Memories picked up outside the Void Sea map are not counted.

This repository does **not** include a loot filter or any item-pickup script — use your own filter in game.

---

## Your data

Everything personal is stored in the `userdata` folder next to the app (it is excluded from Git):

| File | Content |
|---|---|
| `tli_blessing_data.json` | settings and learned prices |
| `voidsea_kayit.json` / `.csv` | Void Sea run log (whispers, memories, reds) — keep it, the learning depends on it |
| `voidsea_kayit.yedek.json` | automatic backup of the run log |
| `voidsea_model.json` | what the app learned from your runs, with history |
| `rakamlar.json`, `ornekler/` | digit samples learned from your purchases (better price reading) |
| `son_tarama*.png`, `hata_log.txt` | last failed screen read and error log, for troubleshooting |

When moving the app to another folder, copy `userdata` too.

## Troubleshooting

- **LOG is red** — see *Turning on the game log*.
- **Picture is red** — run `Setup.bat` again (Tesseract-OCR or a Python package is missing).
- **Game not found** — the app reads the game window directly. Only if it can't find it does it read a whole monitor: then set **Screen #** in the main window to the monitor the game is on.
- **Cards not read** — press **Read** once the cards have finished animating.

---

## License

Copyright © 2026 Apheliops

This program is free software: you can redistribute it and/or modify it under the terms of the **GNU General Public License v3.0** — see [LICENSE](LICENSE).
Third-party data and notices: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

Torchlight: Infinite is a trademark of XD Inc. This is a fan-made tool, not affiliated with or endorsed by XD Inc.
