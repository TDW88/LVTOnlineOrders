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

**Each order runs as a subprocess**, not in-process. Python imports a module once per
process, so a long-running server would keep serving whatever the pipeline looked like
when it booted — which caused a real and confusing bug: the portal was refused with
`lvt_customer_id must be a UUID, got None` by a validator that had been changed half an
hour earlier, while the CLI accepted the same payload. Shelling out costs an interpreter
start against ~60s of Salesforce calls and means **you no longer need to restart the
server after editing the pipeline**. Editing `serve.py` itself still requires a restart.

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
| resolve UUIDs → Account Ids, or provision a new account | `steps/resolve.py`, `steps/provision_account.py` | provision only |
| plan bundle lines | `steps/insert_lines.py` | no |
| upsert Opportunity (early stage) | `steps/create_opp.py` | yes |
| find/create Contact + primary contact role | `steps/resolve_contact.py` | yes |
| create primary Quote | `steps/create_quote.py` | yes |
| insert bundle + standalone fee lines | `steps/insert_lines.py` | yes |
| advance stage to target | `steps/create_opp.py` | yes |
| read back and check | `steps/verify.py` | no |

### Why the stage moves last

The opportunity is inserted at `2 - Discovery / Qualification` and only walked up to
`6 - Verbal Commit` at the very end. Four org validation rules force that ordering:

| Rule | Consequence |
|---|---|
| "New Opportunities cannot be created beyond Stage 1" | cannot insert at the target stage |
| "Please select a Primary Contact" | cannot advance before the contact role exists |
| "You must have a primary quote to progress beyond the 4 - Proposal Delivered stage" | cannot advance before the quote exists |
| "Please only move forward one stage at a time" | cannot jump; each stage is saved in turn |

`Apex_Test__c = true` suppresses **only the last of those rules**, which lets the stage jump
straight from 2 to 6 in a single update. Verified explicitly: it does *not* bypass the
create-beyond-stage-1, primary-contact, primary-quote or units rules — those are still
enforced and still satisfied properly by earlier steps. So the flag skips a bookkeeping
rule, not a data-quality one.

The flag is set for the jump and **cleared immediately afterwards**, because no other
opportunity in the org (0 of 356) carries it and a finished order should not look like Apex
test data. If clearing fails the stage change still stands and the run reports
`FLAG STILL SET` rather than failing the order.

Set `use_apex_test_bypass: false` to fall back to walking `stage_progression`
(`2 → 3 → 4 → 5 → 6`, four separate updates). Both paths are tested and both land on the
target stage; the walk is slower but touches no bypass flag.

`advance_stage` only moves an opportunity that is still sitting at the creation stage. If a
rep has since moved it — qualified it further, closed it — a re-run leaves it alone rather
than dragging it backwards.

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

## Quote line group

Every quote gets one line group — the equivalent of **Edit Lines → Add Group** in the
Quote Line Editor — and all lines are assigned to it. Group name follows the org's own
convention, `<billing account> - <location account>`.

The two account fields are set as requested, both pointing at the order's account. Note
the API names bear no resemblance to the QLE labels:

| QLE label | API field |
|---|---|
| Billing Account | `Billing_Account__c` |
| **VMS Account** | **`End_User__c`** |

This matches the org's own practice: of 10,015 existing groups, 9,807 populate
`Billing_Account__c` and 9,800 populate `End_User__c`, holding the same Account Id on both.

### Three traps in creating a group

**1. `Billing_Account__c` has a lookup filter with a two-part prerequisite.** It rejects an
account unless *both*:

- the group's `Account_Filter_Id__c` matches the account's `Account_Filter_Id__c` (copied
  from the account at group creation), **and**
- the account counts as a valid billing account, which requires
  `Account.Billing_Contact__c` to be populated:

```
Account.Billing_Contact__c set
  → Valid_Billing_Account__c formula turns green
  → Show_in_Billing_Results__c becomes true
  → the lookup filter accepts the account
```

Established accounts already satisfy this. A freshly provisioned one does not, and fails
with the thoroughly unhelpful `Value does not exist or does not match filter criteria`. So
`resolve_contact.link_account_billing_contact` sets the account's billing contact — never
overwriting an existing one — before the group is created.

**2. `SBQQ__LineItemsGrouped__c` must be set AFTER the lines exist.** Setting it during
group creation appears to work and then silently reverts: CPQ's recalculation on line
insert resets it to false, leaving a group that exists but is invisible in the editor.
`enable_grouping` runs last and reads the value back rather than assuming.

**3. `Ship_to_Account__c` is deliberately left blank.** Real groups populate it, but a
validation rule ("Invalid Ship_To Account - Quote Line Group") rejects our location
account. Consequence: the group's `Address_Missing__c` formula will be true, so the editor
may flag a missing address. Resolving that needs a CPQ admin to explain what qualifies as
a valid ship-to.

### Empty-quote recovery

Lines are inserted whenever the quote has none, not only when the quote is newly created.
A run that dies between creating the quote and inserting lines leaves an empty quote
behind; without that check the next run adopts it and reports a $0 order as processed. This
happened for real during development, and `verify` caught it via the expected-total
mismatch.

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
| always added | `Setup Fee` $400 (bundle option) |
| always added | round-trip shipping — $2,000 mobile / $1,000 wall (standalone line) |

### Fees

Setup and round-trip shipping go on every order; they are not customer-selectable. Both
are **one-time** charges, so they are *not* extended over the subscription term — confirmed
against CPQ, which prices them at `NetPrice == ListPrice` where recurring lines come out at
monthly × term. Treating them as recurring would be a 12–36x error on the fee portion,
which is why `expected_net_amount` sums them separately.

Quantity tracks the unit count, since both are per unit shipped and installed.

**They attach differently, and this is not cosmetic.**

`Setup Fee` is a configured option of the bundle, so it nests under the bundle head like
any other option (`bundle_option_fees` in config).

Round-trip shipping is **not** an option of this bundle — the only shipping option under
`LVT Bundle` is One-Way. The round-trip products are not options of *any* bundle, and on
the org's existing quotes they appear as **standalone top-level lines** (`SBQQ__RequiredBy__c`
and `SBQQ__ProductOption__c` both null; 156 lines that way). Hermes matches that rather
than altering the product catalog, which would be a change to shared config affecting
every rep. See `standalone_fees`.

The product also varies by form factor, which the names make explicit:

| Unit type | Product | List |
|---|---|---|
| mobile | `Round-Trip Shipping Fee` | $2,000 |
| wall | `Wall / Pole Round-Trip Shipping Fee` | $1,000 |

Everything is pinned **by id**, which matters more for fees than anywhere else because
`ProductCode` is ambiguous: two distinct products share the code `Setup Fee` ($400 and
$1,800 for Live Unit - Surround) and two share `Shipping Fee` ($1,000 and $500). Matching
on code would land a plausible-looking wrong price on every quote.

`verify.py` distinguishes a bundle head from a standalone line by product code, not just by
`SBQQ__RequiredBy__c` being null — standalone fees are also top-level, so counting nulls
alone would over-report heads and mask a genuinely missing bundle head.

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
