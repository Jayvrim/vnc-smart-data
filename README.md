# VNC App

Android-first VNC connectivity prototype.

## Run locally

```powershell
cd "C:\Users\Admin\Desktop\VNC\The VNC APP"
.\Setup VNC.bat
.\Start VNC.bat
```

Open `http://localhost:8080`.

The app code, web assets, checkout page, database, and Python environment are all stored in this folder. It does not depend on the old `smart-data` folder, the parent `.venv`, or the supplied `campuslink_site` files. A system Python installation is required only to recreate the app-local environment if its `.venv` is removed. You can also double-click `Start VNC.bat` after setup.

## Tests

The backend tests use temporary databases and do not change the app database:

```powershell
cd "C:\Users\Admin\Desktop\VNC\The VNC APP\backend"
..\.venv\Scripts\python.exe -B -m unittest -v test_app
```

## Test accounts

Create a customer account in the app.

No default administrator account is created. To seed one, set both `SMARTDATA_ADMIN_EMAIL` and `SMARTDATA_ADMIN_PASSWORD` before the first server startup. Never use a shared or publicly documented password.

For a LAN test link, run the server and open `http://<this-PC-LAN-IP>:8080` on the Android phone.

## Integration status

- Authentication: real SQLite-backed prototype accounts and sessions
- Wallets: backend-authoritative data wallet plus a separate cash wallet; the local demo starts each customer with ₦100,000 in clearly labelled non-redeemable test funds
- Interswitch: the VNC app uses its own bundled inline checkout page and returns checkout results to the app
- Interswitch setup: checkout defaults to the existing `MX153376` merchant code and `9381029` pay item; override with account-issued `INTERSWITCH_MERCHANT_CODE` and `INTERSWITCH_PAY_ITEM_ID` values. Set `INTERSWITCH_MODE=TEST` or `LIVE`; documentation sample IDs are not credentials
- Cash-wallet data purchases: atomically deduct the package price and credit data; idempotency keys prevent retries from charging twice
- Cash-wallet top-ups: start an Interswitch checkout; the server requeries Interswitch using the documented sandbox endpoint in `TEST` mode or live endpoint in `LIVE` mode (override with `INTERSWITCH_REQUERY_URL`), merchant code, transaction reference, and expected amount. It credits only on response code `00`, a matching merchant reference, and an exact integral `Amount` in kobo. Failed or unconfirmed payments add no funds
- Test package: 500 MB for ₦10; data is credited only when the server confirms the full 1000 kobo payment
- Demo wallet seed: enable with `SMARTDATA_ALLOW_DEMO_WALLET=1`; the ₦100,000 seed is not actual money, withdrawable, or transferable
- Demo purchase: explicitly labelled no-payment simulated wallet credit; enable with `SMARTDATA_ALLOW_DEMO_CREDIT=1`
- Demo usage: automatically deducts 0.005 GB every 30 seconds while the app is open and connected; enable with `SMARTDATA_ALLOW_DEMO_USAGE=1`. This is simulated, not device traffic measurement.
- Routes: Smart Route demo compares simulated 4G/5G candidates and automatically chooses the highest demo download throughput, with latency as a tie-breaker
- Network-quality demo: signal, latency, throughput and 4G/5G switches are simulated; no cellular handover occurs
- Network monitoring: browser-reported online state and Network Information API only; simulated throughput is not a live device or carrier measurement
- eSIM, carrier switching and authoritative usage: require authorized partner APIs and agreements

## Public demo deployment

The `render.yaml` Blueprint runs this Flask app on Render's free web-service plan in Interswitch TEST mode. Public deployments must use environment variables for secrets; the repository intentionally contains no administrator password, customer database, or Python virtual environment. The free service uses an ephemeral filesystem, so SQLite accounts and demo balances can be lost when the service restarts or redeploys. Use persistent database/storage before treating it as production or accepting live payments.
