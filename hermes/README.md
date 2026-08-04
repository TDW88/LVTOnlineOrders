# Hermes — portal order → Salesforce Opportunity + CPQ Quote

Turns a submitted portal configuration into a real Opportunity with a primary CPQ Quote
and correctly structured bundle lines, ready for the existing approval chain.

Deterministic by design: the portal collects, this script writes. No AI in the
transaction path, so the same payload always produces the same records.

## Running it

### From the portal

```bash
python3 -m hermes.serve              # http://localhost:8971/index.html
python3 -m hermes.serve --dry-run    # resolve every order, write nothing
python3 -m hermes.serve --port 9000
```

Serves the portal *and* exposes `POST /api/order`, which the Review step's **Submit
Order** button calls. A plain `python -m http.server` cannot run the script, so the
button falls back to downloading the payload if this server isn't the one answering —
if you see a download instead of a quote, you're on the static server.

Bound to `127.0.0.1` only, on purpose: the endpoint writes to Salesforce using your CLI
credentials and has no authentication of its own. Don't expose it.

Responses: `200` created, `422` order refused (body carries `code`/`field`/`detail`),
`400` malformed JSON, `502` Salesforce CLI failure, `500` unexpected.

An order takes roughly **60–70 seconds** end to end — about twenty `sf` CLI invocations
plus the CPQ pricing wait. The button stays in a submitting state throughout.

### From the command line

```bash
# validate + resolve only, writes nothing
python3 -m hermes.hermes hermes/payloads/golden.json --dry-run

# create records
python3 -m hermes.hermes hermes/payloads/golden.json

# machine-readable output
python3 -m hermes.hermes hermes/payloads/golden.json --json
```

Exit codes: `0` created (or idempotent re-run), `2` rejected with zero records written,
`3` environment failure.

## Pipeline

| Step | Module | Writes? |
|---|---|---|
| validate payload shape | `steps/validate.py` | no |
| assert correct org | `steps/resolve.py` | no |
| resolve UUIDs → Account Ids | `steps/resolve.py` | no |
| plan bundle lines | `steps/insert_lines.py` | no |
| upsert Opportunity | `steps/create_opp.py` | yes |
| create primary Quote | `steps/create_quote.py` | yes |
| insert bundle lines | `steps/insert_lines.py` | yes |
| read back and check | `steps/verify.py` | no |

Everything that can fail runs before the first write, so a rejection leaves nothing
behind. Find-or-reject, never find-or-create — a rejected order a rep picks up is still
faster than today; a quote on the wrong account is a cleanup incident.

## Environment

Two hazards, both absorbed by `sfcli.py` rather than left to your shell:

1. **Netskope TLS interception.** Node uses its own CA bundle, not the macOS keychain,
   so `sf` dies with `SELF_SIGNED_CERT_IN_CHAIN`. Salesforce reports this as
   `AuthCodeExchangeError: Invalid client credentials`, which sends you after the wrong
   problem entirely. Fixed via `NODE_EXTRA_CA_CERTS=~/.certs/all-system-ca.pem`.
   That file is a **snapshot** — regenerate if Netskope rotates its CA:
   ```bash
   security find-certificate -a -p /Library/Keychains/System.keychain > ~/.certs/all-system-ca.pem
   ```
2. **Node version.** `sf` crashes at import on Node < 22. `sfcli.py` resolves the
   highest installed Node ≥ 22 explicitly instead of trusting PATH, because a stale nvm
   default silently reintroduces the crash. Override with `HERMES_SF_BIN`.

## Pinned IDs

`config.json` holds every Salesforce Id. **A sandbox refresh may invalidate all of
them.** Options are pinned by Id, never by name — several `SBQQ__ProductOption__c` rows
under this bundle share identical product names, so name matching is a coin flip.

The mapping table (`Location_Account_UUID_Mapping__c`, ~10k rows) is only *partially*
aligned with Account data in this sandbox; most rows reference UUIDs that resolve to
nothing. The pinned golden-path pair is verified good. Resolution therefore checks
`ParentId` independently of the mapping row, so a stale or cross-wired mapping row
cannot wave an order through.

## Pricing

v1 quotes at **list price only**. There is no discount logic, and that is what makes the
automation safe to run without a rep in the loop. `verify.py` warns if list ≠ net, which
would mean a discount crept in.

Price semantics, which are easy to get wrong:

| Field | Meaning |
|---|---|
| `SBQQ__ListPrice__c` | per unit, per month |
| `SBQQ__NetPrice__c` | per unit, extended over the term (2833.33 × 12 = 34000) |
| `SBQQ__NetTotal__c` | the above × quantity — this is what sums to the quote total |
| `SBQQ__NetAmount__c` | quote-level total. Authoritative |

Bundle head, `BASEUNIT` and `HEADUNIT` all list at **$0**. All recurring value sits on
`FORM FACTOR SUBSCRIPTION` and `MODULE` lines, so a structurally broken quote can render
fine and total wrong. That is why `verify.py` exists.

**CPQ prices asynchronously and incrementally**, and this caused two wrong totals before
it was solved properly:

1. Reading immediately after insert gives zeros — a healthy quote looks broken.
2. Waiting for the total to *stop changing* is also unsound. Pricing lands line by line,
   and a pause longer than the poll interval makes a partial total look settled. Observed
   live: identical orders reporting 68,000, then 80,000, then 92,000, the differences
   being module lines that had not landed when two consecutive reads agreed.

`verify.py` therefore waits for a **known expected total**, computed by
`insert_lines.expected_net_amount` from the pinned config: for each unit group,
`(form factor monthly + module monthlies) × term × quantity`. Nothing computed there is
ever written — it exists purely so the wait has an unambiguous stopping condition, and so
a structurally wrong quote is caught rather than reported as fine. Do not replace it with
a fixed sleep or a stability check.

## New customers

A payload with **no `lvt_customer_id`** is treated as a new customer: Hermes provisions
the account structure instead of filing the order under an existing account. A payload
**with** one resolves against it exactly as before, and is still rejected if anything is
ambiguous.

The portal decides which path applies by company name: it sends the pinned demo UUIDs
only when the typed company matches the pinned demo account, and `null` otherwise.

What gets created, mirroring the shape existing customers have so orders resolve down one
code path either way:

```
Account "1 - Top/Mid Account"          <- billing, holds LvtCustomerId__c
  └── Account "2 - Location Account"   <- ship-to, holds LvtLocationId__c
Location_Account_UUID_Mapping__c        <- binds the two UUIDs
```

Verified round-trip: a provisioned account resolves through the normal existing-customer
path, ParentId and mapping checks included.

### What this costs

**The minted LVT ids do not exist in VMS.** All ~10,800 pre-existing accounts carry an
`LvtCustomerId__c`, which means accounts originate in VMS and Salesforce is downstream.
An account created here is therefore invisible to VMS, and when the customer is set up
there properly a second Salesforce account will likely appear alongside this one. Nothing
here reconciles that — the account's `Description` records what happened so a human can
merge them. **That merge is a manual job someone has to own.**

This is a deliberate departure from the design doc's find-or-reject rule. Accounts and
locations for *existing* customers still reject rather than create.

**Matching is exact-name only.** `Brasfield and Gorrie` will not match
`Brasfield & Gorrie- GA`, so a customer who types their name differently gets a duplicate
account. Exact repeats are deduplicated correctly (verified: same name reuses the account,
creates no second location, reuses the contact).

**Provisioning is not atomic.** It is three separate writes with no transaction across
them. A validation rule requiring a shipping address applies to the location record type
but *not* to Top/Mid, which really did orphan a billing account during development. On
failure Hermes now raises `PARTIAL_CREATION` naming every record created, and a
re-submission with the same company name reuses a billing account left behind by a
previous failure rather than duplicating it.

### Org quirks this step has to satisfy

- **State/Country picklists are enabled.** Writing `UT` to `BillingState` fails with
  "Please select a state from the list of valid states"; two-letter codes belong in
  `BillingStateCode`, which also requires a country code.
- **"Shipping Address Required"** is enforced on the location record type, so billing
  address alone will not save. Shipping mirrors billing for the single-location v1 case.
- Org automation populates the *other* LVT id on each account after insert (the billing
  account gains an `LvtLocationId__c`, the location gains an `LvtCustomerId__c`). Existing
  accounts look the same way, so this is left alone.

## Primary contact

The name on the portal's contact step becomes the primary contact in the two places this
org actually uses:

| Where | Field | Org usage |
|---|---|---|
| Opportunity | `OpportunityContactRole`, `Role = 'Primary Contact'`, `IsPrimary = true` | 562 of 586 roles |
| Quote | `SBQQ__Quote__c.SBQQ__PrimaryContact__c` | 5,751 quotes — the dominant field |

`Opportunity.ContactId` (281) and `Opportunity.Contact__c` (313) are also populated in
this org but are not set here; say so if they matter. `SalesLoft1__Primary_Contact__c` is
a managed package field and is left alone.

**This step creates a Contact if it cannot find one — the only place Hermes does that.**
The golden-path account had zero Contacts, and so do most location accounts, so
find-or-reject would have refused essentially every order. Accounts and locations still
reject rather than create: attaching a quote to the wrong company is a cleanup incident,
whereas a duplicate person is smaller and more fixable.

Duplicates are avoided by matching, in order: email within the account hierarchy (parent
or any child), then first+last name on the billing account, and only then creating.
Contacts are always created against the **billing** account. Re-running an order reuses
the contact and does not stack a second contact role.

A payload with no contact name links nothing and reports it. Inventing a name from an
email prefix would be worse than leaving the field for a rep.

`Contact` requires only `LastName`, so a single-word name becomes the last name rather
than getting a placeholder first name. Middle names stay with the last name so nothing is
silently dropped: `Ana Maria Reyes` → `Ana` / `Maria Reyes`.

## Portal ↔ CPQ mapping

| Portal | CPQ |
|---|---|
| `unit_type: mobile` | `BASEUNIT` Mobile Mounting Structure - Solar |
| `unit_type: mobile` + `needs_generator` | `BASEUNIT` …SMART GENERATOR variant |
| `unit_type: wall` | `BASEUNIT` Universal Pole/Wall Mount-AC |
| `ndaa_compliant: true` | `HEADUNIT` A-IR-PTZ Base-**Axis**, NDAA |
| `ndaa_compliant: false` | `HEADUNIT` D-IR-PTZ Base-**Dahua** (non-NDAA) |
| software packages | `MODULE` Intelligent Deterrence / Investigations / Safety, $500 each |
| recurring charge | `FORM FACTOR SUBSCRIPTION` … LVT Managed |

Software packages are matched by **display name**, not the portal's internal id: the
portal's `alert-management` displays as "Intelligent Investigations", and CPQ separately
carries a legacy "Alert Management" module. Matching on the id would pick the wrong
product.

`night_vision` has **no CPQ counterpart** — IR/thermal capability is inherent to the head
unit. It travels in the payload for the rep and produces no line. Likewise the portal's
$125 NDAA and $1000 generator uplifts do not exist as SKUs; both are expressed by
product selection instead.

## Known gaps

- **No authentication.** The portal has no LVT identity, so `HERMES_DEMO_IDENTITY` in
  `index.html` hardcodes a known-good sandbox account. Production needs an authenticated
  VMS admin session supplying the UUIDs. Do not ship the hardcoded pair.
- **Portal estimates understate the real quote.** For the golden path the portal shows
  $5,450/mo against a CPQ list of $7,666.67/mo. The portal's rate table is not derived
  from the price book. Totals are labelled non-binding estimates; making them agree means
  re-pricing the portal from `PricebookEntry`.
- **Single location per order.** Every configured unit is attributed to one pinned
  location. Multi-site orders need real per-site UUIDs, which requires the auth work.
- **New-site provisioning is out of scope.** Who mints an `LvtLocationId`, and when, is
  an open design question.

## Still to do by hand

The one test that earns trust: build the same order in the **Quote Line Editor** and
compare net totals against the Hermes quote. CPQ calculation on API-inserted lines is
configuration-dependent, and a quote that displays prices can still be wrong. This has
not been done yet — it needs a human in the CPQ UI.
