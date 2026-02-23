# Next Step: Build Android App (TWA) and Upload to Play Store

Use this from your machine terminal in project root.

## 1) Install prerequisites

- Node.js + npm (already present on your machine)
- Java JDK 17+
- Android Studio (for signing and final AAB generation)

## 2) Verify web endpoints after deploy

- `https://roznamcha.app/manifest.webmanifest`
- `https://roznamcha.app/service-worker.js`
- `https://roznamcha.app/.well-known/assetlinks.json`

`assetlinks.json` must not be empty for production.

## 3) Configure production env vars (server)

- `ANDROID_APP_ID=com.roznamcha.app`
- `ANDROID_SHA256_CERT_FINGERPRINTS=<fingerprint1>,<fingerprint2>`

Include both:
- Upload key fingerprint
- Play App Signing key fingerprint (from Play Console)

## 4) Scaffold and build Android TWA project

```bash
cd /Users/syedamirshah/Documents/standard-zarai
APP_URL=https://roznamcha.app APP_ID=com.roznamcha.app ./scripts/prepare_twa.sh
```

This creates/uses `android-twa/` and runs Bubblewrap init/build.

## 5) Generate release AAB

In generated Android project:
- Configure release signing
- Build `app-release.aab`

## 6) Upload to Play Console

- Start with Internal testing track
- Complete listing + privacy policy + data safety
- Promote to production after test approval

## Troubleshooting

- If Bubblewrap install fails: check internet/proxy settings.
- If app-domain verification fails: verify `/.well-known/assetlinks.json` package id + SHA256 fingerprints exactly match Play Console keys.
