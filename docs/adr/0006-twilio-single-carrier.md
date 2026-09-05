# ADR 0006: Twilio is the only carrier, and the daily spend cap lives in the worker

## Status

Accepted (2026-09-02). Supersedes the carrier half of
[ADR 0005](0005-email-recipient-policy.md); the recipient policy there is
unchanged.

## Context

The platform used two carriers. Telnyx carried voice: a SIP connection pointed
at the box's IP, an outbound voice profile, a credential connection, and the
numbers. Twilio arrived later for SMS and MMS alone, and for one day the
messaging code addressed both — `send_sms` preferred Twilio whenever its
credentials existed while the tests asserted the Telnyx endpoint, so the branch
every real text used had no coverage at all.

Two carriers meant two accounts, two sets of credentials in `lane.env`, two
consoles to check at 3am, and two different answers to "did the call reach us".
The number a caller dials and the number they text are the same number; having
it live in two places was the accident, not the design.

One thing Telnyx did that Twilio does not: refuse a call at a daily spend cap.
Telnyx's outbound voice profile carried a `$25/day` limit enforced on the
carrier's side, and the code was written knowing that backstop existed — the
scheduler's own comment says "the carrier should be the backstop, not the only
stop". Twilio's equivalent, a usage trigger, only calls a webhook. Nothing in
Twilio refuses the next call.

## Decision

**Twilio is the only carrier.** Voice arrives over a Twilio Elastic SIP Trunk
whose origination URL points at the box, exactly as the Telnyx connection did,
and leaves over a LiveKit outbound trunk that dials
`<TWILIO_TRUNK_DOMAIN>.pstn.twilio.com` with a termination credential.
`deploy/provisioning/twilio-setup.sh` replaces `telnyx-setup.sh` and provisions
the trunk, origination, credential list, number routing (voice and the SMS
webhook), and the spend alarm. Telnyx remains only in this repository's history.

**The daily spend cap moved into the worker.** `rt_carrier.outbound_allowed()`
reads today's `totalprice` from the Twilio account and refuses at or over
`TWILIO_DAILY_SPEND_USD` before any unattended dial — both the in-call bridge
(`rt_bridge.check_and_record_dial`) and the scheduler's reminder calls. It is
checked before the dial is written to the bridge ledger, and it **fails closed**:
an unreadable bill refuses exactly like a spent one, for the same reason the
bridge ledger does. The figure is cached for 60 seconds.

**The Twilio credentials gate the dial tools.** `bridge_call` and
`schedule_reminder_call` now require `TWILIO_ACCOUNT_SID` and
`TWILIO_AUTH_TOKEN` alongside the SIP trunk, so a lane that cannot ask what
today has cost is never offered a tool that spends money.

**The usage trigger is an alarm, not a brake.** `twilio-setup.sh` creates a
daily `totalprice` trigger posting to `/twilio/usage`. The handler believes
nothing in the request body: it verifies the Twilio signature, then re-reads
the spend from the account and logs `budget.carrier_cap`.

## Consequences

* One account, one console, one credential pair. The auth token now signs
  inbound webhooks as well as authenticating sends and reads, so it cannot be
  scoped send-only and never appears in a log line or telemetry event.
* A lane must set `TWILIO_ACCOUNT_SID` and `TWILIO_AUTH_TOKEN` in `worker.env`
  or it will not dial at all. That is the intended failure: the alternative is
  unattended dialling with no cap.
* The cap is enforced per worker process against an account-wide figure. Two
  lanes on one Twilio account see each other's spend, which is why a lane is a
  separate account or a deliberately shared budget — the same rule as one
  Supabase project per lane.
* The firewall changed shape. Twilio signals from one `/30` per region and all
  of them must be allowed because a trunk fails over between regions, while
  media comes from a single global `/18`. Signaling and media are separate
  lists in `firewall-setup.sh`; conflating them gives a call that connects and
  carries no audio.
* Carrier rates in `rt_costs.py` are Twilio's US pay-as-you-go figures read on
  2026-09-02. Inbound toll-free got cheaper, local inbound and outbound cheaper
  still; the estimate moved and is still an estimate.
* `deploy/provisioning/dev-inventory.md` still shows a pre-migration capture
  (`sip.telnyx.com`). It is a snapshot, not configuration; re-run
  `capture-lane.sh` after the lane is rebuilt.
