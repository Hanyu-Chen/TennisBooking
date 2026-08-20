# TennisBooking

Automation for tennis court/clinic booking on PlayByPoint (`app.playbypoint.com`).

## Development setup

```bash
uv sync --all-extras   # or: make sync -- creates .venv with runtime + dev deps
```

Copy `.env.example` (if present) or create `.env` with:

```
USER_EMAIL=you@example.com
USER_PASSWORD=your-playbypoint-password

# Optional locally (your own IP isn't flagged), required on Lambda -- see
# "Deploying to AWS Lambda" below. Template: http://username:password@host:port
PROXY_URL=
```

## Running locally

```bash
# Book a single date/program
uv run python -m playbypoint.book_court 2026-08-19 AdvancedBeginner

# Run the full weekday_schedule.yaml schedule (every occurrence of each defined weekday in the next 8 days)
uv run python -m playbypoint.book_court --scheduled
```

See `playbypoint/weekday_schedule.yaml` for the example weekday -> program(s) mapping and the card used for scheduled runs.

## Circuit breaker

`same_day_protection` (`weekday_schedule.yaml`) controls whether a scheduled run refuses to book a session that falls on today's UTC date, regardless of what time it starts. Defaults to `true`; set to `false` to disable.

## Deploying to AWS Lambda

Before deploying, set up a residential proxy: AWS Lambda's outbound IPs are datacenter ranges, which Cloudflare (fronting `app.playbypoint.com`) challenges even though `curl_cffi`'s TLS impersonation passes fine from a normal home IP. Get a proxy URL from a residential proxy provider (e.g. IPRoyal) and set it as `PROXY_URL` in `.env`, using the template `http://username:password@host:port`.

Two Lambda functions share the same deployment package, pointed at two different handlers:

- **`playbypoint.book_court.lambda_handler`** -- books a single explicit `date`/`program_slug` (event-driven, for ad hoc/manual invokes).
- **`playbypoint.book_court.scheduled_lambda_handler`** -- always books everything in `weekday_schedule.yaml`, ignoring event content (this is what EventBridge should target for the recurring cron booking).

### 1. Build the deployment package

`curl_cffi` and `pydantic-core` ship prebuilt Linux wheels, so this can be built from macOS without Docker -- just tell `uv`/pip to fetch Linux-compatible wheels instead of the host platform's:

```bash
rm -rf /tmp/lambda-package /tmp/lambda-package.zip
mkdir -p /tmp/lambda-package

uv pip install \
  --target /tmp/lambda-package \
  --python-version 3.13 \
  --python-platform x86_64-unknown-linux-gnu \
  --only-binary=:all: \
  curl_cffi python-dotenv pydantic pyyaml

cp -r playbypoint /tmp/lambda-package/
rm -rf /tmp/lambda-package/playbypoint/__pycache__

cd /tmp/lambda-package && zip -rq /tmp/lambda-package.zip . -x '*.pyc' -x '*/__pycache__/*' && cd -
```

This produces `/tmp/lambda-package.zip` (~17MB) -- code plus every dependency, Lambda-Linux-compatible. Rebuild it after any change to `playbypoint/` (including `weekday_schedule.yaml`, which is bundled into the zip, not read from S3/env).

### 2. Create the Lambda function(s) (console)

For each of the two handlers:

- Lambda -> **Create function** -> "Author from scratch"
- Runtime: **Python 3.13** (Python 3.12 works too if 3.13 isn't listed -- nothing here is 3.13-specific)

### 3. Upload the code

- **Code** tab -> **Upload from** -> **.zip file** -> pick `lambda-package.zip`
- If it's over the console's 50MB direct-upload limit, upload the zip to any S3 bucket first, then **Upload from -> Amazon S3 location**
- **Runtime settings -> Edit -> Handler**:
  - `playbypoint.book_court.lambda_handler` for the ad hoc function
  - `playbypoint.book_court.scheduled_lambda_handler` for the scheduled function

### 4. Configure secrets

Don't put `.env` in the zip. **Configuration -> Environment variables** -> add `USER_EMAIL`, `USER_PASSWORD`, and `PROXY_URL` (Lambda encrypts environment variables at rest) on **both** functions.

### 5. Increase the timeout

New functions default to a **3 second** timeout, which isn't enough for login + program fetch + booking, especially routed through the proxy. On **Configuration -> General configuration -> Edit**, set **Timeout** to something like **30 seconds** on **both** functions. Bumping **Memory** a bit too (e.g. 128MB -> 256MB) proportionally increases network throughput, which helps since most of the time is spent waiting on HTTP round-trips through the proxy, not CPU work.

### 6. Test manually before wiring up cron

**Test** tab -> create a test event, e.g.:

For `playbypoint.book_court.lambda_handler`
```json
{"date": "2026-08-19", "program_slug": "AdvancedBeginner"}
```

### 7. Wire up EventBridge (scheduled function only)

On the scheduled function -> **Add trigger** -> **EventBridge (CloudWatch Events)** -> **Create a new rule** -> Schedule expression, e.g. `cron(0 8 * * ? *)` for daily 8am UTC. The console sets the required invoke permission automatically when the trigger is added this way.
