# Roznamcha PWA + Google Play Store Guide

This project is now wired for:
- PWA manifest at `/manifest.webmanifest`
- Service worker at `/service-worker.js`
- Android TWA verification file at `/.well-known/assetlinks.json`

## 1) Configure environment variables (production)

Set these variables where you host Django (Render, etc):

- `ANDROID_APP_ID`
  - Example: `com.roznamcha.app`
- `ANDROID_SHA256_CERT_FINGERPRINTS`
  - Comma-separated SHA-256 cert fingerprints.
  - Include both upload key and Play App Signing key if applicable.
  - Example:
    `AA:BB:...:11,22:33:...:FF`

After deploy, verify:
- `https://roznamcha.app/manifest.webmanifest`
- `https://roznamcha.app/.well-known/assetlinks.json`

## 2) Validate PWA quality

On desktop Chrome:
1. Open app URL.
2. DevTools -> Application -> Manifest (check installability).
3. DevTools -> Application -> Service Workers (active and controlling page).
4. Run Lighthouse PWA audit and fix any red issues.

## 3) Create Android wrapper (Trusted Web Activity)

Recommended: Bubblewrap (from GoogleChromeLabs).

Typical flow:
1. `npm i -g @bubblewrap/cli`
2. `bubblewrap init --manifest https://roznamcha.app/manifest.webmanifest`
3. Use app id matching `ANDROID_APP_ID` (example: `com.roznamcha.app`).
4. `bubblewrap build`

Output will include Android project and AAB/APK build flow.

## 4) Sign and generate AAB

In Android project:
1. Create/upload keystore.
2. Build release AAB (`app-release.aab`).
3. Keep keystore and passwords safely backed up.

## 5) Upload to Google Play Console

1. Create app listing.
2. Upload `app-release.aab` in internal testing first.
3. Complete store listing, screenshots, privacy policy, content rating, and data safety form.
4. Roll out to production after internal testing passes.

## 6) Important checks before submission

- Site runs only on HTTPS.
- Login and core screens work inside Android WebView/Chrome Custom Tabs.
- `assetlinks.json` contains correct package + SHA256 fingerprints.
- No blocked mixed-content requests.
- App has stable icon, name, splash/theme colors.

## Notes

- If `assetlinks.json` returns `[]`, Android app-domain verification will fail.
- Update `ANDROID_SHA256_CERT_FINGERPRINTS` whenever signing keys change.
