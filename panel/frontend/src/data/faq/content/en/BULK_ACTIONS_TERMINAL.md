# Bulk terminal

One command across many servers, with per-server results.

## Parameters

- **Single command** or **script** — a multi-line block with variables, loops and functions.
- Shell: `sh` (compatibility) or `bash` (extended syntax).
- Timeout from 30 seconds to 10 minutes; on expiry SIGTERM, then SIGKILL.
- Each server shows its exit code, stdout and stderr.

## Good to know

- No `sudo` needed: commands run on the host as root.
- Interactive programs don't work — `htop`, `vim`, `nano`, `passwd` and the like.
- Chaining: `&&` runs the next command only on success, `;` runs it regardless.
- The command hits every selected node at once and cannot be cancelled. Try anything dangerous on a single node first, from the server details terminal.
- Nodes that didn't answer are either offline (the panel marks that immediately) or ran past the timeout: raise it and retry just those.
- Trim output in the command itself: `| tail -20`, `| grep -c ...` — otherwise reading results from fifty nodes is painful.
