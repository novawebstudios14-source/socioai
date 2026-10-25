# Nova Cuts import service

Private resolver using yt-dlp 2026.10.21, bgutil 2.0.2 and an isolated WPC 1.1.2 fallback. Deploy this directory as a Docker service. The token provider listens only on loopback; never expose port 4416.

The Docker image includes Chromium and Xvfb. WPC runs in a separate Python environment so bgutil cannot silently take precedence during its retry. A failed resolution or invalid media response triggers the WPC attempt automatically. Each attempt runs in its own process group with a 75-second timeout; browser descendants are stopped when the attempt exits. One resolution is allowed at a time. The caller must allow at least 170 seconds for both attempts.

Every selected video/audio track is checked with a bounded Range request before returning success: HTTP 200/206, MP4 MIME type and an MP4 `ftyp` signature are required. Metadata, token generation or HTTP status alone are insufficient. Real imports remain unverified until media is successfully decoded from the deployed service's network. No paid service is activated by these source changes.

Required secret: IMPORT_RESOLVER_KEY, at least 32 random characters.
Required PUBLIC_ORIGIN: the service's HTTPS origin, without trailing slash.
On Railway, set the service root directory to `/importer-service` and the config path to `/importer-service/railway.json`. Generate a public domain pointing to port 8080, then use its HTTPS origin as PUBLIC_ORIGIN. The checked-in configuration enables sleep mode; this reduces idle usage but does not guarantee zero charges. Verify the account plan and credit before activating a deployment.
PORT defaults to 8080. GET /health is the health check. POST /resolve requires Bearer authentication and {"url":"https://www.youtube.com/watch?v=..."}.

Set the Site's IMPORT_RESOLVER_URL to https://<service-domain>/resolve and IMPORT_RESOLVER_KEY to the same secret. Do not activate before a real import test passes. No credentials, browser cookies or proxy rotation are configured. A YouTube authentication challenge remains a terminal failure.

Resolved video/audio use signed one-hour URLs on this service, so resolution and media transfer share the same network origin. The relay streams bounded chunks, accepts only signed Googlevideo destinations, rejects redirects and never logs keys or signed URLs. One resolution and six concurrent media transfers are allowed per process.
Source selection accepts H.264 MP4 up to 1080p and prefers the highest available audio bitrate within the 64 MB limit. Actual resolution remains limited by the source and available formats.

Run tests: python -m unittest discover -s . -p 'test_*.py'. A passing unit test does not prove YouTube access; validate video and audio in production before activation.
