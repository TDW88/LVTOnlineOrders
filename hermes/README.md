# Hermes — portal order → Salesforce Opportunity + CPQ Quote

Turns a submitted portal configuration into a real Opportunity with a primary CPQ Quote
and correctly structured bundle lines, ready for the existing approval chain.

Deterministic by design: the portal collects, this script writes. No AI in the
transaction path, so the same payload always produces the same records.

## Running it

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

**CPQ prices asynchronously and incrementally.** A partially-calculated quote reports a
non-zero total that is simply too low — observed live: the same order read 68,000
mid-calculation and 92,000 once settled. `verify.py` waits for the total to repeat
across consecutive reads before believing it. Do not replace that with a fixed sleep.

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
