# wispr-flow-mcp

Lets Claude search everything you have dictated with Wispr Flow. It also keeps a permanent copy of your dictations on
your own Mac, so they survive if Wispr Flow deletes them, and can often bring back ones already deleted.

You can ask Claude things like:

- "What did I dictate about the budget last week?"
- "Show me my last ten dictations in Slack."
- "How many words did I dictate each day this month?"

**Requirements:** a Mac, the Wispr Flow desktop app, and Claude desktop or Claude Code. It does not work on Windows,
or with Wispr Flow on a phone.

This is an independent project, not made by or connected to Wispr Flow or Anthropic. It reads Wispr Flow's private
database, so a future Wispr Flow update could change that database and stop this working until the code is updated.

## What it does

| Part | What it does |
|---|---|
| **Search for Claude** | Gives Claude eight tools for your dictations, personal dictionary and notes. |
| **Permanent archive** | Every 15 minutes, copies new dictations into a private file on your Mac. Nothing is ever deleted from it. |
| **Optional daily backup** | Off unless you turn it on. Saves a checked copy of the archive to a folder you choose and keeps the newest 7. |
| **Recovery tool** | Tries to bring back dictations Wispr Flow has already deleted. You run it by hand. |

## Your privacy

- **Wispr Flow's database is read-only.** See [the guarantee](#the-read-only-guarantee) below.
- **The code makes no network connections.** The search server, archive and recovery tool switch off network access
  in their own process, and a test checks it.
- **Only text and basic details are kept**, such as the time and the app. Audio, screenshots and the on-screen text
  Wispr Flow captures (`axText`, `axHTML`, `textboxContents`) are never read or saved.
- **Logs hold counts only**, never your words.

Your dictations can leave your Mac in two ways:

- **Claude.** Text Claude retrieves becomes part of that Claude conversation, like anything else you share with it.
- **A synced backup folder.** If you point the backup at a folder synced by a cloud service, that service uploads the
  copies.

The archive, backups and recovery results are not encrypted by this tool. They are readable only by your Mac account.

## The read-only guarantee

Four separate protections apply to every read of Wispr Flow's database:

1. Anything that is not a plain read (`SELECT`) is refused before it reaches the database.
2. The file is opened in SQLite's read-only mode, so the database engine itself refuses writes.
3. An allowlist permits only reads of named columns. Audio, screenshots and captured screen text are not on it, so a
   query asking for them fails.
4. Each read is short, with a 20-second limit, so it cannot hold up Wispr Flow.

**How the tests prove it.** The test suite runs on an invented database and tries to break each protection:

- It attempts deletes and other writes, and checks they are refused.
- It plants marker values in the audio and captured-text columns, and checks they never come out of any tool.
- It checks the file is unchanged after every tool has run.

A second set of tests does the same read-only checks on your real file, and you can run it yourself (see
[Running the tests](#running-the-tests)).

SQLite's read-only mode may still use `flow.sqlite-shm`, a small shared coordination file that Wispr Flow also uses. It
holds no dictation data.

## Setup

### The easy way: let Claude Code do it

Install [Claude Code](https://claude.com/claude-code), then paste this into it:

```text
Install wispr-flow-mcp from https://github.com/ehill-amera/wispr-flow-mcp on this Mac.

1. Clone it to ~/wispr-flow-mcp and read its README.
2. Install uv from https://docs.astral.sh/uv/ if it is missing, then run "uv sync" in that folder.
3. Run "uv run pytest" and stop and tell me if anything fails.
4. Run "bin/install-agents". Leave the daily backup off unless I ask for it.
5. Register the server with Claude Code at user scope:
   claude mcp add --scope user wispr-flow "$HOME/wispr-flow-mcp/bin/wispr-flow-mcp"
6. If Claude desktop is installed, back up
   "~/Library/Application Support/Claude/claude_desktop_config.json", then add this server under mcpServers,
   keeping everything else in the file, with the full path to bin/wispr-flow-mcp as the command.
7. Tell me in plain words what you did and whether I need to restart anything.

Never show me or log my dictation text while doing this, and do not delete anything.
```

When it finishes, restart Claude desktop if it asks you to, then ask Claude: "How many words did I dictate this week?"

### The manual way

1. Install [uv](https://docs.astral.sh/uv/).
2. In Terminal:
   ```sh
   git clone https://github.com/ehill-amera/wispr-flow-mcp ~/wispr-flow-mcp
   cd ~/wispr-flow-mcp
   uv sync
   bin/install-agents
   ```
3. For Claude Code, run:
   ```sh
   claude mcp add --scope user wispr-flow "$HOME/wispr-flow-mcp/bin/wispr-flow-mcp"
   ```
4. For Claude desktop, add this under `mcpServers` in
   `~/Library/Application Support/Claude/claude_desktop_config.json`, using your real path, then restart the app:
   ```json
   "wispr-flow": {
     "command": "/full/path/to/wispr-flow-mcp/bin/wispr-flow-mcp"
   }
   ```

### Check Wispr Flow's auto-delete setting

Wispr Flow can remove its local history every 24 hours. While that option is on, the archive only catches what exists
at each 15-minute sync. To keep everything, switch it off in Wispr Flow's settings.

### Turning on the daily backup (optional)

```sh
bin/install-agents --backup-folder "/path/to/your/folder"
```

Any existing folder works, including one synced by a cloud storage app. Copies go in a `wispr-flow-archive` subfolder
at 3:30 AM, or when the Mac next wakes. This code only writes to the local folder; if it is a synced folder, the
cloud app does the uploading.

See `config.example.toml` for the settings. To turn the backup off, remove the `backup_folder` line from
`~/Library/Application Support/wispr-flow-mcp/config.toml`, then run `bin/install-agents` again.

### Timezone

Times use your Mac's timezone. To use a different one, set `timezone` in the config file.

## Recovering deleted dictations

> **⚠️ Do these two things first:**
>
> 1. **Switch off Wispr Flow's 24-hour auto-delete.** If it stays on, it keeps removing data while you try to get it back.
> 2. **Run the recovery soon.** Deleted dictations stay in the database's free space only until new data overwrites
>    them. Every new dictation can destroy some of them, so waiting costs you data, and what is overwritten is gone.

Then, in Terminal:

```sh
cd ~/wispr-flow-mcp
bin/recover
```

It works like this:

1. **Checks first.** It looks at whether the free space still holds data and whether you have room for a full copy of
   the database plus 1 GB. If either check fails, it stops without copying anything.
2. **Copies the database.** It works only on the copy, and only reads the original. The copy is about the same size
   as your Wispr Flow database, which can be several gigabytes.
3. **Reads deleted records from the copy.** This can take several minutes. Only text and basic details go into
   `~/wispr-recovery/recovered.sqlite`. Audio, screenshots and captured screen text do not, and no in-between file
   holds them.
4. **Offers to import** the results into your archive.
5. **Asks before deleting the temporary copy.** Until you delete it, that copy holds your whole database, including
   audio. Delete it unless you have a reason to keep it.

Good to know:

- **Recovery is best effort.** Some dictations will be gone, and some will come back partly damaged or with missing
  fields. Anything the tool can tell is damaged is marked `possibly_damaged`. It cannot catch every case, so treat
  recovered text as a strong lead, not a perfect record.
- **Live data wins.** A recovered dictation still in Wispr Flow is replaced by the live version at the next sync.
- **`recovered.sqlite` remains after the run**, and it holds your dictation text. Delete it yourself once you have
  imported it. The tool will not overwrite an earlier result, so move or delete it before running again.

## The tools Claude gets

| Tool | What it returns |
|---|---|
| `search_dictations` | Dictations matching some text, newest first |
| `recent_dictations` | Your latest dictations |
| `get_dictation` | One dictation with all its text versions |
| `dictation_stats` | Counts and words by day or by app |
| `search_dictionary` | Entries from your personal dictionary |
| `list_dictionary` | Your dictionary, most used first |
| `search_notes` | Notes matching some text |
| `get_note` | One note in full |

Dictation tools search the archive and live data together. Results are capped in size, and say so when they are cut.
Recovered rows can hold garbage, so every tool checks details such as the app and word count before returning them.

## Running the tests

```sh
uv run pytest                  # invented data only
uv run pytest -m live --tb=no  # your real file, read-only; --tb=no keeps your text out of any failure output
```

## Uninstalling

1. Remove the background jobs and their logs:
   ```sh
   cd ~/wispr-flow-mcp
   bin/uninstall
   ```
2. Remove the server from Claude Code:
   ```sh
   claude mcp remove --scope user wispr-flow
   ```
3. Remove the `wispr-flow` entry from `mcpServers` in `claude_desktop_config.json`, and restart Claude desktop.
4. Delete the `~/wispr-flow-mcp` folder.

That leaves your data in place. **Think before deleting it, because the archive may hold dictations Wispr Flow no
longer has.** These are permanent deletions, if you want them:

- `~/Library/Application Support/wispr-flow-mcp/`: the archive and settings
- `~/wispr-recovery/`: recovery results
- the `wispr-flow-archive` subfolder in your backup folder, if you turned the backup on
- `~/Library/Logs/Claude/mcp-server-wispr-flow.log`: Claude desktop's own log, if it exists

Wispr Flow and its database are not affected by any of this.

## License

MIT. See [LICENSE](LICENSE).
