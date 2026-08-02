# SSH security

sshd settings, brute-force protection and access keys — across all nodes from one place.

## What you can do

- Edit sshd parameters: port, root login, password login, auth attempts, timeouts.
- Enable and tune Fail2ban, view banned addresses and lift bans.
- Distribute public keys across servers.
- Apply ready presets ("recommended" and "maximum") or save your current settings as one.

Bulk operations stream: each node's result appears as it completes rather than after all of them finish.

## The safe order

1. Upload the key and **verify login with it** in a separate session, keeping the current one open.
2. Only then disable password login.
3. When changing the port, open the new one in the firewall first, and edit sshd second.

## Good to know

- The panel validates the config before applying it, but it can't protect you from a logical mistake such as disabling passwords without adding a key.
- Locked out? Only the provider console helps: edit `/etc/ssh/sshd_config` there and restart the service.
- Port 9100 has nothing to do with SSH: the panel needs it to reach the agent, don't close it.
