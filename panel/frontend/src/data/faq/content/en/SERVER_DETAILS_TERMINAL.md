# Built-in terminal

Commands on the node host without an SSH session. Output streams live.

## Modes and parameters

- **Single command** — runs in the selected shell.
- **Script** — a multi-line block in a temporary file: variables, loops, functions.
- Shell: `sh` (maximum compatibility) or `bash` (extended syntax).
- Timeout from 30 seconds to 10 minutes. On expiry the process gets SIGTERM, then SIGKILL.
- You see the full result: stdout, stderr and the exit code (`0` means success).

## Limitations

- Interactive programs don't work: `htop`, `top`, `vim`, `nano`, `less`, `passwd`. Use `cat`/`head`/`tail` instead of `less`, `ps aux` instead of `top`, `sed -i` or `echo > file` instead of an editor.
- Very large output is truncated — append `| head -100`, `| tail -50` or `| wc -l`.
- The cancel button stops the output stream and the command itself, but a background process it already spawned keeps running on the server.

## Good to know

- No `sudo` needed: commands run on the host as root, in the host namespace — this is the real host, not the agent container.
- Exit code `127` means command not found: use a full path or install the package.
- Command history is kept in the browser, per administrator.
- To run the same thing on dozens of nodes use Bulk actions: same terminal, but across a group, with a cap on concurrency.
