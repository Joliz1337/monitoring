# Hoster access

A rented VM almost always ships with channels the hoster can use to get inside: the hypervisor guest agent can run commands and reset the root password, cloud-init rewrites the password and keys on reboot, and monitoring agents and foreign SSH keys open direct access. This section scans the server, shows what it found, and removes the selected items.

## What it finds

- **QEMU Guest Agent** — the hypervisor uses it to run commands as root and reset the password. The hoster's main access channel.
- **cloud-init** — on the next boot it can re-apply the root password, SSH keys and users from a seed disk or metadata.
- **Monitoring agents** — zabbix, telegraf, node_exporter, netdata, the cloud guest-agent and similar.
- **Foreign SSH keys** — hoster keys in `authorized_keys` (your own key is flagged and not selected by default).
- **Extra users** — cloud-created accounts with login and sudo.
- **Hoster repositories** — apt mirrors and sources with `trusted=yes` (packages served without signature checks).
- **Weakened SSH** — password login and root-by-password enabled.

## How it removes

Removal is irreversible and has no backup: packages are wiped with `apt purge`, services get `disable` + `mask`, foreign keys and users are deleted, and the hoster mirror is swapped for `archive.ubuntu.com`. Before it runs, the section lists everything and asks you to confirm you have independent access to the server.

This section closes access **from inside the guest**. Hoster access at the hypervisor level — disk and memory snapshots, rescue mode — is not covered: every hoster keeps that regardless.
