# SSH keys

Public keys for password-free login: a key can't realistically be brute-forced, unlike a password.

## How to add one

1. Create a pair if you don't have one: `ssh-keygen -t ed25519 -C "email"`.
2. Take the **public** half: `cat ~/.ssh/id_ed25519.pub` — the line starts with `ssh-ed25519`.
3. Paste the whole line and pick the servers.
4. Verify in a separate session: `ssh -i ~/.ssh/id_ed25519 root@address`.

## Good to know

- Only the `.pub` file goes here. The private key (`id_ed25519`, no extension) never leaves your machine.
- Ed25519 beats RSA: shorter, faster, stronger. RSA is acceptable from 4096 bits.
- The trailing comment is free-form and handy for telling whose key it is.
- Removing a key from the panel removes it from `authorized_keys` on the selected nodes; active sessions aren't dropped.
- Key added but login fails? Almost always permissions: `.ssh` must be 700, `authorized_keys` 600.
