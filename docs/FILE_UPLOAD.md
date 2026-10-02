# Drag-and-drop file uploads

noVNC's RFB connection does not provide general file transfer. The optional
upload relay transfers files without opening an inbound port on the Linux
guest:

1. In the same Codespace where `utils/novnc_proxy` is running, open a second
   terminal and run:

   ```bash
   python3 utils/file_transfer_relay.py
   ```

   Keep it running. It listens on port 8765 and prints a one-time access
   token.
2. In the VS Code **Ports** tab, forward port `8765` and set its visibility to
   **Public**. Copy the HTTPS forwarded URL shown there.
3. In noVNC's **Settings > Advanced**, set **File upload URL** to
   `https://<forwarded-host>/api/upload` and **File upload token** to the
   token printed by the relay.
4. In a terminal on the remote Linux desktop, download the receiver script
   and start it:

   ```bash
   curl -fsS 'https://<forwarded-host>/api/client' -o ~/novnc-file-transfer.py
   python3 ~/novnc-file-transfer.py --server 'https://<forwarded-host>'
   ```

   Paste the relay token when prompted. By default, received files are saved
   in `~/Downloads`. Use `--directory /path/to/folder` to choose a different
   destination.
5. Drag a file onto the noVNC desktop. Once the Linux receiver downloads it,
   the file appears in the selected folder and can be opened or copied in the
   remote desktop.

Only outbound HTTPS from the remote Linux machine is needed; no Linux firewall
port has to be opened. If the receiver reports a connection error, outbound
HTTPS to the Codespaces forwarded URL is blocked and this relay method cannot
work on that network.

Anyone who can reach the public forwarded URL still needs the token to upload,
list, or download queued files. Keep the token private and stop the relay with
Ctrl+C when finished. The relay keeps queued files only in temporary storage
and removes them after the Linux client acknowledges a successful download,
or when the relay exits. Uploads are limited to 100 MiB per file and 500 MiB
queued in total.

This is a separate HTTPS transfer path; it does not make VNC Ctrl+C/Ctrl+V
support files. The VNC clipboard continues to handle text.
