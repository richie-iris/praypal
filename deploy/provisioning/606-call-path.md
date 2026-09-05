# The 606 Call Path (Dev Lane Reference)

> [!NOTE]
> This document details the live SIP routing architecture and Twilio/LiveKit IDs for the reference dev lane (`+19736069515`). For other lanes, use `stand-up-lane.sh` to provision and `capture-lane.sh` to record lane state.
>
> **Carrier changed 2026-09-02** ([ADR 0006](../../docs/adr/0006-twilio-single-carrier.md)): Twilio replaced Telnyx for voice as well as messaging. The Telnyx IDs below are kept only where they name what a rebuild must REPLACE; every number in the firewall table and every script reference is Twilio's.

## End to end

```
+19736069515  (606)
      │
      ▼  Twilio Elastic SIP Trunk "phone-pal-dev"
      │   origination sip:173.255.235.130:5060 — routes by IP,
      │   which is why the Linode IP is RESERVED
      ▼
173.255.235.130:5060
      │
      ▼  LiveKit inbound trunk  ST_GH2EDgwJtwZJ   "phone-pal-dev-606"
      │     numbers: +19736069515
      │     allowed_addresses: Telnyx SIP egress only
      ▼
   dispatch rule  SDR_fKTdjHsssPBu   "phone-pal-dev-606"
      │     type: Individual (Caller)
      │     room:  phone-_<caller>_<random>
      ▼
   agent "phone-pal-dev"   ← must equal AGENT_NAME in worker.env
```

Outbound (reminder calls) leaves by a different trunk:

```
LiveKit outbound trunk  ST_r3dJm55sk6aQ  "phone-pal-dev-outbound"
      address:  <TWILIO_TRUNK_DOMAIN>.pstn.twilio.com
      numbers:  +19736069515          (the caller ID presented)
      auth:     phonepaldevout / <password lives in lane.env, never here>
```

`SIP_OUTBOUND_TRUNK_ID=ST_r3dJm55sk6aQ` in `worker.env` refers to this trunk. The
worker will not dial at all unless that variable AND `RT_SCHEDULER_ENABLED=1` are
both set — deliberately two switches, not one.

## Twilio side

- An **Elastic SIP Trunk** `phone-pal-dev` whose *origination* URL is
  `sip:173.255.235.130:5060` and whose *termination* domain is
  `<TWILIO_TRUNK_DOMAIN>.pstn.twilio.com`.
- A **credential list** `phone-pal-dev-outbound` (username `phonepaldevout`)
  attached to the trunk. The LiveKit outbound trunk authenticates with the same
  pair; a mismatch is challenged and refused with nothing in this repo's logs.
- The **number** assigned to the trunk for voice, and its *A MESSAGE COMES IN*
  webhook pointed at `https://<lane-domain>/sms/incoming`.
- A **usage trigger** at `$25/day` on `totalprice`, posting to
  `https://<lane-domain>/twilio/usage`.

**The spend cap moved into the worker.** Telnyx's outbound voice profile refused
the call itself once the daily cap was reached; Twilio has no equivalent — a
usage trigger only calls a webhook. So `rt_carrier.py` asks the account what
today has cost before every unattended dial and refuses at or over
`TWILIO_DAILY_SPEND_USD`, failing closed when it cannot ask. The trigger above
is the alarm, not the brake. A lane that carries the SIP trunk id but not
`TWILIO_ACCOUNT_SID`/`TWILIO_AUTH_TOKEN` in `worker.env` dials nobody, and the
dial tools are never offered to the model at all.

Twilio routes to the box **by IP**, so the Linode IP is a *reserved* IP. A rebuild
that changes the address silently breaks inbound calling with no error anywhere in
this repo — the phone simply rings out. Keep the reservation, and remember the
origination URL on the trunk carries that same address: a moved box needs both.

## Firewall

Linode cloud firewall `phone-pal-dev` (`120376948`), attached to the instance.
Default inbound policy **DROP**. There is no ufw on the host; this is the whole
firewall.

| Action | Proto | Ports | Source |
|---|---|---|---|
| ACCEPT | TCP | 22 | operator addresses only |
| ACCEPT | TCP | 80 | 0.0.0.0/0 — Caddy, ACME challenge |
| ACCEPT | TCP | 443 | 0.0.0.0/0 — Caddy |
| ACCEPT | TCP | 5060 | Twilio SIP signaling |
| ACCEPT | UDP | 5060 | Twilio SIP signaling |
| ACCEPT | UDP | 10000-20000 | Twilio media — SIP RTP |
| ACCEPT | UDP | 50000-60000 | 0.0.0.0/0 — LiveKit WebRTC media |

Twilio SIP **signaling** comes from one `/30` per region and all of them must be
allowed, because a trunk fails over across regions: `54.172.60.0/30`,
`54.244.51.0/30`, `54.171.127.192/30`, `35.156.191.128/30`, `54.65.63.192/30`,
`54.169.127.128/30`, `54.252.254.64/30`, `177.71.206.192/30`. Twilio **media**
comes from one global range, `168.86.128.0/18`. Signaling and media are separate
lists on purpose: opening 5060 to the media range, or the RTP ports to the
signaling ranges, gives a call that connects and carries no audio.

Note the two distinct media ranges: `sip.yaml` uses **10000-20000** for the SIP
leg, `livekit.yaml` uses **50000-60000** for WebRTC. Both must be open or calls
connect and then carry no audio.

## Recreating it

One command, from `lane.env`:

```bash
APPLY=1 bash deploy/provisioning/stand-up-lane.sh
```

Everything on this page is now a script rather than a paragraph to re-type:

| Section above | Script |
|---|---|
| Twilio trunk, origination, credentials, number, spend alarm | `twilio-setup.sh` |
| Firewall table | `firewall-setup.sh` |
| Inbound trunk + dispatch rule | `recreate-routing.sh` |
| Outbound trunk | `outbound-trunk.sh` |
| The whole sequence, in order | `stand-up-lane.sh` |

They all dry-run by default. This page stays because the scripts say *how* and
it says *why* — the IP reservation, the two distinct media ranges, and the
deletion that cost a production dispatch rule are the parts a script cannot
argue for. Keep both, and re-capture after any rebuild:

```bash
HOST=root@<box> LANE=dev bash deploy/provisioning/capture-lane.sh
```

`recreate-routing.sh` creates the inbound trunk and dispatch rule against a
LiveKit server. It is deliberately **not** idempotent-by-deletion: it refuses to
run if a trunk already claims the number, rather than clearing conflicts first.

That refusal is the whole point. The script this replaces deleted dispatch rules
by name across a LiveKit namespace shared by several lanes, which is how a
production dispatch rule was silently destroyed when a second lane was added.
Give every lane its own LiveKit server and credentials, and never let a
provisioning script delete a rule it did not create.
