# Telegram Video Duplicate Remover

A Windows/Mac/Linux tool that finds and removes **duplicate video files** across
your Telegram groups.

It works in two phases:

1. **Index your stock** – scans every group, records all existing videos, and
   flags duplicates already sitting in your stock.
2. **Auto-check new videos** – watches your groups and, whenever a new video is
   posted, checks it against the index. If it's a duplicate it asks you to
   delete it; if it's unique it keeps it and remembers it.

Nothing is ever deleted without a **`y/n` confirmation**, and the **oldest**
copy of each video is always the one kept.

There are three ways to use it:

* **Ready-made Windows .exe (easiest – no install)** – just download and
  double-click:
  **[Download TelegramDuplicateRemover.exe](https://github.com/anirudhatalmale6-alt/tg-video-dedup/releases/download/latest/TelegramDuplicateRemover.exe)**
* **Desktop app from source** – a single window where you load your groups,
  tick the ones to clean, and click buttons. Run `python gui.py`.
* **Command line** – same features from the terminal (`python tgdedup.py ...`).

---

## How duplicates are detected

Controlled by `mode` in `config.ini`:

| mode        | how it matches                        | speed  | notes                          |
|-------------|---------------------------------------|--------|--------------------------------|
| `name_size` | same file **name + byte size**        | fast   | **recommended** – no downloads, near-exact |
| `name`      | same file **name** only               | fast   | looser, can catch renamed sizes|
| `hash`      | same file **content (SHA-256)**       | slow   | downloads each file to compare |

`name_size` is the sweet spot: Telegram reports the exact size instantly, so two
videos with the same name **and** size are almost certainly identical – without
downloading gigabytes of data.

---

## Setup (one time)

### 1. Install Python
Install Python 3.9+ from https://www.python.org/downloads/ (on Windows tick
**"Add Python to PATH"** during install).

### 2. Install the tool
Open a terminal (Command Prompt / PowerShell) in this folder and run:

```
pip install -r requirements.txt
```

### 3. Get your Telegram API keys (free, 2 minutes)
1. Go to https://my.telegram.org and log in with your phone number.
2. Click **API development tools**.
3. Create an app (any name, e.g. "dedup"). You'll get an **api_id** and an
   **api_hash**.

### 4. Configure
Copy `config.example.ini` to `config.ini` and fill in your `api_id` and
`api_hash`. Set which groups to work on under `[groups] targets`
(`all` = every group you're in).

---

## Usage – Desktop app (recommended)

```
python gui.py
```

Then, top to bottom in the window:
1. Enter your **API ID** + **API Hash**, click **Save & Log in** (Telegram sends
   a code to your phone – you type it in once).
2. Click **Load my groups**, tick the groups to clean (none ticked = all).
3. Click **Scan (build index)** – Phase 1.
4. Click **Review duplicates** – see each duplicate set, oldest kept, tick the
   copies to delete, then **Delete ticked copies**.
5. Click **Start watching** – Phase 2. It first catches up on anything added
   while the app was off, then checks every new video live. Leave it running.

**Copy videos between groups:** in section 4, pick a **From** group and a **To**
group, click **Copy all videos**. It forwards every video to the destination
(handles 1000+ with automatic pacing), then detects duplicates there so you can
remove them.

## Usage – Command line

```
python tgdedup.py login     # one-time: log into Telegram (phone + code)
python tgdedup.py groups    # list your groups + their IDs (to pick targets)
python tgdedup.py scan      # PHASE 1: build the index of all videos
python tgdedup.py dedup     # review & delete duplicates already in your stock
python tgdedup.py watch     # PHASE 2: keep running to auto-check NEW videos
python tgdedup.py stats     # show index summary anytime
```

Typical first run: `login` → `scan` → `dedup`, then leave `watch` running.

### Options
- `--auto` – delete duplicates **without** asking each time (use only once you
  trust the results). Default behaviour always asks.
- `--config myfile.ini` – use a different config file.

---

## Notes & safety
- You must be **admin** in a group to delete other people's videos. The tool
  deletes for everyone (`revoke`).
- The index is stored locally in `index.db` (SQLite). Deleting it just means the
  next `scan` rebuilds it – your Telegram data is untouched.
- Your login session is saved in `dedup_session.session`. Keep it private; it
  grants access to your account.
- The tool never uploads or shares anything – it runs entirely on your machine.
