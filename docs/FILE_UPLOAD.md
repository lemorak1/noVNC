# Drag-and-drop file uploads

noVNC's RFB connection does not provide general file transfer. The optional
upload relay transfers files without opening an inbound port on the Linux
guest:

1. Start noVNC with `utils/novnc_proxy`. In this repository the relay starts
   automatically on port 8765 and prints its access token. Use
   `--no-file-relay` to disable that behavior. If you previously started the
   relay manually, stop it first so port 8765 is free.

   The relay saves its token and the approved receiver identity in
   `~/.local/state/novnc-file-relay/state.json` with private file permissions,
   so the URL/token and pairing remain stable across restarts in the same
   Codespace.
2. In the VS Code **Ports** tab, forward port `8765` and set its visibility to
   **Public**. Copy the HTTPS forwarded URL shown there.
3. In noVNC's **Settings > Advanced**, set **File upload URL** to the HTTPS
   forwarded URL itself, for example `https://<forwarded-host>`. noVNC adds
   `/api/upload` automatically. **File upload token** is the token printed
   by the relay.
4. On the Ubuntu desktop, install the X11 clipboard helper and download the
   receiver script:

   ```bash
   sudo apt update && sudo apt install -y xclip
   curl -fsS 'https://<forwarded-host>/api/client' -o ~/novnc-file-transfer.py
   python3 ~/novnc-file-transfer.py \
     --server 'https://<forwarded-host>' --install-user-service
   ```

   This installs a systemd user service and starts it. No token is entered on
   Linux. In noVNC **Settings > Advanced**, click **Approve waiting file
   receiver** and approve the request once. Ubuntu saves the limited receiver
   identity in `~/.config/novnc-file-transfer/token` with private permissions.
   Run the install command from a terminal inside the Ubuntu graphical
   desktop. It installs a desktop-login hook that refreshes `DISPLAY` and
   `XAUTHORITY` and restarts the receiver at each Ubuntu graphical login. The
   receiver saves files into `~/Downloads`. Clipboard polling is activated
   only while the noVNC sync setting is enabled and the browser session is
   connected. Add
   `--directory /path/to/folder` if a different destination is preferred.
5. Drag a file onto the noVNC desktop. Once the Linux receiver downloads it,
   the file appears in the selected folder and can be opened or copied in the
   remote desktop.

Only outbound HTTPS from the remote Linux machine is needed; no Linux firewall
port has to be opened. If the receiver reports a connection error, outbound
HTTPS to the Codespaces forwarded URL is blocked and this relay method cannot
work on that network.

Anyone who can reach the public forwarded URL can request a pairing, but
cannot access files unless the request is explicitly approved in noVNC. Keep
the noVNC token private and stop the relay with Ctrl+C when finished. Receiver
tokens are limited to file retrieval and remain authorized until the relay
state is removed. The relay
keeps queued files only in temporary storage and removes them after the Linux
client acknowledges a successful download, or when the relay exits. Uploads
are limited to 100 MiB per file and 500 MiB queued in total.

The receiver token is saved on Ubuntu and reused across restarts. To revoke all
receiver identities and rotate the noVNC token, stop the relay and delete
`~/.local/state/novnc-file-relay/state.json` in the Codespace, then restart the
relay and update **File upload token** in noVNC. To remove the Ubuntu service,
run `systemctl --user disable --now novnc-file-transfer.service` and delete
`~/.config/novnc-file-transfer/token`.

In noVNC **Settings > Advanced**, enable **Sync clipboard text through the
relay** once. The browser may ask for clipboard permission; keep the noVNC
page open and connected for automatic synchronization. This uses the HTTPS
relay instead of relying on the VNC server's clipboard implementation.
Browsers only allow clipboard access while the noVNC page is focused and
visible, so bring that browser tab/window to the foreground when using sync.

Clipboard text is sensitive: while enabled, copied text is sent through the
Codespace relay and is held in relay memory until replaced or the relay exits.
The relay does not persist or log clipboard contents. Disable the setting
when you do not want clipboard synchronization.

This is a separate HTTPS transfer path; it does not make VNC Ctrl+C/Ctrl+V
support files. Ubuntu clipboard synchronization currently requires an X11
desktop with `xclip`; Wayland clipboard tools are not yet supported.
