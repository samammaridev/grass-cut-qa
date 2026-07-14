# Safeguard POC API — Reference Documentation

Complete reference for the Safeguard work-order API used by the Grass Cut QA agent.
All schemas and examples below were captured from **live calls against test order `500119014`** on 2026-07-13 (dev environment `*.sgpdev.com`). Payload values shown are test data.

---

## 1. Overview

The integration is a simple 4-step read-only flow:

| Step | Purpose | Endpoint |
|---|---|---|
| 1 | Authenticate (Basic → Bearer) | `GET https://isov2authentication.sgpdev.com/json` |
| 2 | Work-order details | `GET https://aiapi.sgpdev.com/aiapi/luggage/{orderNumber}` |
| 3 | Work-order image list | `GET https://aiapi.sgpdev.com/aiapi/images/{orderNumber}` |
| 4 | Download an image | `GET https://image.sgpdev.com/{webFileName}` → 302 → pre-signed S3 URL |

### Hosts & configuration

Credentials live in `.env` (never hardcode; never commit):

| Env var | Value / purpose |
|---|---|
| `SAFEGUARD_AUTH_URL` | `https://isov2authentication.sgpdev.com/json` |
| `SAFEGUARD_AUTH_USER` | `poc` |
| `SAFEGUARD_AUTH_PASSWORD` | Basic-auth password (secret — in `.env` only) |
| `SAFEGUARD_API_BASE` | `https://aiapi.sgpdev.com` |
| `SAFEGUARD_IMAGE_BASE` | `https://image.sgpdev.com` |

---

## 2. Authentication

```bash
curl -u "$SAFEGUARD_AUTH_USER:$SAFEGUARD_AUTH_PASSWORD" "$SAFEGUARD_AUTH_URL"
```

**Response — `200 OK`, `application/json`:**

```json
{
  "access_token": "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9....",
  "token_type": "Bearer",
  "expires_in": 36000
}
```

| Field | Type | Notes |
|---|---|---|
| `access_token` | string | RS256-signed JWT. Send as `Authorization: Bearer <token>` on all subsequent calls. |
| `token_type` | string | Always `"Bearer"`. |
| `expires_in` | int | Seconds. Observed **36000 (10 hours)**. |

Decoded JWT claims (observed): `aud: "sgp"`, `iss: "https://isov2authentication.sgpdev.com"`, `sub` / `unique_name`: `"poc"`, plus standard `iat` / `nbf` / `exp` (`exp − iat` = 36000 s).

**Practical guidance:** fetch one token per run and cache it; refresh when a call returns `403` or when within ~5 min of expiry. Wrong Basic credentials return `401`.

---

## 3. Work-Order Details — `GET /aiapi/luggage/{orderNumber}`

```bash
curl -H "Authorization: Bearer $TOKEN" "$SAFEGUARD_API_BASE/aiapi/luggage/500119014"
```

**Response — `200 OK`, `application/json`** (~14 KB for the test order).

### 3.1 Schema

The payload is a single wrapper around a **dynamic key/value map of "questions"**:

```json
{
  "propertyRecord": {
    "questions": {
      "<Question.Key.Name>": {
        "answers": [
          { "value": "<string>" }
        ]
      }
    }
  }
}
```

| Path | Type | Notes |
|---|---|---|
| `propertyRecord` | object | Only top-level key. |
| `propertyRecord.questions` | object (map) | **Dynamic keys** — 205 keys for order 500119014. The key set varies by order/work-code; treat as a lookup map, not a fixed schema. |
| `questions.<key>.answers` | array | Observed length 1 for every key on this order, but it is an array — always read defensively (take `answers[0].value`, tolerate empty). |
| `answers[].value` | string | **All values are strings**, including numbers, booleans, and dates. |

### 3.2 Value conventions (important for parsing)

- **Enum-style values** use dotted namespaces: `Default.Yes` / `Default.No` / `Default.Unknown`, `Property.OccupancyStatus.Vacant`, `WorkCode.GCL`, `GrassCutType.RegularCut`, `WasGrassCutCompletedAtBackyard.Yes`.
- **Booleans** appear as `Default.Yes|No|Unknown`, as `"true"`, or as `"0"`/`"1"` depending on the field — normalize per field.
- **Dates** appear in at least 3 formats: ISO-8601 UTC (`2026-07-10T19:43:35Z`), ISO with offset (`2026-07-08T00:00:00-04:00`), and plain (`2026-07-10 15:43:35`, `2000-11-30 00:00:00`).
- **Numbers as strings**: money as `"0.00"`, counts as `"42"`.
- **`-1` sentinel** = "not computed / unavailable" on the distance metrics (`WorkOrder.DoorKnockPhotoToPropertyLocationDistance`, `WorkOrder.FarthestImageToPropertyDistance`, `WorkOrder.FarthestDistanceBetweenImages`).

### 3.3 Question-key namespaces

Keys are grouped by prefix (counts from order 500119014):

| Prefix | Count | Contents |
|---|---|---|
| `AuditAssistant.Impediment.1–7` | 7 | The **audit rules/standards text** injected for the QA assistant (e.g. "Check all areas on exterior for large overgrowth…", "Ensure the lawn is maintained."). |
| `Property.*` | ~76 | Property master data: address, city/state/zip, client code, loan info, grass-season window, utilities status, lock codes, occupancy, prior-order history (`Property.LastOpen*`). `Property.Tbl*` variants are raw DB-table mirrors of the same data. |
| `Safeguard.*` | 4 | Request metadata: `CorrelationID` (GUID), `RequestingSystem`, `SourceSystem`, `UserName`. |
| `System.*` | 4 | Server time at request: `CurrentDate` (UTC ISO), `CurrentDay`, `CurrentMonth`, `CurrentDate.WithinGrassSeason` (`"true"`). |
| `WorkOrder.*` / `Workorder.*` | ~114 | Everything about this order: dates, vendor, work code, status/workflow, invoice, contractor questionnaire answers, photo stats. Note both capitalizations exist (`Workorder.YardType`). |

### 3.4 Key fields for the grass-cut QA decision

| Question key | Example value (order 500119014) | QA use |
|---|---|---|
| `WorkOrder.OrderNumber` / `WorkOrder.InvoiceNumber` | `500119014` | Identity check. |
| `WorkOrder.TblWorkCode` / `WorkOrder.TblWorkCodeDesc` | `GCL` / `MONTH/BIMONTHLY GRASS CUT` | Order type. |
| `Property.Address1`, `Property.City`, `Property.State`, `Property.ZipCode` | `1801 WOODWARD AVE`, `SPRINGFIELD`, `OH`, `45506` | **Location match** vs. photo GPS / house-number photo. |
| `WorkOrder.TimeOnSite` | `38` | **Time-on-site check** (minutes) — flags the "3-minute fake cut". |
| `WorkOrder.NumberOfPhotos` | `42` | Cross-check against the images endpoint (42 JPEGs of 50 items — see §4). |
| `WorkOrder.DateGrassCut`, `WorkOrder.CompletedDate` | `2026-07-08T00:00:00-04:00` | Compare with photo `takenDate` EXIF/metadata. |
| `WorkOrder.GrassCutCompleted`, `WorkOrder.GrassCutBackyard` | `Default.Yes`, `WasGrassCutCompletedAtBackyard.Yes` | **Contractor claims** — verify against photos ("cut front, skip back" fraud pattern). |
| `WorkOrder.GCHasViolations`, `WorkOrder.TreesCausingViolationDamage`, `Workorder.ShrubsPresent`, `WorkOrder.GCHasPoolHotTub` | `Default.No` | Extraordinary-condition flags → likely human review. |
| `WorkOrder.SpecialInstructions` / `WorkOrder.TblSplInstruction` | `TEST GRASS Order` | Free-text instructions to honor. |
| `WorkOrder.ResponsibleVendor` / `WorkOrder.TblVendorCode` | `DEVGUY` | Vendor identity; `WorkOrder.VendorAssignmentHistory` shows reassignments. |
| `WorkOrder.DoorKnockPhotoToPropertyLocationDistance`, `WorkOrder.FarthestImageToPropertyDistance`, `WorkOrder.FarthestDistanceBetweenImages` | `-1` | Pre-computed GPS distance metrics when available (`-1` = not computed → compute from image lat/long yourself). |
| `WorkOrder.TblWfRemarks` | `Billing: Fail Tasks: Pass Bids: Pass Damages: Pass Submission: Fail` | Existing workflow audit summary. |
| `WorkOrder.AI.Findings` | `No task found for defects of station: Inside Broom Swept` | Findings from an upstream AI pass, when present. |
| `AuditAssistant.Impediment.*` | rule text | The completion standards the QA agent should enforce. |
| `Property.GrassSeason*`, `System.CurrentDate.WithinGrassSeason` | `MARCH`…, `true` | Season validity of a grass order. |

### 3.5 Abridged real example

```json
{
  "propertyRecord": {
    "questions": {
      "AuditAssistant.Impediment.6": { "answers": [ { "value": "Ensure the lawn is maintained." } ] },
      "Property.Address1":           { "answers": [ { "value": "1801 WOODWARD AVE" } ] },
      "Property.City":               { "answers": [ { "value": "SPRINGFIELD" } ] },
      "Property.State":              { "answers": [ { "value": "OH" } ] },
      "Property.ZipCode":            { "answers": [ { "value": "45506" } ] },
      "WorkOrder.OrderNumber":       { "answers": [ { "value": "500119014" } ] },
      "WorkOrder.TblWorkCodeDesc":   { "answers": [ { "value": "MONTH/BIMONTHLY GRASS CUT" } ] },
      "WorkOrder.TimeOnSite":        { "answers": [ { "value": "38" } ] },
      "WorkOrder.NumberOfPhotos":    { "answers": [ { "value": "42" } ] },
      "WorkOrder.GrassCutCompleted": { "answers": [ { "value": "Default.Yes" } ] },
      "WorkOrder.GrassCutBackyard":  { "answers": [ { "value": "WasGrassCutCompletedAtBackyard.Yes" } ] },
      "WorkOrder.ResponsibleVendor": { "answers": [ { "value": "DEVGUY" } ] }
    }
  }
}
```

---

## 4. Work-Order Images — `GET /aiapi/images/{orderNumber}`

```bash
curl -H "Authorization: Bearer $TOKEN" "$SAFEGUARD_API_BASE/aiapi/images/500119014"
```

**Response — `200 OK`, `application/json`** (~42 KB, 50 items for the test order). No pagination observed — a single `items` array.

### 4.1 Schema

```json
{ "items": [ { ...imageItem } ] }
```

Field reference (presence counted across the 50 items of order 500119014 — fields below 50/50 are **optional/nullable**):

| Field | Type | Presence | Description |
|---|---|---|---|
| `id` | string | 50/50 | Image record ID (numeric string, e.g. `"3002285470"`). |
| `guid` | string | 50/50 | File GUID; also the basename of `imageFileName` / `webFileName`. |
| `orderNumber` | string | 50/50 | Echoes the requested order (`"500119014"`). |
| `category` | string | 50/50 | Grouping. Observed: `Yard Maintenance` (27), `General Property Info` (11), `Miscellaneous` (10), `General Property Information` (2 — note the inconsistent long form). |
| `descPrefix` | string | 50/50 | **Photo stage** — observed: `Before` (7), `During` (4), `After` (16), `Condition` (5), `Bid` (10), `Document` (8). |
| `descText` | string | 50/50 | Station/label, e.g. `Grass Recut - Front Lawn`, `Grass Recut - Rear - Edging, Fence Line and Foundations Free of Tall Grass and Weeds`, `Front Of House`, `Property Address`, `Invoice`. |
| `imageType` | int | 50/50 | Numeric type code. Observed mapping: `5` → `image/jpeg` (42), `3`/`4` → `text/html` (6), `10` → `application/pdf` (2). |
| `mimeType` | string | 50/50 | `image/jpeg`, `text/html`, or `application/pdf`. **Filter on `image/jpeg` for photo analysis.** |
| `imageFileName` | string | 50/50 | `<guid>.jpg` etc. |
| `webFileName` | string | 50/50 | **Relative download path** — `s3/sgpobject-3/order/500/119/014/<guid>.jpg`. Prepend `SAFEGUARD_IMAGE_BASE` (see §5). |
| `generatedImageFilePath` | string | 50/50 | Absolute URL on `https://sbimage.sgpdev.com/...`. **Not reachable from outside (403 with or without token) — do not use; use `webFileName` instead.** |
| `fileSize` | int | 50/50 | Bytes. |
| `imageWidth` / `imageHeight` | int | 42/50 | Pixel dimensions. Present only on JPEG photos. |
| `latitude` / `longitude` | float | 42/50 | **GPS of the photo** (from device EXIF). Present only on JPEG photos. Core input for the location-match check. |
| `takenDate` | string | 48/50 | Capture time, format `YYYY-MM-DD HH:MM:SS` (device local time). |
| `dateCreated` / `dateModified` / `releaseDate` / `scanDate` | string | 50/50 | System timestamps, ISO-like `2026-07-10T13:21:38` (no offset). |
| `contractorId` | int | 48/50 | Uploading contractor. |
| `deptCode` | string | 48/50 | e.g. `"06"` (Grass dept). |

### 4.2 Example items (real, from order 500119014)

A JPEG work photo (the type the QA agent analyzes):

```json
{
  "category": "Yard Maintenance",
  "contractorId": 204537,
  "dateCreated": "2026-07-10T13:21:38",
  "dateModified": "2026-07-10T14:03:25",
  "deptCode": "06",
  "descPrefix": "After",
  "descText": "Grass Recut - Front Lawn",
  "fileSize": 81672,
  "id": "3002285470",
  "imageFileName": "449251ac-6533-419c-be00-695a0893c3bb.jpg",
  "imageHeight": 480,
  "imageWidth": 360,
  "imageType": 5,
  "mimeType": "image/jpeg",
  "orderNumber": "500119014",
  "releaseDate": "2026-07-10T13:21:38",
  "scanDate": "2026-07-10T13:21:38",
  "webFileName": "s3/sgpobject-3/order/500/119/014/449251ac-6533-419c-be00-695a0893c3bb.jpg",
  "generatedImageFilePath": "https://sbimage.sgpdev.com/s3/sgpobject-3/order/500/119/014/449251ac-6533-419c-be00-695a0893c3bb.jpg?e865b30e-...",
  "guid": "449251ac-6533-419c-be00-695a0893c3bb",
  "takenDate": "2026-07-08 15:06:35",
  "latitude": 39.903873,
  "longitude": -83.813357
}
```

A non-photo item (document — no GPS, no dimensions):

```json
{
  "category": "Miscellaneous",
  "descPrefix": "Document",
  "descText": "Invoice",
  "imageType": 10,
  "mimeType": "application/pdf",
  "...": "same envelope fields as above; imageWidth/imageHeight/latitude/longitude absent"
}
```

### 4.3 Observations that matter for the QA agent

- **50 items ≠ 42 photos.** `WorkOrder.NumberOfPhotos` (42) counts only the JPEGs; the other 8 items are HTML/PDF documents (invoices, "Update", "Audit Property Assistant"). Always filter `mimeType == "image/jpeg"`.
- **Before/After pairing** comes from `descPrefix` (`Before`/`During`/`After`), and **station coverage** (front / rear / sides / edging / fence line / foundation / under deck / behind outbuilding) comes from `descText` — together these drive the "all 4 sides addressed" completeness check.
- **GPS sanity example:** on this test order, 41 of 42 photos cluster at ≈ (39.9039, −83.8134) — consistent with the Springfield, OH address — but one `Condition / Rear of House` photo sits at (34.6358, −79.0135), hundreds of miles away. Exactly the anomaly the location-match check must catch.
- `takenDate` spans `2026-07-08 15:06` → `2026-07-10 18:03` on this order; compare against `WorkOrder.DateGrassCut` and use per-photo timestamps to estimate real time on site.
- Photo EXIF survives download (device model, GPS, capture datetime were present in the sample JPEG) — a secondary evidence source beyond the API metadata.

---

## 5. Image Download — `GET {SAFEGUARD_IMAGE_BASE}/{webFileName}`

```bash
# -L follows the 302 to the pre-signed S3 URL
curl -L -H "Authorization: Bearer $TOKEN" \
  "$SAFEGUARD_IMAGE_BASE/s3/sgpobject-3/order/500/119/014/449251ac-6533-419c-be00-695a0893c3bb.jpg" \
  -o photo.jpg
```

**Flow (verified live):**

1. `GET https://image.sgpdev.com/{webFileName}` with the Bearer token →
2. **`302 Found`** with a `Location:` header pointing to a **pre-signed AWS S3 URL** (`...accesspoint.s3-global.amazonaws.com/...&X-Amz-Expires=300&...`) →
3. `GET` the `Location` URL **without any Authorization header** → `200 OK`, image bytes (`content-type: image/jpeg`).

**Rules:**

- The pre-signed URL expires in **300 seconds (5 minutes)** — use it immediately; don't build a queue of pre-resolved URLs.
- Do **not** send the Bearer token to the S3 URL (the signature is the auth). Do send it on every `image.sgpdev.com` request.
- `curl -L` / any redirect-following HTTP client handles this transparently.
- Ignore `generatedImageFilePath` (`sbimage.sgpdev.com`): returns `403` externally.

---

## 6. Errors (observed behavior)

| Scenario | Status | Body |
|---|---|---|
| Wrong Basic-auth credentials (auth endpoint) | `401` | — |
| Missing or invalid Bearer token (`aiapi.*`) | `403` | `Forbidden` (plain text) |
| Unknown / invalid order number (both `luggage` and `images`) | **`500`** | `failed workOrderClient.GetWorkOrderInfo` (plain text) |
| Expired pre-signed S3 URL | `403` (S3 XML) | re-request the `image.sgpdev.com` URL to get a fresh one |

Notes:

- There is **no 404** for unknown orders — a `500` with `failed workOrderClient.GetWorkOrderInfo` is the "not found" signal. Handle it explicitly; don't treat every 500 as a server outage.
- On `403` from `aiapi.*`, refresh the token once and retry before failing.
- Responses are not always JSON on error — parse defensively.

---

## 7. Quick-reference: full happy path

```bash
set -a; source .env; set +a
ORDER=500119014

TOKEN=$(curl -s -u "$SAFEGUARD_AUTH_USER:$SAFEGUARD_AUTH_PASSWORD" "$SAFEGUARD_AUTH_URL" | jq -r .access_token)

curl -s -H "Authorization: Bearer $TOKEN" "$SAFEGUARD_API_BASE/aiapi/luggage/$ORDER" > order.json
curl -s -H "Authorization: Bearer $TOKEN" "$SAFEGUARD_API_BASE/aiapi/images/$ORDER"  > images.json

# download every JPEG photo
jq -r '.items[] | select(.mimeType=="image/jpeg") | .webFileName' images.json | while read -r f; do
  curl -sL -H "Authorization: Bearer $TOKEN" "$SAFEGUARD_IMAGE_BASE/$f" -o "photos/$(basename "$f")"
done
```
