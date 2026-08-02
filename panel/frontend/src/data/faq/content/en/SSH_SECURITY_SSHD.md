# sshd parameters

Settings for the SSH daemon. The panel restarts the service after applying them — active sessions are not dropped.

| Parameter | Meaning and advice |
|---|---|
| Port | 22 by default. Changing it reduces scanner noise in logs but isn't protection |
| Root login | `prohibit-password` — key only (the usual choice), `no` — forbidden entirely, `yes` — risky |
| Password login | Disable only after a verified key login |
| Auth attempts | 3–4 is plenty; fewer attempts leave brute force less room |
| Idle timeout | Closes forgotten sessions automatically |
| Public key auth | Should always be enabled |

## Good to know

- Changes are validated before being applied: a config with a syntax error is not saved.
- Changing the port does not open it in the firewall — do that first, or you won't get back in after the restart.
- The current session survives the sshd restart, so verify the new access in another window immediately: while the old connection lives, mistakes are still fixable.
- The "maximum" preset disables passwords entirely: make sure keys are present on every node it targets.
