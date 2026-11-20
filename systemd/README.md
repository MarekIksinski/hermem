# systemd service for the hermem web UI

Read-only browser at http://127.0.0.1:8765 (stdlib server, no deps).
User-scoped unit — no root needed; paths use %h (home) so the file is
portable across machines/users.

Install:

    mkdir -p ~/.config/systemd/user
    cp systemd/hermem-webui.service ~/.config/systemd/user/
    systemctl --user daemon-reload
    systemctl --user enable --now hermem-webui

Manage:

    systemctl --user status hermem-webui     # state + last logs
    systemctl --user restart hermem-webui    # after editing webui.py
    journalctl --user -u hermem-webui -f     # follow logs

Notes:
- Linger must be on for start-before-login: `loginctl show-user $USER | grep Linger`
  (enable: `sudo loginctl enable-linger $USER`).
- Restart=on-failure only: after fixing a crash cause, use
  `systemctl --user reset-failed hermem-webui && systemctl --user start hermem-webui`
  if the start-limit is hit.
- Port 8765; pass a different one by editing ExecStart.
