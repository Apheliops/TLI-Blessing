# TLI Blessing Helper

> [!CAUTION]
> ## ⚠ UPDATING? NEVER DELETE THE `userdata` FOLDER
>
> The **`userdata`** folder (next to `Start.bat`) holds **ALL your data**:
> - **`voidsea_runs.json`** and **`voidsea_runs.backup.json`** — your Void Sea run log (the learning depends on it and it **cannot be recovered**)
> - `tli_blessing_data.json` — your settings and learned prices
>
> Release zips **never contain** `userdata`, so an update never overwrites it — but you lose it if you delete it.
>
> **How to update (Windows "Extract All"):**
> 1. **Close the app** (✕ on the overlay, *Quit app*, or right-click the tray icon → *Quit*).
> 2. Download the new `TLI-Blessing-vX.Y.zip` from Releases.
> 3. Right-click the zip → **Extract All…**
> 4. **Change the destination** to the folder that *contains* your app folder — e.g. if the app is in `C:\TLI-Blessing`, extract to `C:\`. (Windows suggests a new folder in *Downloads* — **don't accept that**.)
> 5. When Windows asks *"Replace or Skip Files"*, choose **Replace the files in the destination**. This only replaces program files — `userdata` is not in the zip, so it is never touched. (*Skip* keeps the old version; *Let me decide* can mix versions.)
> 6. Start the app with **`Start.bat`** — the version at the bottom of the overlay should be the new one. Run `Setup.bat` again only if the release notes say so.
>
> - ❌ **DON'T delete the old folder** before updating — `userdata` is deleted with it.
> - ❌ **DON'T extract into a different folder** — the app starts empty there. Your data is still in the old folder: copy `userdata` over.
> - ❌ **DON'T extract inside the old folder** — you get `TLI-Blessing\TLI-Blessing` and the app starts empty.
> - 💾 **Back up `userdata` from time to time** — just copy the folder somewhere safe.

A small Windows companion app for **Torchlight: Infinite** with a transparent in-game overlay.

- **DS Calculator** — reads the *Blessings from Gods* offers on your screen and tells you which ones are worth buying with Divinity Stones (DS).
- **Void Sea advisor** — on the *Void Sea Seal* and *Void Sea Terminal* screens it recommends the flame / Seal Whisper that gives the best chance of **red (Ultimate) Hero Memories**, and it learns from your own runs.

The app only **reads the game's log file and takes screenshots**. It does not read or change game memory, does not send any input to the game and does not automate anything. All your data stays on your PC.

---

## Requirements

- Windows 10 / 11
- [Python 3.9+](https://www.python.org/downloads/) — tick **"Add python.exe to PATH"** during setup
- Game language: **English** or **Simplified Chinese** (简体中文). Chinese needs the Windows Chinese (Simplified) OCR language pack, which Chinese Windows has by default. On other Windows, install it from an administrator PowerShell with `Add-WindowsCapability -Online -Name "Language.OCR~~~zh-CN~0.0.1.0"`. The app detects the game language by itself. If you play in another language, opening a [GitHub issue](https://github.com/Apheliops/TLI-Blessing/issues) with screenshots of the Blessings and Void Sea Seal / Terminal screens helps add support.
- Screen: tested at **2560×1440**, **1920×1080** and **2560×1080 (21:9 ultrawide)**. Other resolutions and in-game UI scales are detected automatically; below 1080p reading gets less reliable.
- Fullscreen, borderless and **windowed** mode all work — the app finds the game window and reads only that

## Installation

1. Download the latest **`TLI-Blessing-vX.Y.zip`** from the [Releases](https://github.com/Apheliops/TLI-Blessing/releases/latest) page and extract it anywhere, e.g. `C:\TLI-Blessing`.
   **Updating?** Read the warning at the top first — extract over the old folder and never delete `userdata`.
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

**Language** (bottom of the main window): **English** or **简体中文**. It changes the text of the main window and the overlay right away, no restart needed. It is only the interface language: reading the game screen follows the game's own language automatically, and the run log / error reports stay in English.

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

- **Void Sea Seal (flames):** compares the *Armament* and *Wonder* flames (top-left / top-right — their corners can swap between sessions; the app tells them apart from the selected flame's text and the colour in the centre circle). The flame colour is its quality — blue < purple < yellow. A yellow flame guarantees a *Rare or higher* whisper, purple *Magic or higher*; a blue flame only guarantees the whisper type, its quality is random. When both flames give almost the same gain, the higher-quality flame is recommended.
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

### What to expect

- **It does not guarantee red memories**, from the start or later on. The goal is that, once it has collected enough data, it increases the odds of getting more Hero Memories, and rarer ones.
- **All of its data is collected locally, only from you.** Nothing is shared with or taken from other players — so how well it learns depends entirely on your own data (see below).

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

Everything personal is stored in the `userdata` folder next to the app — it stays on your PC and is never uploaded anywhere:

| File | Content |
|---|---|
| `tli_blessing_data.json` | settings and learned prices |
| `voidsea_runs.json` / `.csv` | Void Sea run log (whispers, memories, reds) — keep it, the learning depends on it |
| `voidsea_runs.backup.json` | automatic backup of the run log |
| `voidsea_model.json` | what the app learned from your runs, with history |
| `learned_digits.json`, `price_samples/` | digit samples learned from your purchases (better price reading) |
| `error_report.md`, `error_screens/` | every reading problem and program error, with a screenshot — attach these to a bug report |
| `last_scan*.png`, `error_log.txt` | last failed screen read and raw error log |
| `icon.png` | app icon, downloaded once from tlidb |

When moving the app to another folder, copy `userdata` too.

**Update check:** at startup (and every 6 hours) the app asks GitHub for the latest release number of this repository. Nothing about you or your data is sent. If a newer version exists, a "new version available" bar appears in the main window and a one-line notice on the overlay. **Install update** downloads the release zip from this repository's GitHub Releases, replaces the program files and restarts the app; the `userdata` folder is never touched and the replaced files are kept in `backup\update-from-v<old version>`. **Release page** opens the download page instead, if you prefer to update by hand. Turn the check off with **Check for updates** at the bottom of the main window.

## Troubleshooting

- **LOG is red** — see *Turning on the game log*.
- **Picture is red** — run `Setup.bat` again (Tesseract-OCR or a Python package is missing).
- **Game not found** — the app reads the game window directly. Only if it can't find it does it read a whole monitor: then set **Screen #** in the main window to the monitor the game is on.
- **Cards not read** — press **Read** once the cards have finished animating. If it keeps happening, open a [bug report](https://github.com/Apheliops/TLI-Blessing/issues/new/choose) and attach `userdata\error_report.md` with the matching screenshot from `userdata\error_screens`.

---

## Contributors

Thanks to everyone who helped make this tool better:

- **LordZ3us97** — tested it on a 2560×1080 ultrawide screen and sent the screenshots that made 21:9 support possible ([#1](https://github.com/Apheliops/TLI-Blessing/issues/1)).
- **Express_Cherry_39** — sent screenshots of the Chinese client (Void Sea Seal / Terminal and Blessings from Gods) that made Simplified Chinese support possible.

Found a bug or play in another language or resolution? [Open an issue](https://github.com/Apheliops/TLI-Blessing/issues/new/choose) — every report helps.

---

## License

Copyright © 2026 Apheliops

This program is free software: you can redistribute it and/or modify it under the terms of the **GNU General Public License v3.0** — see [LICENSE](LICENSE).
Third-party data and notices: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

Torchlight: Infinite is a trademark of XD Inc. This is a fan-made tool, not affiliated with or endorsed by XD Inc.
