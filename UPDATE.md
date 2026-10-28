# Updating SAGE without losing your data

SAGE keeps its live state separate from its source code. The default database, memories, Obsidian notes, projects, uploads, and generated files live under `data/`. Git releases exclude that directory and credential files.

## First update for an existing Kali installation

The updater works inside a Git clone. If your current Kali copy was installed by copying a folder or a ZIP, migrate once:

1. Stop Sage: `systemctl --user stop sage`.
2. Make a separate safety copy of the current `data/` folder before proceeding.
3. Clone the repository into a new folder: `git clone https://github.com/Kyan-vd-anker/SAGE.git ~/SAGE`. If the repository is private, sign in to GitHub when Git asks.
4. Copy the old `data/` folder into `~/SAGE/data/`. Merge its contents; do not replace it with an empty directory. This carries over `sage.db`, memories, vault notes, projects, backups, and workspace files.
5. If you added personal plugins, copy those files from the old `plugins/` folder into the new one. Do not overwrite the bundled plugins unless you intentionally changed them.
6. Reinstall the user service from the new folder: `cd ~/SAGE && bash scripts/install-kali.sh`. The service definition is updated to point at the new source folder. Telegram credentials remain in `~/.config/sage/telegram.env`.
7. Restore any custom environment settings as a systemd service drop-in, then start Sage and check `systemctl --user status sage` and `journalctl --user -u sage -n 80`.

If you set `SAGE_DATA`, `SAGE_DB`, `SAGE_VAULT`, or `SAGE_WORKSPACE` to locations outside the project, back up and carry those locations forward separately. The default updater archives the in-project `data/` directory; it never deletes user state.

## Later updates

From the Git clone on Kali, run:

```sh
bash scripts/update-kali.sh
```

The script checks that the checkout is clean and points to the SAGE repository, stops the user service if it is running, creates a private timestamped archive under `~/.local/state/sage/backups/`, fetches a fast-forward-only update, checks Python syntax, and restarts the service if it was running. If syntax validation or startup fails, it restores the previous source revision. The database and other `data/` files are kept in place throughout the update.

Keep the backup archives until you have confirmed Sage starts and your memories and projects are present. Restoring a backup is a manual step; do not extract it over a running Sage service.

## First-time upload through VS Code

The SAGE source folder used for this work is a snapshot and has no `.git` directory. That is why it does not appear as a repository in VS Code yet. Clone the existing GitHub repository first; do not initialize a new repository or force-push over the remote history.

1. In VS Code, sign in to GitHub with the account that can access `Kyan-vd-anker/SAGE` (Accounts icon at the lower left).
2. Open the Command Palette with `Ctrl+Shift+P`, run **Git: Clone**, and paste `https://github.com/Kyan-vd-anker/SAGE.git`. Choose a new folder, then select **Open** when the clone finishes.
3. In the clone's integrated PowerShell terminal, run this command. It copies source and documentation from the current snapshot into the clone, updates the version/changelog to v2.2.0, and deliberately leaves the clone's Git history and local data alone:

   ```powershell
   & "C:\Users\Kyan\Documents\Codex\2026-10-08\referenced-chatgpt-conversation-this-is-an\scripts\publish-release.ps1" -RepositoryPath "$PWD" -PrepareOnly -Bump minor -Summary "Improve memory and conversation-gap learning; add safe, documented updates"
   ```

4. Open **Source Control**. Review the changed-file list. The release helper excludes `data/`, databases, backups, `.vscode/`, `work/`, credentials, and `start-sage.local.bat`. Stage the intended source and documentation changes, enter a commit message such as `SAGE v2.2.0`, and commit.
5. Use **Sync Changes** or **Push** in Source Control. The first upload will use the VS Code GitHub sign-in. Keep working from this cloned folder afterward; it contains the `.git` history VS Code and Codex need for later updates.

If VS Code cannot clone the URL, check that the account shown in the Accounts menu is the one with repository access. Do not create a similarly named repository or use force push as a workaround.

## Publishing a release from Windows

After editing SAGE, open PowerShell in the project folder and run:

```powershell
.\scripts\publish-release.ps1 -Bump minor -Summary "Describe the user-visible changes"
```

Use `-Bump patch` for a small fix or `-Bump minor` for a new feature. The major version number is never changed by this script. It clones the configured GitHub repository, copies the source and documentation while excluding runtime data and machine-local settings, updates `VERSION` and `CHANGELOG.md`, creates a commit and `vX.Y.Z` tag, then pushes the commit and tag together. GitHub sign-in and permission to push are required. Review `git diff` and the generated changelog before publishing if you want to inspect the release first.
