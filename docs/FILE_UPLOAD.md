# Drag-and-drop file uploads

noVNC's RFB connection does not provide general file transfer. The optional
drag-and-drop upload in the default web UI uses a separate HTTP(S) endpoint;
the remote machine must run or expose a service that accepts the upload.

Set `file_upload_url` in `defaults.json`, `mandatory.json`, the page URL, or
the Settings panel. When a file is dropped on the desktop, noVNC sends one
`multipart/form-data` `POST` request per file. Each request contains one part
named `file`, with the original filename. A successful endpoint must return a
2xx HTTP response. Relative URLs are resolved against the noVNC page URL.

The endpoint is responsible for authentication, destination selection,
filename validation, size limits, and safe storage. Do not expose an
unauthenticated upload service to the network. When noVNC is served over
HTTPS, the upload endpoint must also use HTTPS. A cross-origin endpoint must
allow the noVNC origin with CORS; credentials are only sent for same-origin
requests.

This browser-side support does not install or configure an upload service on
the VNC host.
